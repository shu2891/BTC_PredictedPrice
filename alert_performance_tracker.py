#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Callable

import requests

from analytics_pipeline import (
    build_conditional_probability_report,
    build_parameter_tuning_report,
    export_market_sample_store,
)
from event_types import event_direction, event_role, is_watch_only_event


USER_AGENT = "alert-performance-tracker/1.0"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
HORIZON_MINUTES = {
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "24h": 1440,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill alert performance and generate markdown report.")
    p.add_argument("--db-path", default="alert_state.db")
    p.add_argument("--horizons", default="15m,1h,4h,24h")
    p.add_argument("--analytics-dir", default="analytics")
    p.add_argument("--analytics-db", default="analytics/market_samples.duckdb")
    p.add_argument("--probability-report-md", default="條件機率分析報告.md")
    p.add_argument("--probability-min-samples", type=int, default=3)
    p.add_argument("--tuning-report-md", default="參數調校建議報告.md")
    p.add_argument("--tuning-min-samples", type=int, default=5)
    p.add_argument("--report-md", default="提醒事件成效報告.md")
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--loop-minutes", type=int, default=0)
    return p.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            symbol TEXT NOT NULL,
            event_type TEXT NOT NULL,
            direction TEXT NOT NULL,
            level REAL NOT NULL,
            sent_at TEXT NOT NULL,
            last_price REAL NOT NULL,
            stop_loss REAL,
            take_profit_1 REAL,
            take_profit_2 REAL,
            timeframe_view_json TEXT NOT NULL,
            actionable_levels_json TEXT NOT NULL,
            short_term_signal_json TEXT NOT NULL,
            protections_json TEXT,
            long_short_plan_json TEXT NOT NULL,
            message TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_event_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            horizon_label TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            bars INTEGER NOT NULL,
            close_price REAL NOT NULL,
            close_return_pct REAL NOT NULL,
            max_runup_pct REAL NOT NULL,
            max_drawdown_pct REAL NOT NULL,
            tp1_hit INTEGER NOT NULL,
            tp2_hit INTEGER NOT NULL,
            stop_loss_hit INTEGER NOT NULL,
            evaluation_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(event_id, horizon_label)
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
    if "protections_json" not in existing:
        conn.execute("ALTER TABLE alert_events ADD COLUMN protections_json TEXT")
    conn.commit()
def migrate_legacy_alerts(conn: sqlite3.Connection) -> int:
    alerts_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'"
    ).fetchone()
    if not alerts_table:
        return 0
    rows = conn.execute(
        """
        SELECT event_key, symbol, event_type, level, last_sent_at, last_price, message
        FROM alerts
        """
    ).fetchall()
    inserted = 0
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM alert_events WHERE event_key = ? AND sent_at = ?",
            (row["event_key"], row["last_sent_at"]),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO alert_events (
                event_key, symbol, event_type, direction, level, sent_at, last_price,
                stop_loss, take_profit_1, take_profit_2, timeframe_view_json,
                actionable_levels_json, short_term_signal_json, protections_json, long_short_plan_json, message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["event_key"],
                row["symbol"],
                row["event_type"],
                event_direction(row["event_type"]),
                row["level"],
                row["last_sent_at"],
                row["last_price"],
                None,
                None,
                None,
                "{}",
                "{}",
                "{}",
                "{}",
                "{}",
                row["message"],
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch_binance_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    timeout: int,
) -> list[dict[str, float | int]]:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(
        BINANCE_KLINES_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        },
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    bars: list[dict[str, float | int]] = []
    for row in data:
        bars.append(
            {
                "open_time": int(row[0]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
            }
        )
    return bars


def horizon_to_minutes(label: str) -> int:
    if label not in HORIZON_MINUTES:
        raise ValueError(f"Unsupported horizon: {label}")
    return HORIZON_MINUTES[label]


def interval_for_horizon(label: str) -> str:
    mapping = {
        "15m": "1m",
        "1h": "5m",
        "4h": "5m",
        "24h": "15m",
    }
    return mapping[label]


def evaluate_event(event: sqlite3.Row, horizon_label: str, timeout: int) -> dict[str, Any] | None:
    sent_at = dt.datetime.fromisoformat(event["sent_at"])
    end_at = sent_at + dt.timedelta(minutes=horizon_to_minutes(horizon_label))
    if utc_now() < end_at:
        return None

    start_ms = int(sent_at.timestamp() * 1000)
    end_ms = int(end_at.timestamp() * 1000)
    bars = fetch_binance_klines_range(
        symbol=event["symbol"],
        interval=interval_for_horizon(horizon_label),
        start_ms=start_ms,
        end_ms=end_ms,
        timeout=timeout,
    )
    if not bars:
        return None

    entry = float(event["last_price"])
    direction = event["direction"]
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]
    close_price = float(bars[-1]["close"])

    if direction == "up":
        max_runup_pct = (max(highs) / entry - 1) * 100
        max_drawdown_pct = (min(lows) / entry - 1) * 100
        close_return_pct = (close_price / entry - 1) * 100
        tp1_hit = int(event["take_profit_1"] is not None and max(highs) >= float(event["take_profit_1"]))
        tp2_hit = int(event["take_profit_2"] is not None and max(highs) >= float(event["take_profit_2"]))
        stop_loss_hit = int(event["stop_loss"] is not None and min(lows) <= float(event["stop_loss"]))
    elif direction == "down":
        max_runup_pct = (entry / min(lows) - 1) * 100
        max_drawdown_pct = -((max(highs) / entry - 1) * 100)
        close_return_pct = (entry / close_price - 1) * 100
        tp1_hit = int(event["take_profit_1"] is not None and min(lows) <= float(event["take_profit_1"]))
        tp2_hit = int(event["take_profit_2"] is not None and min(lows) <= float(event["take_profit_2"]))
        stop_loss_hit = int(event["stop_loss"] is not None and max(highs) >= float(event["stop_loss"]))
    else:
        max_runup_pct = (max(highs) / entry - 1) * 100
        max_drawdown_pct = (min(lows) / entry - 1) * 100
        close_return_pct = (close_price / entry - 1) * 100
        tp1_hit = 0
        tp2_hit = 0
        stop_loss_hit = 0

    evaluation = {
        "high_price": round(max(highs), 4),
        "low_price": round(min(lows), 4),
        "close_price": round(close_price, 4),
        "entry_price": round(entry, 4),
        "direction": direction,
    }
    return {
        "window_start": sent_at.isoformat(),
        "window_end": end_at.isoformat(),
        "bars": len(bars),
        "close_price": round(close_price, 4),
        "close_return_pct": round(close_return_pct, 3),
        "max_runup_pct": round(max_runup_pct, 3),
        "max_drawdown_pct": round(max_drawdown_pct, 3),
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
        "stop_loss_hit": stop_loss_hit,
        "evaluation_json": json.dumps(evaluation, ensure_ascii=False),
    }


def backfill(conn: sqlite3.Connection, horizons: list[str], timeout: int) -> tuple[int, int]:
    inserted = 0
    errors = 0
    rows = conn.execute(
        """
        SELECT *
        FROM alert_events
        ORDER BY sent_at ASC
        """
    ).fetchall()
    for event in rows:
        for horizon_label in horizons:
            exists = conn.execute(
                "SELECT 1 FROM alert_event_performance WHERE event_id = ? AND horizon_label = ?",
                (event["id"], horizon_label),
            ).fetchone()
            if exists:
                continue
            try:
                perf = evaluate_event(event, horizon_label, timeout)
                if perf is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO alert_event_performance (
                        event_id, horizon_label, window_start, window_end, bars, close_price,
                        close_return_pct, max_runup_pct, max_drawdown_pct, tp1_hit, tp2_hit,
                        stop_loss_hit, evaluation_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["id"],
                        horizon_label,
                        perf["window_start"],
                        perf["window_end"],
                        perf["bars"],
                        perf["close_price"],
                        perf["close_return_pct"],
                        perf["max_runup_pct"],
                        perf["max_drawdown_pct"],
                        perf["tp1_hit"],
                        perf["tp2_hit"],
                        perf["stop_loss_hit"],
                        perf["evaluation_json"],
                        utc_now().isoformat(),
                    ),
                )
                inserted += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(
                    f"[{utc_now().isoformat()}] backfill_error event_id={event['id']} "
                    f"horizon={horizon_label} error={exc}"
                )
    conn.commit()
    return inserted, errors


def fetch_report_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    raw_rows = conn.execute(
        """
        SELECT
            p.horizon_label,
            e.symbol,
            e.event_type,
            e.direction,
            e.short_term_signal_json,
            e.protections_json,
            p.close_return_pct,
            p.max_runup_pct,
            p.max_drawdown_pct,
            p.tp1_hit,
            p.tp2_hit,
            p.stop_loss_hit
        FROM alert_event_performance p
        JOIN alert_events e ON e.id = p.event_id
        ORDER BY p.horizon_label, e.symbol, e.event_type
        """
    ).fetchall()
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        try:
            short_term_signal = json.loads(row["short_term_signal_json"] or "{}")
        except Exception:
            short_term_signal = {}
        try:
            protections = json.loads(row["protections_json"] or "{}")
        except Exception:
            protections = {}
        rows.append(
            {
                "horizon_label": row["horizon_label"],
                "symbol": row["symbol"],
                "event_type": row["event_type"],
                "direction": row["direction"],
                "market_regime": short_term_signal.get("market_regime", "unknown"),
                "protection_status": protections.get("status", "unknown"),
                "protection_active": bool(protections.get("active", False)),
                "close_return_pct": row["close_return_pct"],
                "max_runup_pct": row["max_runup_pct"],
                "max_drawdown_pct": row["max_drawdown_pct"],
                "tp1_hit": row["tp1_hit"],
                "tp2_hit": row["tp2_hit"],
                "stop_loss_hit": row["stop_loss_hit"],
            }
        )
    return rows


def pct(num: float) -> str:
    return f"{num:.1f}%"


def summarize_group(rows: list[dict[str, Any]]) -> dict[str, float]:
    count = len(rows)
    return {
        "count": count,
        "avg_close_return_pct": statistics.fmean(r["close_return_pct"] for r in rows) if rows else 0.0,
        "avg_max_runup_pct": statistics.fmean(r["max_runup_pct"] for r in rows) if rows else 0.0,
        "avg_max_drawdown_pct": statistics.fmean(r["max_drawdown_pct"] for r in rows) if rows else 0.0,
        "tp1_rate": (sum(r["tp1_hit"] for r in rows) / count * 100) if rows else 0.0,
        "tp2_rate": (sum(r["tp2_hit"] for r in rows) / count * 100) if rows else 0.0,
        "stop_rate": (sum(r["stop_loss_hit"] for r in rows) / count * 100) if rows else 0.0,
        "positive_close_rate": (sum(1 for r in rows if r["close_return_pct"] > 0) / count * 100) if rows else 0.0,
    }


def append_ranked_section(
    lines: list[str],
    title: str,
    rows: list[dict[str, Any]],
    key_builder: Callable[[dict[str, Any]], str] | None = None,
) -> None:
    lines.append(title)
    if not rows:
        lines.append("- 尚無資料。")
        lines.append("")
        return

    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = key_builder(row) if key_builder else row["event_type"]
        by_type.setdefault(str(key), []).append(row)
    ranked_types = sorted(
        ((name, summarize_group(group)) for name, group in by_type.items()),
        key=lambda item: (item[1]["avg_close_return_pct"], item[1]["tp1_rate"], item[1]["positive_close_rate"]),
        reverse=True,
    )
    for name, summary in ranked_types:
        role_label = event_role(name) if not key_builder else "bucket"
        lines.append(
            f"- `{name}` ({role_label}): 樣本 `{summary['count']}`，正報酬率 `{pct(summary['positive_close_rate'])}`，"
            f"平均收盤報酬 `{summary['avg_close_return_pct']:.3f}%`，"
            f"TP1 `{pct(summary['tp1_rate'])}`，TP2 `{pct(summary['tp2_rate'])}`，"
            f"SL `{pct(summary['stop_rate'])}`"
        )
    lines.append("")


def append_actionable_breakdown(lines: list[str], rows: list[dict[str, Any]]) -> None:
    append_ranked_section(
        lines,
        "### 可交易事件拆分（方向 x 市場狀態）",
        rows,
        key_builder=lambda row: f"{row['direction']} | {row['market_regime']}",
    )
    append_ranked_section(
        lines,
        "### 可交易事件拆分（幣種 x 市場狀態 x 方向）",
        rows,
        key_builder=lambda row: f"{row['symbol']} | {row['market_regime']} | {row['direction']}",
    )
    append_ranked_section(
        lines,
        "### 可交易事件拆分（方向 x 市場狀態 x 保護層）",
        rows,
        key_builder=lambda row: f"{row['direction']} | {row['market_regime']} | protection={row['protection_status']}",
    )


def build_report(conn: sqlite3.Connection, report_path: Path, horizons: list[str]) -> None:
    rows = fetch_report_rows(conn)
    lines: list[str] = []
    lines.append("# 提醒事件成效報告")
    lines.append("")
    lines.append(f"- 生成時間（UTC）: `{utc_now().isoformat()}`")
    total_events = conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0]
    total_perf = conn.execute("SELECT COUNT(*) FROM alert_event_performance").fetchone()[0]
    total_watch = conn.execute(
        "SELECT COUNT(*) FROM alert_events WHERE event_type IN ('approach_up', 'approach_down')"
    ).fetchone()[0]
    total_protected = conn.execute(
        "SELECT COUNT(*) FROM alert_events WHERE COALESCE(protections_json, '{}') NOT IN ('{}', '')"
    ).fetchone()[0]
    lines.append(f"- 提醒事件總數: `{total_events}`")
    lines.append(f"- 已完成成效回填筆數: `{total_perf}`")
    lines.append(f"- watch-only 觀察事件: `{total_watch}`")
    lines.append(f"- 非 watch-only 事件: `{max(total_events - total_watch, 0)}`")
    lines.append(f"- 帶保護層 metadata 的事件: `{total_protected}`")
    lines.append("")

    if not rows:
        lines.append("目前還沒有足夠的已到期提醒事件可分析。")
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    for horizon in horizons:
        h_rows = [r for r in rows if r["horizon_label"] == horizon]
        lines.append(f"## {horizon}")
        lines.append("")
        if not h_rows:
            lines.append("尚無資料。")
            lines.append("")
            continue

        actionable_rows = [row for row in h_rows if not is_watch_only_event(row["event_type"])]
        watch_rows = [row for row in h_rows if is_watch_only_event(row["event_type"])]
        lines.append(
            f"- 可交易/確認事件 `{len(actionable_rows)}` 筆；watch-only 觀察事件 `{len(watch_rows)}` 筆。"
        )
        lines.append("")
        append_ranked_section(lines, "### 哪類可交易事件最值得看", actionable_rows)
        append_actionable_breakdown(lines, actionable_rows)
        append_ranked_section(lines, "### 觀察事件（watch-only，和可掛單事件分開看）", watch_rows)

        by_symbol = {}
        for row in actionable_rows:
            by_symbol.setdefault(row["symbol"], []).append(row)
        ranked_symbols = sorted(
            ((name, summarize_group(group)) for name, group in by_symbol.items()),
            key=lambda item: (item[1]["avg_close_return_pct"], item[1]["tp1_rate"], item[1]["positive_close_rate"]),
            reverse=True,
        )
        lines.append("### 哪個幣最值得盯")
        for name, summary in ranked_symbols:
            lines.append(
                f"- `{name}`: 樣本 `{summary['count']}`，平均收盤報酬 `{summary['avg_close_return_pct']:.3f}%`，"
                f"平均最大順向 `{summary['avg_max_runup_pct']:.3f}%`，平均最大逆向 `{summary['avg_max_drawdown_pct']:.3f}%`，"
                f"TP1 `{pct(summary['tp1_rate'])}`，SL `{pct(summary['stop_rate'])}`"
            )
        lines.append("")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    horizons = [h.strip() for h in args.horizons.split(",") if h.strip()]
    db_path = Path(args.db_path)
    report_path = Path(args.report_md)
    analytics_dir = Path(args.analytics_dir)
    analytics_db = Path(args.analytics_db)
    probability_report_path = Path(args.probability_report_md)
    tuning_report_path = Path(args.tuning_report_md)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    migrated = migrate_legacy_alerts(conn)
    try:
        while True:
            try:
                inserted, errors = backfill(conn, horizons, args.timeout)
                build_report(conn, report_path, horizons)
                store_summary = export_market_sample_store(conn, analytics_dir, analytics_db)
                build_conditional_probability_report(
                    analytics_dir,
                    analytics_db,
                    probability_report_path,
                    min_samples=args.probability_min_samples,
                )
                build_parameter_tuning_report(
                    analytics_dir,
                    tuning_report_path,
                    min_samples=args.tuning_min_samples,
                )
                print(
                    f"[{utc_now().isoformat()}] performance cycle complete, "
                    f"migrated={migrated}, inserted={inserted}, errors={errors}, report={report_path}, "
                    f"analytics_context={store_summary['market_context_rows']}, "
                    f"analytics_outcomes={store_summary['event_outcome_rows']}, "
                    f"probability_report={probability_report_path}, "
                    f"tuning_report={tuning_report_path}"
                )
                migrated = 0
                if args.loop_minutes <= 0:
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"[{utc_now().isoformat()}] performance_cycle_error error={exc}")
                if args.loop_minutes <= 0:
                    raise
            time.sleep(args.loop_minutes * 60)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
