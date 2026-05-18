#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

import requests

from event_types import DEFAULT_ELIGIBLE_EVENTS, DEFAULT_EVENT_RISK_MULTIPLIERS, event_direction


USER_AGENT = "paper-order-engine/1.0"
BINANCE_TICKER_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulated order engine for check_price.")
    parser.add_argument("--source-db", default="alert_state.db")
    parser.add_argument("--paper-db", default="paper_trading.db")
    parser.add_argument("--report-md", default="paper_trading_report.md")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--once", action="store_true")
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


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value)


def fmt_price(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):.4f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def _json_loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def resolve_event_risk_pct(
    event_type: str,
    base_risk_pct: float,
    multipliers: Mapping[str, Any] | None = None,
) -> float:
    multiplier_map = dict(DEFAULT_EVENT_RISK_MULTIPLIERS)
    if multipliers:
        for key, value in multipliers.items():
            try:
                multiplier_map[str(key)] = float(value)
            except Exception:
                continue
    multiplier = float(multiplier_map.get(event_type, 1.0))
    resolved = float(base_risk_pct) * multiplier
    return max(0.05, round(resolved, 4))


def ensure_source_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_paper_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sim_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_event_id INTEGER NOT NULL UNIQUE,
            source_event_key TEXT NOT NULL,
            symbol TEXT NOT NULL,
            event_type TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            status TEXT NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            risk_reward_ratio REAL NOT NULL,
            risk_pct REAL NOT NULL,
            created_at TEXT NOT NULL,
            cancel_after_ts TEXT NOT NULL,
            filled_at TEXT,
            closed_at TEXT,
            exit_price REAL,
            exit_reason TEXT,
            last_price REAL,
            notes_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sim_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            fill_ts TEXT NOT NULL,
            fill_price REAL NOT NULL,
            fill_reason TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES sim_orders(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sim_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            exit_price REAL,
            exit_reason TEXT,
            pnl_pct REAL,
            realized_r REAL,
            mfe_pct REAL NOT NULL DEFAULT 0,
            mae_pct REAL NOT NULL DEFAULT 0,
            last_price REAL NOT NULL,
            risk_pct REAL NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES sim_orders(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sim_equity_curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            equity REAL NOT NULL,
            open_positions INTEGER NOT NULL,
            closed_trades INTEGER NOT NULL,
            notes TEXT
        )
        """
    )
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(sim_positions)").fetchall()
    }
    extra_columns = {
        "gross_pnl_pct": "ALTER TABLE sim_positions ADD COLUMN gross_pnl_pct REAL NOT NULL DEFAULT 0",
        "net_pnl_pct": "ALTER TABLE sim_positions ADD COLUMN net_pnl_pct REAL NOT NULL DEFAULT 0",
        "gross_realized_r": "ALTER TABLE sim_positions ADD COLUMN gross_realized_r REAL NOT NULL DEFAULT 0",
        "entry_fee_pct": "ALTER TABLE sim_positions ADD COLUMN entry_fee_pct REAL NOT NULL DEFAULT 0",
        "exit_fee_pct": "ALTER TABLE sim_positions ADD COLUMN exit_fee_pct REAL NOT NULL DEFAULT 0",
        "account_return_pct": "ALTER TABLE sim_positions ADD COLUMN account_return_pct REAL NOT NULL DEFAULT 0",
    }
    for column_name, ddl in extra_columns.items():
        if column_name not in existing_columns:
            conn.execute(ddl)
    conn.commit()
    return conn


def ensure_initial_equity_snapshot(conn: sqlite3.Connection, starting_equity: float) -> None:
    row = conn.execute("SELECT 1 FROM sim_equity_curve ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        return
    conn.execute(
        """
        INSERT INTO sim_equity_curve (ts, equity, open_positions, closed_trades, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (utc_now().isoformat(), float(starting_equity), 0, 0, "initial_balance"),
    )
    conn.commit()


def normalize_order_type(raw_value: str | None) -> str:
    value = str(raw_value or "").lower().strip()
    if "limit" in value:
        return "limit_entry"
    return "stop_entry"


def _relevant_setup(event: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    direction = str(event.get("direction") or event_direction(str(event.get("event_type", ""))))
    plan = _json_loads(event.get("long_short_plan_json"))
    protections = _json_loads(event.get("protections_json"))
    if direction == "up":
        return "long", plan.get("long_setup", {}), plan, protections
    if direction == "down":
        return "short", plan.get("short_setup", {}), plan, protections
    return "neutral", {}, plan, protections


def build_sim_order_from_event(
    event: Mapping[str, Any],
    rr_ratio: float,
    risk_pct: float,
    default_cancel_after_minutes: int,
    mode: str,
    now: dt.datetime | None = None,
    risk_pct_multipliers: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    event = dict(event)
    now = now or utc_now()
    side, setup, plan, protections = _relevant_setup(event)
    if side not in {"long", "short"} or not setup:
        return None
    if protections.get("hard_block"):
        return None
    if side == "long" and protections.get("pause_long"):
        return None
    if side == "short" and protections.get("pause_short"):
        return None

    executor = setup.get("executor_plan", {})
    if str(executor.get("quality", "")).lower() == "observe_only":
        return None

    entry_price = float(executor.get("entry_trigger") or setup.get("trigger_price") or event.get("level") or 0.0)
    stop_loss = float(setup.get("stop_loss") or event.get("stop_loss") or 0.0)
    if entry_price <= 0 or stop_loss <= 0 or entry_price == stop_loss:
        return None

    risk_abs = abs(entry_price - stop_loss)
    if mode == "plan_based":
        plan_tp = (setup.get("take_profit") or [None])[0]
        take_profit = float(plan_tp) if plan_tp else entry_price + (risk_abs * rr_ratio if side == "long" else -risk_abs * rr_ratio)
    else:
        take_profit = entry_price + risk_abs * rr_ratio if side == "long" else entry_price - risk_abs * rr_ratio

    cancel_after_minutes = int(executor.get("cancel_after_minutes") or default_cancel_after_minutes)
    effective_risk_pct = resolve_event_risk_pct(str(event.get("event_type") or ""), float(risk_pct), risk_pct_multipliers)
    notes = {
        "source_event_sent_at": event.get("sent_at"),
        "executor_quality": executor.get("quality"),
        "execution_readiness": plan.get("execution_readiness"),
        "preferred_setup": plan.get("preferred_setup"),
        "analysis_bias": plan.get("analysis_bias"),
        "protection_status": protections.get("status", "unknown"),
        "rr_mode": mode,
        "base_risk_pct": float(risk_pct),
        "effective_risk_pct": effective_risk_pct,
        "risk_multiplier": round(effective_risk_pct / float(risk_pct), 4) if float(risk_pct) else 0.0,
    }
    return {
        "source_event_id": int(event["id"]),
        "source_event_key": str(event["event_key"]),
        "symbol": str(event["symbol"]),
        "event_type": str(event["event_type"]),
        "side": side,
        "order_type": normalize_order_type(executor.get("order_type")),
        "status": "pending",
        "entry_price": round(entry_price, 8),
        "stop_loss": round(stop_loss, 8),
        "take_profit": round(float(take_profit), 8),
        "risk_reward_ratio": float(rr_ratio),
        "risk_pct": effective_risk_pct,
        "created_at": now.isoformat(),
        "cancel_after_ts": (now + dt.timedelta(minutes=cancel_after_minutes)).isoformat(),
        "filled_at": None,
        "closed_at": None,
        "exit_price": None,
        "exit_reason": None,
        "last_price": float(event.get("last_price") or event.get("level") or entry_price),
        "notes_json": json.dumps(notes, ensure_ascii=False),
    }


def insert_sim_order(conn: sqlite3.Connection, order: Mapping[str, Any]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO sim_orders (
            source_event_id, source_event_key, symbol, event_type, side, order_type, status,
            entry_price, stop_loss, take_profit, risk_reward_ratio, risk_pct,
            created_at, cancel_after_ts, filled_at, closed_at, exit_price, exit_reason, last_price, notes_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order["source_event_id"],
            order["source_event_key"],
            order["symbol"],
            order["event_type"],
            order["side"],
            order["order_type"],
            order["status"],
            order["entry_price"],
            order["stop_loss"],
            order["take_profit"],
            order["risk_reward_ratio"],
            order["risk_pct"],
            order["created_at"],
            order["cancel_after_ts"],
            order["filled_at"],
            order["closed_at"],
            order["exit_price"],
            order["exit_reason"],
            order["last_price"],
            order["notes_json"],
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def fetch_new_source_events(
    source_conn: sqlite3.Connection,
    paper_conn: sqlite3.Connection,
    eligible_events: list[str],
) -> list[sqlite3.Row]:
    existing_ids = {
        int(row["source_event_id"])
        for row in paper_conn.execute("SELECT source_event_id FROM sim_orders").fetchall()
    }
    placeholders = ",".join("?" for _ in eligible_events)
    query = f"""
        SELECT *
        FROM alert_events
        WHERE event_type IN ({placeholders})
        ORDER BY sent_at ASC, id ASC
    """
    rows = source_conn.execute(query, eligible_events).fetchall()
    return [row for row in rows if int(row["id"]) not in existing_ids]


def sync_orders_from_source(
    source_conn: sqlite3.Connection,
    paper_conn: sqlite3.Connection,
    eligible_events: list[str],
    rr_ratio: float,
    risk_pct: float,
    default_cancel_after_minutes: int,
    mode: str,
    *,
    now: dt.datetime,
    starting_equity: float,
    max_open_positions: int,
    max_same_side_positions: int,
    max_symbol_positions: int,
    daily_loss_limit_pct: float,
    drawdown_halt_pct: float,
    risk_pct_multipliers: Mapping[str, Any] | None = None,
) -> int:
    inserted = 0
    for event in fetch_new_source_events(source_conn, paper_conn, eligible_events):
        order = build_sim_order_from_event(
            event,
            rr_ratio,
            risk_pct,
            default_cancel_after_minutes,
            mode,
            risk_pct_multipliers=risk_pct_multipliers,
        )
        if not order:
            continue
        allowed, reason = can_create_order(
            paper_conn,
            side=str(order["side"]),
            symbol=str(order["symbol"]),
            now=now,
            starting_equity=starting_equity,
            max_open_positions=max_open_positions,
            max_same_side_positions=max_same_side_positions,
            max_symbol_positions=max_symbol_positions,
            daily_loss_limit_pct=daily_loss_limit_pct,
            drawdown_halt_pct=drawdown_halt_pct,
        )
        if not allowed:
            notes = _json_loads(order.get("notes_json"))
            notes["blocked_reason"] = reason
            order["notes_json"] = json.dumps(notes, ensure_ascii=False)
            continue
        insert_sim_order(paper_conn, order)
        inserted += 1
    return inserted


def fetch_symbol_price(symbol: str, timeout: int) -> float:
    response = requests.get(
        BINANCE_TICKER_PRICE_URL,
        params={"symbol": symbol},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return float(payload["price"])


def fetch_prices(symbols: list[str], timeout: int) -> dict[str, float]:
    prices: dict[str, float] = {}
    for symbol in sorted(set(symbols)):
        prices[symbol] = fetch_symbol_price(symbol, timeout)
    return prices


def _long_fill_condition(order_type: str, current_price: float, entry_price: float) -> bool:
    if order_type == "limit_entry":
        return current_price <= entry_price
    return current_price >= entry_price


def _short_fill_condition(order_type: str, current_price: float, entry_price: float) -> bool:
    if order_type == "limit_entry":
        return current_price >= entry_price
    return current_price <= entry_price


def _update_order_status(
    conn: sqlite3.Connection,
    order_id: int,
    status: str,
    now: dt.datetime,
    last_price: float,
    *,
    filled_at: str | None = None,
    closed_at: str | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE sim_orders
        SET status = ?,
            filled_at = COALESCE(?, filled_at),
            closed_at = COALESCE(?, closed_at),
            exit_price = COALESCE(?, exit_price),
            exit_reason = COALESCE(?, exit_reason),
            last_price = ?
        WHERE id = ?
        """,
        (status, filled_at, closed_at, exit_price, exit_reason, last_price, order_id),
    )


def _insert_fill(conn: sqlite3.Connection, order_id: int, now: dt.datetime, fill_price: float, fill_reason: str) -> None:
    conn.execute(
        """
        INSERT INTO sim_fills (order_id, fill_ts, fill_price, fill_reason)
        VALUES (?, ?, ?, ?)
        """,
        (order_id, now.isoformat(), fill_price, fill_reason),
    )


def _open_position(conn: sqlite3.Connection, order: sqlite3.Row, now: dt.datetime, fill_price: float) -> None:
    conn.execute(
        """
        INSERT INTO sim_positions (
            order_id, symbol, side, status, opened_at, closed_at, entry_price, stop_loss, take_profit,
            exit_price, exit_reason, pnl_pct, realized_r, mfe_pct, mae_pct, last_price, risk_pct, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order["id"],
            order["symbol"],
            order["side"],
            "open",
            now.isoformat(),
            None,
            fill_price,
            order["stop_loss"],
            order["take_profit"],
            None,
            None,
            None,
            None,
            0.0,
            0.0,
            fill_price,
            order["risk_pct"],
            now.isoformat(),
        ),
    )


def _close_position(
    conn: sqlite3.Connection,
    position: sqlite3.Row,
    now: dt.datetime,
    exit_price: float,
    exit_reason: str,
) -> None:
    side = str(position["side"])
    entry_price = float(position["entry_price"])
    stop_loss = float(position["stop_loss"])
    risk_abs = abs(entry_price - stop_loss)
    if side == "long":
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
        realized_r = (exit_price - entry_price) / risk_abs if risk_abs else 0.0
    else:
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0
        realized_r = (entry_price - exit_price) / risk_abs if risk_abs else 0.0
    conn.execute(
        """
        UPDATE sim_positions
        SET status = 'closed',
            closed_at = ?,
            exit_price = ?,
            exit_reason = ?,
            pnl_pct = ?,
            realized_r = ?,
            last_price = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            now.isoformat(),
            exit_price,
            exit_reason,
            pnl_pct,
            realized_r,
            exit_price,
            now.isoformat(),
            position["id"],
        ),
    )


def _update_open_position_metrics(conn: sqlite3.Connection, position: sqlite3.Row, current_price: float, now: dt.datetime) -> None:
    side = str(position["side"])
    entry_price = float(position["entry_price"])
    current_return_pct = ((current_price - entry_price) / entry_price) * 100.0 if side == "long" else ((entry_price - current_price) / entry_price) * 100.0
    mfe_pct = max(float(position["mfe_pct"]), current_return_pct)
    mae_pct = min(float(position["mae_pct"]), current_return_pct)
    conn.execute(
        """
        UPDATE sim_positions
        SET mfe_pct = ?,
            mae_pct = ?,
            last_price = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (mfe_pct, mae_pct, current_price, now.isoformat(), position["id"]),
    )


def _current_equity(conn: sqlite3.Connection, starting_equity: float) -> float:
    row = conn.execute("SELECT equity FROM sim_equity_curve ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return float(starting_equity)
    return float(row["equity"])


def _record_equity_snapshot(conn: sqlite3.Connection, now: dt.datetime, equity: float, notes: str) -> None:
    open_positions = int(conn.execute("SELECT COUNT(*) FROM sim_positions WHERE status = 'open'").fetchone()[0])
    closed_trades = int(conn.execute("SELECT COUNT(*) FROM sim_positions WHERE status = 'closed'").fetchone()[0])
    conn.execute(
        """
        INSERT INTO sim_equity_curve (ts, equity, open_positions, closed_trades, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (now.isoformat(), equity, open_positions, closed_trades, notes),
    )


def bps_to_pct(bps: float) -> float:
    return float(bps) / 100.0


def apply_slippage_price(reference_price: float, side: str, stage: str, slippage_bps: float) -> float:
    slippage_mult = 1.0 + (float(slippage_bps) / 10_000.0)
    if side == "long":
        if stage == "entry":
            return reference_price * slippage_mult
        return reference_price / slippage_mult
    if stage == "entry":
        return reference_price / slippage_mult
    return reference_price * slippage_mult


def current_drawdown_pct(conn: sqlite3.Connection, starting_equity: float) -> float:
    row = conn.execute(
        """
        SELECT
            MAX(equity) AS peak_equity,
            (SELECT equity FROM sim_equity_curve ORDER BY id DESC LIMIT 1) AS current_equity
        FROM sim_equity_curve
        """
    ).fetchone()
    peak = float(row["peak_equity"] or starting_equity)
    current = float(row["current_equity"] or starting_equity)
    if peak <= 0:
        return 0.0
    return max(0.0, (1.0 - (current / peak)) * 100.0)


def daily_account_return_pct(conn: sqlite3.Connection, now: dt.datetime) -> float:
    day_start = now.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(account_return_pct), 0) AS total
        FROM sim_positions
        WHERE status = 'closed' AND closed_at >= ?
        """,
        (day_start,),
    ).fetchone()
    return float(row["total"] or 0.0)


def can_create_order(
    conn: sqlite3.Connection,
    *,
    side: str,
    symbol: str,
    now: dt.datetime,
    starting_equity: float,
    max_open_positions: int,
    max_same_side_positions: int,
    max_symbol_positions: int,
    daily_loss_limit_pct: float,
    drawdown_halt_pct: float,
) -> tuple[bool, str | None]:
    total_open = int(conn.execute("SELECT COUNT(*) FROM sim_positions WHERE status = 'open'").fetchone()[0])
    if max_open_positions > 0 and total_open >= max_open_positions:
        return False, "max_open_positions"
    same_side_open = int(
        conn.execute("SELECT COUNT(*) FROM sim_positions WHERE status = 'open' AND side = ?", (side,)).fetchone()[0]
    )
    if max_same_side_positions > 0 and same_side_open >= max_same_side_positions:
        return False, "max_same_side_positions"
    symbol_open = int(
        conn.execute("SELECT COUNT(*) FROM sim_positions WHERE status = 'open' AND symbol = ?", (symbol,)).fetchone()[0]
    )
    if max_symbol_positions > 0 and symbol_open >= max_symbol_positions:
        return False, "max_symbol_positions"
    if daily_loss_limit_pct > 0 and daily_account_return_pct(conn, now) <= -abs(float(daily_loss_limit_pct)):
        return False, "daily_loss_limit"
    if drawdown_halt_pct > 0 and current_drawdown_pct(conn, starting_equity) >= abs(float(drawdown_halt_pct)):
        return False, "drawdown_halt"
    return True, None


def process_pending_orders(
    conn: sqlite3.Connection,
    prices: Mapping[str, float],
    now: dt.datetime,
    *,
    entry_slippage_bps: float = 0.0,
) -> dict[str, int]:
    fills = 0
    cancels = 0
    for order in conn.execute("SELECT * FROM sim_orders WHERE status = 'pending' ORDER BY created_at ASC, id ASC").fetchall():
        current_price = float(prices.get(order["symbol"], order["last_price"] or order["entry_price"]))
        cancel_after = parse_iso_datetime(order["cancel_after_ts"])
        if cancel_after and now >= cancel_after:
            _update_order_status(conn, int(order["id"]), "canceled", now, current_price, closed_at=now.isoformat(), exit_reason="timeout")
            cancels += 1
            continue
        should_fill = _long_fill_condition(str(order["order_type"]), current_price, float(order["entry_price"])) if order["side"] == "long" else _short_fill_condition(str(order["order_type"]), current_price, float(order["entry_price"]))
        if not should_fill:
            _update_order_status(conn, int(order["id"]), "pending", now, current_price)
            continue
        fill_price = apply_slippage_price(float(order["entry_price"]), str(order["side"]), "entry", entry_slippage_bps)
        _insert_fill(conn, int(order["id"]), now, fill_price, "entry_fill")
        _open_position(conn, order, now, fill_price)
        _update_order_status(conn, int(order["id"]), "filled", now, current_price, filled_at=now.isoformat())
        fills += 1
    conn.commit()
    return {"fills": fills, "cancels": cancels}


def process_open_positions(
    conn: sqlite3.Connection,
    prices: Mapping[str, float],
    now: dt.datetime,
    starting_equity: float,
    *,
    fee_bps: float = 0.0,
    stop_slippage_bps: float = 0.0,
    take_profit_slippage_bps: float = 0.0,
) -> dict[str, int]:
    closed = 0
    for position in conn.execute("SELECT * FROM sim_positions WHERE status = 'open' ORDER BY opened_at ASC, id ASC").fetchall():
        current_price = float(prices.get(position["symbol"], position["last_price"] or position["entry_price"]))
        _update_open_position_metrics(conn, position, current_price, now)
        side = str(position["side"])
        stop_loss = float(position["stop_loss"])
        take_profit = float(position["take_profit"])
        exit_reason: str | None = None
        exit_price: float | None = None
        if side == "long":
            if current_price <= stop_loss:
                exit_reason = "stop_loss"
                exit_price = stop_loss
            elif current_price >= take_profit:
                exit_reason = "take_profit"
                exit_price = take_profit
        else:
            if current_price >= stop_loss:
                exit_reason = "stop_loss"
                exit_price = stop_loss
            elif current_price <= take_profit:
                exit_reason = "take_profit"
                exit_price = take_profit
        if exit_reason is None or exit_price is None:
            continue
        exit_slippage_bps = stop_slippage_bps if exit_reason == "stop_loss" else take_profit_slippage_bps
        adjusted_exit_price = apply_slippage_price(exit_price, side, "exit", exit_slippage_bps)
        _close_position(conn, position, now, adjusted_exit_price, exit_reason)
        _update_order_status(
            conn,
            int(position["order_id"]),
            "closed",
            now,
            current_price,
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


def build_paper_report(conn: sqlite3.Connection, output_path: Path) -> None:
    total_orders = int(conn.execute("SELECT COUNT(*) FROM sim_orders").fetchone()[0])
    pending = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status = 'pending'").fetchone()[0])
    filled = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status IN ('filled','closed')").fetchone()[0])
    closed = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status = 'closed'").fetchone()[0])
    canceled = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status = 'canceled'").fetchone()[0])
    equity_row = conn.execute("SELECT equity FROM sim_equity_curve ORDER BY id DESC LIMIT 1").fetchone()
    equity = float(equity_row["equity"]) if equity_row else 0.0
    trade_stats = conn.execute(
        """
        SELECT
            COUNT(*) AS n,
            AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0 AS win_rate,
            AVG(pnl_pct) AS avg_pnl_pct,
            AVG(realized_r) AS avg_realized_r
        FROM sim_positions
        WHERE status = 'closed'
        """
    ).fetchone()
    lines = [
        "# 模擬下單報告",
        "",
        f"- 生成時間: `{utc_now().isoformat()}`",
        f"- 總訂單數: `{total_orders}`",
        f"- 待成交: `{pending}`",
        f"- 已成交: `{filled}`",
        f"- 已平倉: `{closed}`",
        f"- 已取消: `{canceled}`",
        f"- 最新權益: `{equity:.2f}`",
        "",
    ]
    if trade_stats and trade_stats["n"]:
        lines.extend(
            [
                "## 已平倉統計",
                "",
                f"- 樣本數: `{int(trade_stats['n'])}`",
                f"- 勝率: `{float(trade_stats['win_rate'] or 0.0):.1f}%`",
                f"- 平均損益: `{float(trade_stats['avg_pnl_pct'] or 0.0):.3f}%`",
                f"- 平均 R 值: `{float(trade_stats['avg_realized_r'] or 0.0):.3f}`",
                "",
            ]
        )
    lines.append("## 事件類型表現")
    lines.append("")
    for row in conn.execute(
        """
        SELECT
            event_type,
            COUNT(*) AS n,
            AVG(CASE WHEN status = 'closed' THEN 1.0 ELSE 0.0 END) * 100.0 AS close_rate
        FROM sim_orders
        GROUP BY event_type
        ORDER BY n DESC, event_type ASC
        """
    ).fetchall():
        lines.append(f"- `{row['event_type']}`: 訂單 `{int(row['n'])}`，平倉率 `{float(row['close_rate'] or 0.0):.1f}%`")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_paper_report(conn: sqlite3.Connection, output_path: Path) -> None:
    total_orders = int(conn.execute("SELECT COUNT(*) FROM sim_orders").fetchone()[0])
    pending = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status = 'pending'").fetchone()[0])
    filled = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status IN ('filled','closed')").fetchone()[0])
    closed = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status = 'closed'").fetchone()[0])
    canceled = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status = 'canceled'").fetchone()[0])
    equity_row = conn.execute("SELECT equity FROM sim_equity_curve ORDER BY id DESC LIMIT 1").fetchone()
    equity = float(equity_row["equity"]) if equity_row else 0.0
    trade_stats = conn.execute(
        """
        SELECT
            COUNT(*) AS n,
            AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0 AS win_rate,
            AVG(pnl_pct) AS avg_pnl_pct,
            AVG(realized_r) AS avg_realized_r
        FROM sim_positions
        WHERE status = 'closed'
        """
    ).fetchone()
    lines = [
        "# 模擬下單報告",
        "",
        f"- 產生時間: `{utc_now().isoformat()}`",
        f"- 總訂單數: `{total_orders}`",
        f"- 待成交: `{pending}`",
        f"- 已成交: `{filled}`",
        f"- 已平倉: `{closed}`",
        f"- 已取消: `{canceled}`",
        f"- 最新權益: `{equity:.2f}`",
        "",
    ]
    if trade_stats and trade_stats["n"]:
        lines.extend(
            [
                "## 已平倉統計",
                "",
                f"- 樣本數: `{int(trade_stats['n'])}`",
                f"- 勝率: `{float(trade_stats['win_rate'] or 0.0):.1f}%`",
                f"- 平均報酬率: `{float(trade_stats['avg_pnl_pct'] or 0.0):.3f}%`",
                f"- 平均實現 R: `{float(trade_stats['avg_realized_r'] or 0.0):.3f}`",
                "",
            ]
        )
    lines.append("## 事件型別統計")
    lines.append("")
    for row in conn.execute(
        """
        SELECT
            event_type,
            COUNT(*) AS n,
            AVG(CASE WHEN status = 'closed' THEN 1.0 ELSE 0.0 END) * 100.0 AS close_rate
        FROM sim_orders
        GROUP BY event_type
        ORDER BY n DESC, event_type ASC
        """
    ).fetchall():
        lines.append(f"- `{row['event_type']}`: 樣本 `{int(row['n'])}`，平倉率 `{float(row['close_rate'] or 0.0):.1f}%`")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_clean_paper_report(conn: sqlite3.Connection, output_path: Path) -> None:
    total_orders = int(conn.execute("SELECT COUNT(*) FROM sim_orders").fetchone()[0])
    pending = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status = 'pending'").fetchone()[0])
    filled = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status IN ('filled', 'closed')").fetchone()[0])
    closed = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status = 'closed'").fetchone()[0])
    canceled = int(conn.execute("SELECT COUNT(*) FROM sim_orders WHERE status = 'canceled'").fetchone()[0])
    equity_row = conn.execute("SELECT equity FROM sim_equity_curve ORDER BY id DESC LIMIT 1").fetchone()
    equity = float(equity_row["equity"]) if equity_row else 0.0
    trade_stats = conn.execute(
        """
        SELECT
            COUNT(*) AS n,
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
    drawdown_pct = current_drawdown_pct(conn, equity if equity > 0 else 10_000.0)
    lines = [
        "# Paper Trading Report",
        "",
        f"- Generated At: `{utc_now().isoformat()}`",
        f"- Total Orders: `{total_orders}`",
        f"- Pending: `{pending}`",
        f"- Filled: `{filled}`",
        f"- Closed: `{closed}`",
        f"- Canceled: `{canceled}`",
        f"- Latest Equity: `{equity:.2f}`",
        f"- Current Drawdown: `{drawdown_pct:.2f}%`",
        "",
    ]
    if trade_stats and trade_stats["n"]:
        lines.extend(
            [
                "## Closed Trade Stats",
                "",
                f"- Trades: `{int(trade_stats['n'])}`",
                f"- Win Rate: `{float(trade_stats['win_rate'] or 0.0):.1f}%`",
                f"- Avg PnL %: `{float(trade_stats['avg_pnl_pct'] or 0.0):.3f}%`",
                f"- Avg Realized R: `{float(trade_stats['avg_realized_r'] or 0.0):.3f}`",
                f"- Avg Gross PnL %: `{float(trade_stats['avg_gross_pnl_pct'] or 0.0):.3f}%`",
                f"- Avg Gross Realized R: `{float(trade_stats['avg_gross_realized_r'] or 0.0):.3f}`",
                f"- Total Fees %: `{float(trade_stats['total_fee_pct'] or 0.0):.3f}`",
                "",
            ]
        )
    lines.append("## Event Type Stats")
    lines.append("")
    for row in conn.execute(
        """
        SELECT
            event_type,
            COUNT(*) AS n,
            AVG(CASE WHEN status = 'closed' THEN 1.0 ELSE 0.0 END) * 100.0 AS close_rate
        FROM sim_orders
        GROUP BY event_type
        ORDER BY n DESC, event_type ASC
        """
    ).fetchall():
        lines.append(f"- `{row['event_type']}`: samples `{int(row['n'])}`, close rate `{float(row['close_rate'] or 0.0):.1f}%`")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


build_paper_report = render_clean_paper_report


def run_cycle(
    source_conn: sqlite3.Connection,
    paper_conn: sqlite3.Connection,
    eligible_events: list[str],
    rr_ratio: float,
    risk_pct: float,
    default_cancel_after_minutes: int,
    mode: str,
    timeout: int,
    starting_equity: float,
    fee_bps: float,
    entry_slippage_bps: float,
    stop_slippage_bps: float,
    take_profit_slippage_bps: float,
    max_open_positions: int,
    max_same_side_positions: int,
    max_symbol_positions: int,
    daily_loss_limit_pct: float,
    drawdown_halt_pct: float,
    risk_pct_multipliers: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    now = utc_now()
    created = sync_orders_from_source(
        source_conn,
        paper_conn,
        eligible_events,
        rr_ratio,
        risk_pct,
        default_cancel_after_minutes,
        mode,
        now=now,
        starting_equity=starting_equity,
        max_open_positions=max_open_positions,
        max_same_side_positions=max_same_side_positions,
        max_symbol_positions=max_symbol_positions,
        daily_loss_limit_pct=daily_loss_limit_pct,
        drawdown_halt_pct=drawdown_halt_pct,
        risk_pct_multipliers=risk_pct_multipliers,
    )
    symbols = [
        row["symbol"]
        for row in paper_conn.execute(
            "SELECT DISTINCT symbol FROM sim_orders WHERE status IN ('pending','filled')"
        ).fetchall()
    ]
    prices = fetch_prices(symbols, timeout) if symbols else {}
    pending_stats = process_pending_orders(paper_conn, prices, now, entry_slippage_bps=entry_slippage_bps)
    position_stats = process_open_positions(
        paper_conn,
        prices,
        now,
        starting_equity,
        fee_bps=fee_bps,
        stop_slippage_bps=stop_slippage_bps,
        take_profit_slippage_bps=take_profit_slippage_bps,
    )
    return {
        "created": created,
        "fills": pending_stats["fills"],
        "cancels": pending_stats["cancels"],
        "closed": position_stats["closed"],
    }


def main() -> None:
    args = parse_args()
    source_conn = ensure_source_conn(Path(args.source_db))
    paper_conn = ensure_paper_db(Path(args.paper_db))
    ensure_initial_equity_snapshot(paper_conn, args.starting_equity)
    eligible_events = [item.strip() for item in str(args.eligible_events).split(",") if item.strip()]
    try:
        while True:
            summary = run_cycle(
                source_conn=source_conn,
                paper_conn=paper_conn,
                eligible_events=eligible_events,
                rr_ratio=float(args.rr_ratio),
                risk_pct=float(args.risk_pct),
                default_cancel_after_minutes=int(args.cancel_after_minutes),
                mode=str(args.mode),
                timeout=int(args.timeout),
                starting_equity=float(args.starting_equity),
                fee_bps=float(args.fee_bps),
                entry_slippage_bps=float(args.entry_slippage_bps),
                stop_slippage_bps=float(args.stop_slippage_bps),
                take_profit_slippage_bps=float(args.take_profit_slippage_bps),
                max_open_positions=int(args.max_open_positions),
                max_same_side_positions=int(args.max_same_side_positions),
                max_symbol_positions=int(args.max_symbol_positions),
                daily_loss_limit_pct=float(args.daily_loss_limit_pct),
                drawdown_halt_pct=float(args.drawdown_halt_pct),
                risk_pct_multipliers=DEFAULT_EVENT_RISK_MULTIPLIERS,
            )
            build_paper_report(paper_conn, Path(args.report_md))
            print(json.dumps(summary, ensure_ascii=False))
            if args.once:
                break
            time.sleep(max(int(args.interval_seconds), 1))
    finally:
        paper_conn.close()
        source_conn.close()


if __name__ == "__main__":
    main()
