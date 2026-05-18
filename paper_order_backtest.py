#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from historical_replay_backtest import (
    build_replay_analysis,
    fetch_klines_range,
    parse_utc_date,
    to_iso,
)
from market_alert_daemon import (
    apply_protection_layer_to_events,
    build_events,
    default_symbol_state,
    extract_risk_levels,
    load_config,
)
from paper_order_engine import (
    DEFAULT_EVENT_RISK_MULTIPLIERS,
    DEFAULT_ELIGIBLE_EVENTS,
    _close_position,
    _current_equity,
    _insert_fill,
    _open_position,
    _record_equity_snapshot,
    _update_order_status,
    build_paper_report,
    build_sim_order_from_event,
    ensure_paper_db,
    ensure_source_conn,
    ensure_initial_equity_snapshot,
    parse_iso_datetime,
    apply_slippage_price,
    bps_to_pct,
    can_create_order,
)
from protections import normalize_protection_config
from shadow_mode import Candle, normalize_symbol


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Historical paper trading backtest for check_price.")
    parser.add_argument("--config", default="watchlist.json")
    parser.add_argument("--symbols", nargs="*", help="Override symbols from watchlist, e.g. BTC ETH")
    parser.add_argument("--quote", default="USDT")
    parser.add_argument("--start-date", required=True, help="UTC date in YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="UTC date in YYYY-MM-DD")
    parser.add_argument("--paper-db", default="")
    parser.add_argument("--report-md", default="")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--rr-ratio", type=float, default=3.0)
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--starting-equity", type=float, default=10_000.0)
    parser.add_argument("--cancel-after-minutes", type=int, default=360)
    parser.add_argument("--mode", choices=["fixed_rr", "plan_based"], default="fixed_rr")
    parser.add_argument("--eligible-events", default=",".join(DEFAULT_ELIGIBLE_EVENTS))
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--entry-slippage-bps", type=float, default=5.0)
    parser.add_argument("--stop-slippage-bps", type=float, default=10.0)
    parser.add_argument("--take-profit-slippage-bps", type=float, default=5.0)
    parser.add_argument("--max-open-positions", type=int, default=5)
    parser.add_argument("--max-same-side-positions", type=int, default=3)
    parser.add_argument("--max-symbol-positions", type=int, default=2)
    parser.add_argument("--daily-loss-limit-pct", type=float, default=0.0)
    parser.add_argument("--drawdown-halt-pct", type=float, default=0.0)
    return parser.parse_args()


def process_pending_orders_for_candle(
    conn: sqlite3.Connection,
    symbol: str,
    candle: Candle,
    *,
    entry_slippage_bps: float,
) -> dict[str, int]:
    fills = 0
    cancels = 0
    now = dt.datetime.fromtimestamp(candle.ts_ms / 1000, tz=dt.timezone.utc)
    rows = conn.execute(
        """
        SELECT *
        FROM sim_orders
        WHERE status = 'pending' AND symbol = ?
        ORDER BY created_at ASC, id ASC
        """,
        (symbol,),
    ).fetchall()
    for order in rows:
        cancel_after = parse_iso_datetime(order["cancel_after_ts"])
        if cancel_after and now >= cancel_after:
            _update_order_status(
                conn,
                int(order["id"]),
                "canceled",
                now,
                candle.close,
                closed_at=now.isoformat(),
                exit_reason="timeout",
            )
            cancels += 1
            continue
        entry_price = float(order["entry_price"])
        order_type = str(order["order_type"])
        side = str(order["side"])
        if side == "long":
            should_fill = candle.low <= entry_price if order_type == "limit_entry" else candle.high >= entry_price
        else:
            should_fill = candle.high >= entry_price if order_type == "limit_entry" else candle.low <= entry_price
        if not should_fill:
            _update_order_status(conn, int(order["id"]), "pending", now, candle.close)
            continue
        fill_price = apply_slippage_price(entry_price, side, "entry", entry_slippage_bps)
        _insert_fill(conn, int(order["id"]), now, fill_price, "entry_fill")
        _open_position(conn, order, now, fill_price)
        _update_order_status(conn, int(order["id"]), "filled", now, candle.close, filled_at=now.isoformat())
        fills += 1
    conn.commit()
    return {"fills": fills, "cancels": cancels}


def process_open_positions_for_candle(
    conn: sqlite3.Connection,
    symbol: str,
    candle: Candle,
    starting_equity: float,
    *,
    fee_bps: float,
    stop_slippage_bps: float,
    take_profit_slippage_bps: float,
) -> dict[str, int]:
    closed = 0
    now = dt.datetime.fromtimestamp(candle.ts_ms / 1000, tz=dt.timezone.utc)
    rows = conn.execute(
        """
        SELECT *
        FROM sim_positions
        WHERE status = 'open' AND symbol = ?
        ORDER BY opened_at ASC, id ASC
        """,
        (symbol,),
    ).fetchall()
    for position in rows:
        side = str(position["side"])
        stop_loss = float(position["stop_loss"])
        take_profit = float(position["take_profit"])
        exit_reason: str | None = None
        exit_price: float | None = None
        if side == "long":
            hit_stop = candle.low <= stop_loss
            hit_tp = candle.high >= take_profit
            if hit_stop and hit_tp:
                exit_reason = "stop_loss"
                exit_price = stop_loss
            elif hit_stop:
                exit_reason = "stop_loss"
                exit_price = stop_loss
            elif hit_tp:
                exit_reason = "take_profit"
                exit_price = take_profit
        else:
            hit_stop = candle.high >= stop_loss
            hit_tp = candle.low <= take_profit
            if hit_stop and hit_tp:
                exit_reason = "stop_loss"
                exit_price = stop_loss
            elif hit_stop:
                exit_reason = "stop_loss"
                exit_price = stop_loss
            elif hit_tp:
                exit_reason = "take_profit"
                exit_price = take_profit
        if exit_reason is None or exit_price is None:
            conn.execute(
                """
                UPDATE sim_positions
                SET last_price = ?, updated_at = ?
                WHERE id = ?
                """,
                (candle.close, now.isoformat(), position["id"]),
            )
            continue
        exit_slippage_bps = stop_slippage_bps if exit_reason == "stop_loss" else take_profit_slippage_bps
        adjusted_exit_price = apply_slippage_price(exit_price, side, "exit", exit_slippage_bps)
        _close_position(conn, position, now, adjusted_exit_price, exit_reason)
        _update_order_status(
            conn,
            int(position["order_id"]),
            "closed",
            now,
            candle.close,
            closed_at=now.isoformat(),
            exit_price=adjusted_exit_price,
            exit_reason=exit_reason,
        )
        closed_position = conn.execute("SELECT * FROM sim_positions WHERE id = ?", (position["id"],)).fetchone()
        entry_fee_pct = bps_to_pct(fee_bps)
        exit_fee_pct = bps_to_pct(fee_bps)
        gross_realized_r = float(closed_position["realized_r"])
        gross_pnl_pct = float(closed_position["pnl_pct"])
        fee_pct_total = entry_fee_pct + exit_fee_pct
        net_pnl_pct = gross_pnl_pct - fee_pct_total
        entry_price = float(closed_position["entry_price"])
        stop_loss = float(closed_position["stop_loss"])
        risk_abs = abs(entry_price - stop_loss)
        if risk_abs > 0:
            net_realized_r = (net_pnl_pct / 100.0) * entry_price / risk_abs
        else:
            net_realized_r = 0.0
        account_return_pct = float(closed_position["risk_pct"]) * net_realized_r
        conn.execute(
            """
            UPDATE sim_positions
            SET gross_pnl_pct = ?,
                net_pnl_pct = ?,
                gross_realized_r = ?,
                realized_r = ?,
                pnl_pct = ?,
                entry_fee_pct = ?,
                exit_fee_pct = ?,
                account_return_pct = ?
            WHERE id = ?
            """,
            (
                gross_pnl_pct,
                net_pnl_pct,
                gross_realized_r,
                net_realized_r,
                net_pnl_pct,
                entry_fee_pct,
                exit_fee_pct,
                account_return_pct,
                position["id"],
            ),
        )
        closed_position = conn.execute("SELECT * FROM sim_positions WHERE id = ?", (position["id"],)).fetchone()
        current_equity = _current_equity(conn, starting_equity)
        new_equity = current_equity * (1.0 + (float(closed_position["account_return_pct"]) / 100.0))
        _record_equity_snapshot(conn, now, new_equity, f"{position['symbol']} {side} {exit_reason}")
        closed += 1
    conn.commit()
    return {"closed": closed}


def close_remaining_positions(
    conn: sqlite3.Connection,
    final_ts: int,
    starting_equity: float,
    *,
    fee_bps: float,
    take_profit_slippage_bps: float,
) -> int:
    now = dt.datetime.fromtimestamp(final_ts / 1000, tz=dt.timezone.utc)
    closed = 0
    rows = conn.execute("SELECT * FROM sim_positions WHERE status = 'open' ORDER BY opened_at ASC, id ASC").fetchall()
    for position in rows:
        side = str(position["side"])
        exit_price = apply_slippage_price(float(position["last_price"]), side, "exit", take_profit_slippage_bps)
        _close_position(conn, position, now, exit_price, "end_of_backtest")
        _update_order_status(
            conn,
            int(position["order_id"]),
            "closed",
            now,
            exit_price,
            closed_at=now.isoformat(),
            exit_price=exit_price,
            exit_reason="end_of_backtest",
        )
        closed_position = conn.execute("SELECT * FROM sim_positions WHERE id = ?", (position["id"],)).fetchone()
        entry_fee_pct = bps_to_pct(fee_bps)
        exit_fee_pct = bps_to_pct(fee_bps)
        gross_realized_r = float(closed_position["realized_r"])
        gross_pnl_pct = float(closed_position["pnl_pct"])
        fee_pct_total = entry_fee_pct + exit_fee_pct
        net_pnl_pct = gross_pnl_pct - fee_pct_total
        entry_price = float(closed_position["entry_price"])
        stop_loss = float(closed_position["stop_loss"])
        risk_abs = abs(entry_price - stop_loss)
        if risk_abs > 0:
            net_realized_r = (net_pnl_pct / 100.0) * entry_price / risk_abs
        else:
            net_realized_r = 0.0
        account_return_pct = float(closed_position["risk_pct"]) * net_realized_r
        conn.execute(
            """
            UPDATE sim_positions
            SET gross_pnl_pct = ?,
                net_pnl_pct = ?,
                gross_realized_r = ?,
                realized_r = ?,
                pnl_pct = ?,
                entry_fee_pct = ?,
                exit_fee_pct = ?,
                account_return_pct = ?
            WHERE id = ?
            """,
            (
                gross_pnl_pct,
                net_pnl_pct,
                gross_realized_r,
                net_realized_r,
                net_pnl_pct,
                entry_fee_pct,
                exit_fee_pct,
                account_return_pct,
                position["id"],
            ),
        )
        closed_position = conn.execute("SELECT * FROM sim_positions WHERE id = ?", (position["id"],)).fetchone()
        current_equity = _current_equity(conn, starting_equity)
        new_equity = current_equity * (1.0 + (float(closed_position["account_return_pct"]) / 100.0))
        _record_equity_snapshot(conn, now, new_equity, f"{position['symbol']} {position['side']} end_of_backtest")
        closed += 1
    conn.commit()
    return closed


def build_source_event(event_id: int, symbol: str, event: dict[str, Any], analysis: dict[str, Any], event_ts_ms: int) -> dict[str, Any]:
    stop_loss, _, _ = extract_risk_levels(analysis, event["event_type"])
    return {
        "id": event_id,
        "event_key": f"{symbol}:{event['event_type']}:{event_ts_ms}:{round(float(event['level']), 6)}",
        "symbol": symbol,
        "event_type": event["event_type"],
        "direction": event.get("direction"),
        "sent_at": to_iso(event_ts_ms),
        "last_price": float(analysis["price"]),
        "level": float(event["level"]),
        "stop_loss": stop_loss,
        "protections_json": json.dumps(analysis.get("protections", {}), ensure_ascii=False),
        "long_short_plan_json": json.dumps(analysis.get("long_short_plan", {}), ensure_ascii=False),
    }


def build_backtest_summary(
    conn: sqlite3.Connection,
    start_dt: dt.datetime,
    end_dt: dt.datetime,
    *,
    created_event_counts: dict[str, int] | None = None,
    blocked_reason_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    total_orders = int(conn.execute("SELECT COUNT(*) FROM sim_orders").fetchone()[0])
    status_counts = {
        row["status"]: int(row["n"])
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM sim_orders GROUP BY status").fetchall()
    }
    type_counts = {
        row["event_type"]: int(row["n"])
        for row in conn.execute("SELECT event_type, COUNT(*) AS n FROM sim_orders GROUP BY event_type ORDER BY n DESC, event_type ASC").fetchall()
    }
    event_net_stats = [
        {
            "event_type": str(row["event_type"]),
            "trades": int(row["trades"] or 0),
            "win_rate": float(row["win_rate"] or 0.0),
            "avg_net_pnl_pct": float(row["avg_net_pnl_pct"] or 0.0),
            "avg_net_realized_r": float(row["avg_net_realized_r"] or 0.0),
        }
        for row in conn.execute(
            """
            SELECT
                o.event_type AS event_type,
                COUNT(*) AS trades,
                AVG(CASE WHEN p.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0 AS win_rate,
                AVG(p.pnl_pct) AS avg_net_pnl_pct,
                AVG(p.realized_r) AS avg_net_realized_r
            FROM sim_positions p
            JOIN sim_orders o ON o.id = p.order_id
            WHERE p.status = 'closed'
            GROUP BY o.event_type
            ORDER BY avg_net_realized_r DESC, trades DESC, o.event_type ASC
            """
        ).fetchall()
    ]
    symbol_event_net_stats = [
        {
            "symbol": str(row["symbol"]),
            "event_type": str(row["event_type"]),
            "trades": int(row["trades"] or 0),
            "win_rate": float(row["win_rate"] or 0.0),
            "avg_net_pnl_pct": float(row["avg_net_pnl_pct"] or 0.0),
            "avg_net_realized_r": float(row["avg_net_realized_r"] or 0.0),
        }
        for row in conn.execute(
            """
            SELECT
                p.symbol AS symbol,
                o.event_type AS event_type,
                COUNT(*) AS trades,
                AVG(CASE WHEN p.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0 AS win_rate,
                AVG(p.pnl_pct) AS avg_net_pnl_pct,
                AVG(p.realized_r) AS avg_net_realized_r
            FROM sim_positions p
            JOIN sim_orders o ON o.id = p.order_id
            WHERE p.status = 'closed'
            GROUP BY p.symbol, o.event_type
            HAVING COUNT(*) >= 3
            ORDER BY avg_net_realized_r DESC, trades DESC, p.symbol ASC, o.event_type ASC
            """
        ).fetchall()
    ]
    trade_stats = conn.execute(
        """
        SELECT
            COUNT(*) AS trades,
            AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0 AS win_rate,
            AVG(pnl_pct) AS avg_pnl_pct,
            AVG(realized_r) AS avg_realized_r,
            AVG(gross_pnl_pct) AS avg_gross_pnl_pct,
            AVG(gross_realized_r) AS avg_gross_realized_r,
            SUM(entry_fee_pct + exit_fee_pct) AS total_fee_pct
        FROM sim_positions
        WHERE status = 'closed'
        """
    ).fetchone()
    equity_row = conn.execute("SELECT equity FROM sim_equity_curve ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "start_date_utc": start_dt.date().isoformat(),
        "end_date_utc": end_dt.date().isoformat(),
        "total_orders": total_orders,
        "status_counts": status_counts,
        "type_counts": type_counts,
        "trades": int(trade_stats["trades"] or 0),
        "win_rate": float(trade_stats["win_rate"] or 0.0),
        "avg_pnl_pct": float(trade_stats["avg_pnl_pct"] or 0.0),
        "avg_realized_r": float(trade_stats["avg_realized_r"] or 0.0),
        "avg_gross_pnl_pct": float(trade_stats["avg_gross_pnl_pct"] or 0.0),
        "avg_gross_realized_r": float(trade_stats["avg_gross_realized_r"] or 0.0),
        "total_fee_pct": float(trade_stats["total_fee_pct"] or 0.0),
        "latest_equity": float(equity_row["equity"]) if equity_row else 0.0,
        "created_event_counts": created_event_counts or {},
        "blocked_reason_counts": blocked_reason_counts or {},
        "event_net_stats": event_net_stats,
        "symbol_event_net_stats": symbol_event_net_stats,
    }


def write_backtest_md(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Paper Trading Backtest",
        "",
        f"- Generated At: `{summary['generated_at']}`",
        f"- Range: `{summary['start_date_utc']}` ~ `{summary['end_date_utc']}`",
        f"- Total Orders: `{summary['total_orders']}`",
        f"- Latest Equity: `{summary['latest_equity']:.2f}`",
        "",
        "## Closed Trade Stats",
        "",
        f"- Trades: `{summary['trades']}`",
        f"- Win Rate: `{summary['win_rate']:.1f}%`",
        f"- Avg Gross PnL %: `{summary['avg_gross_pnl_pct']:.3f}%`",
        f"- Avg Net PnL %: `{summary['avg_pnl_pct']:.3f}%`",
        f"- Avg Gross Realized R: `{summary['avg_gross_realized_r']:.3f}`",
        f"- Avg Net Realized R: `{summary['avg_realized_r']:.3f}`",
        f"- Total Fee % Charged: `{summary['total_fee_pct']:.3f}%`",
        "",
        "## Order Status Counts",
        "",
    ]
    if summary["status_counts"]:
        for status, count in sorted(summary["status_counts"].items()):
            lines.append(f"- `{status}`: `{count}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Event Type Counts", ""])
    if summary["type_counts"]:
        for event_type, count in summary["type_counts"].items():
            lines.append(f"- `{event_type}`: `{count}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Blocked Reasons", ""])
    if summary.get("blocked_reason_counts"):
        for reason, count in sorted(summary["blocked_reason_counts"].items()):
            lines.append(f"- `{reason}`: `{count}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Event Net Expectancy", ""])
    if summary.get("event_net_stats"):
        for row in summary["event_net_stats"]:
            lines.append(
                f"- `{row['event_type']}`: trades `{row['trades']}`, win `{row['win_rate']:.1f}%`, net pnl `{row['avg_net_pnl_pct']:.3f}%`, net R `{row['avg_net_realized_r']:.3f}`"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Symbol Event Net Expectancy", ""])
    if summary.get("symbol_event_net_stats"):
        for row in summary["symbol_event_net_stats"]:
            lines.append(
                f"- `{row['symbol']} / {row['event_type']}`: trades `{row['trades']}`, win `{row['win_rate']:.1f}%`, net pnl `{row['avg_net_pnl_pct']:.3f}%`, net R `{row['avg_net_realized_r']:.3f}`"
            )
    else:
        lines.append("- none")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    config = load_config(Path(args.config))
    symbols = [normalize_symbol(s, args.quote) for s in (args.symbols or config.get("symbols", []))]
    if not symbols:
        raise RuntimeError("No symbols provided.")

    start_dt = parse_utc_date(args.start_date)
    end_dt = parse_utc_date(args.end_date)
    if end_dt <= start_dt:
        raise ValueError("end-date must be after start-date")

    warmup_dt = start_dt - dt.timedelta(days=12)
    range_label = f"{start_dt.date().isoformat()}_{end_dt.date().isoformat()}"
    paper_db_path = Path(args.paper_db) if args.paper_db else Path(f"paper_trading_backtest_{range_label}.db")
    report_md_path = Path(args.report_md) if args.report_md else Path(f"paper_trading_backtest_{range_label}.md")
    output_json_path = Path(args.output_json) if args.output_json else Path("reports") / f"paper_trading_backtest_{range_label}.json"
    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    if paper_db_path.exists():
        paper_db_path.unlink()

    paper_conn = ensure_paper_db(paper_db_path)
    ensure_initial_equity_snapshot(paper_conn, float(args.starting_equity))
    settings = dict(config.get("alerts", {}))
    protection_settings = normalize_protection_config(config.get("protections", {}))
    eligible_events = {item.strip() for item in str(args.eligible_events).split(",") if item.strip()}

    event_id = 1
    stats = Counter()
    blocked_reasons = Counter()
    final_ts_ms = int(start_dt.timestamp() * 1000)
    try:
        for symbol in symbols:
            candles_5m = fetch_klines_range(symbol, "5m", warmup_dt, end_dt, args.timeout)
            candles_15m = fetch_klines_range(symbol, "15m", warmup_dt, end_dt, args.timeout)
            candles_1h = fetch_klines_range(symbol, "1h", warmup_dt, end_dt, args.timeout)
            candles_4h = fetch_klines_range(symbol, "4h", warmup_dt, end_dt, args.timeout)
            ts_15m = [c.ts_ms for c in candles_15m]
            ts_1h = [c.ts_ms for c in candles_1h]
            ts_4h = [c.ts_ms for c in candles_4h]
            symbol_state = default_symbol_state(symbol)
            cooldown_by_key: dict[str, int] = {}

            for idx, candle in enumerate(candles_5m):
                current_dt = dt.datetime.fromtimestamp(candle.ts_ms / 1000, tz=dt.timezone.utc)
                if current_dt < start_dt or current_dt > end_dt:
                    continue
                final_ts_ms = max(final_ts_ms, candle.ts_ms)

                process_pending_orders_for_candle(
                    paper_conn,
                    symbol,
                    candle,
                    entry_slippage_bps=float(args.entry_slippage_bps),
                )
                process_open_positions_for_candle(
                    paper_conn,
                    symbol,
                    candle,
                    float(args.starting_equity),
                    fee_bps=float(args.fee_bps),
                    stop_slippage_bps=float(args.stop_slippage_bps),
                    take_profit_slippage_bps=float(args.take_profit_slippage_bps),
                )

                i1 = next((i for i in range(len(ts_1h) - 1, -1, -1) if ts_1h[i] <= candle.ts_ms), -1)
                i15 = next((i for i in range(len(ts_15m) - 1, -1, -1) if ts_15m[i] <= candle.ts_ms), -1)
                i4 = next((i for i in range(len(ts_4h) - 1, -1, -1) if ts_4h[i] <= candle.ts_ms), -1)
                if i15 < 49 or i1 < 49 or i4 < 49 or idx < 288:
                    continue

                analysis = build_replay_analysis(
                    symbol=symbol,
                    candles_5m=candles_5m[: idx + 1],
                    candles_15m=candles_15m[: i15 + 1],
                    candles_1h=candles_1h[: i1 + 1],
                    candles_4h=candles_4h[: i4 + 1],
                    macro_calendar=[],
                    profile=str(config.get("risk_profile", "conservative")),
                    protection_settings=protection_settings,
                )
                replay_events, symbol_state = build_events(analysis, settings, symbol_state)
                replay_events = apply_protection_layer_to_events(replay_events, analysis.get("protections", {}))
                for replay_event in replay_events:
                    if replay_event["event_type"] not in eligible_events:
                        continue
                    event_key = f"{symbol}:{replay_event['event_type']}:{round(float(replay_event['level']), 4)}"
                    cooldown_minutes = int(config.get("cooldown_minutes", 30))
                    last_sent_ts = cooldown_by_key.get(event_key)
                    if last_sent_ts is not None and candle.ts_ms - last_sent_ts < cooldown_minutes * 60 * 1000:
                        continue
                    cooldown_by_key[event_key] = candle.ts_ms

                    source_event = build_source_event(event_id, symbol, replay_event, analysis, candle.ts_ms)
                    order = build_sim_order_from_event(
                        source_event,
                        rr_ratio=float(args.rr_ratio),
                        risk_pct=float(args.risk_pct),
                        default_cancel_after_minutes=int(args.cancel_after_minutes),
                        mode=str(args.mode),
                        now=current_dt,
                        risk_pct_multipliers=DEFAULT_EVENT_RISK_MULTIPLIERS,
                    )
                    event_id += 1
                    if not order:
                        continue
                    allowed, reason = can_create_order(
                        paper_conn,
                        side=str(order["side"]),
                        symbol=str(order["symbol"]),
                        now=current_dt,
                        starting_equity=float(args.starting_equity),
                        max_open_positions=int(args.max_open_positions),
                        max_same_side_positions=int(args.max_same_side_positions),
                        max_symbol_positions=int(args.max_symbol_positions),
                        daily_loss_limit_pct=float(args.daily_loss_limit_pct),
                        drawdown_halt_pct=float(args.drawdown_halt_pct),
                    )
                    if not allowed:
                        blocked_reasons[str(reason or "unknown")] += 1
                        continue
                    fields = ", ".join(order.keys())
                    placeholders = ", ".join("?" for _ in order)
                    paper_conn.execute(
                        f"INSERT INTO sim_orders ({fields}) VALUES ({placeholders})",
                        tuple(order.values()),
                    )
                    paper_conn.commit()
                    stats[order["event_type"]] += 1

        close_remaining_positions(
            paper_conn,
            final_ts_ms,
            float(args.starting_equity),
            fee_bps=float(args.fee_bps),
            take_profit_slippage_bps=float(args.take_profit_slippage_bps),
        )
        build_paper_report(paper_conn, report_md_path)
        summary = build_backtest_summary(
            paper_conn,
            start_dt,
            end_dt,
            created_event_counts=dict(stats),
            blocked_reason_counts=dict(blocked_reasons),
        )
        summary["eligible_events"] = sorted(eligible_events)
        output_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        write_backtest_md(report_md_path, summary)
        print(json.dumps({"paper_db": str(paper_db_path), "report_md": str(report_md_path), "output_json": str(output_json_path), "total_orders": summary["total_orders"]}, ensure_ascii=False))
    finally:
        paper_conn.close()


if __name__ == "__main__":
    main()
