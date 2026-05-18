#!/usr/bin/env python3
import argparse
import copy
import datetime as dt
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from alert_delivery import resolve_env, send_telegram, telegram_api_get_updates
from event_types import event_direction, event_risk_multiplier, event_role
from market_trend_report import build_market_outlook, build_market_outlook_message, save_market_outlook
from protections import (
    evaluate_market_protections,
    evaluate_performance_protections,
    merge_protections,
    normalize_protection_config,
)
from shadow_mode import (
    analysis_to_dict,
    analyze_news_event_context,
    build_symbol_analysis,
    fetch_rss_items,
    normalize_symbol,
)


DEFAULT_TELEGRAM_SCRIPT = Path(r"C:\Users\User\.codex\skills\telegram-notify\scripts\send-telegram.ps1")
TAIPEI_TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Taipei")
TRADE_TICKET_EVENTS = {
    "effective_long_breakout",
    "effective_short_breakdown",
    "second_breakout_long",
    "second_breakdown_short",
}
DEFAULT_SESSION_LABELS: tuple[dict[str, str], ...] = (
    {"start": "08:00", "end": "15:59", "label": "亞盤"},
    {"start": "16:00", "end": "20:59", "label": "歐盤"},
    {"start": "21:00", "end": "23:59", "label": "美盤開盤"},
    {"start": "00:00", "end": "03:59", "label": "美盤後半"},
    {"start": "04:00", "end": "07:59", "label": "低量觀察"},
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="24/7 market alert daemon with Telegram notifications.")
    p.add_argument("--config", default="watchlist.json")
    p.add_argument("--state-db", default="alert_state.db")
    p.add_argument("--interval-seconds", type=int, default=60)
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--once", action="store_true")
    p.add_argument("--llama", choices=["off", "auto", "on"], default="off")
    p.add_argument("--llama-model", default="llama3.2:3b")
    p.add_argument("--quote", default="USDT")
    p.add_argument("--telegram-script", default=str(DEFAULT_TELEGRAM_SCRIPT))
    return p.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fmt_price(value: Any) -> str:
    if value in (None, "-"):
        return "-"
    try:
        return f"{float(value):.4f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def fmt_range(values: list[Any] | tuple[Any, Any]) -> str:
    if not values or len(values) < 2:
        return "-"
    return f"{fmt_price(values[0])} ~ {fmt_price(values[1])}"


def fmt_compact_range(values: list[Any] | tuple[Any, Any]) -> str:
    if not values or len(values) < 2:
        return "-"
    return f"{fmt_price(values[0])}-{fmt_price(values[1])}"


def fmt_compact_targets(values: list[Any]) -> str:
    targets = [fmt_price(value) for value in values if value not in (None, "-")]
    return "-".join(targets) if targets else "-"


def display_symbol(symbol: str) -> str:
    base = str(symbol).upper()
    for suffix in ("USDT", "USD", "PERP"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base[:1] + base[1:].lower() if base else str(symbol)


def resolve_display_timezone(name: str | None) -> dt.tzinfo:
    if str(name or "").lower() in {"utc", "z"}:
        return dt.timezone.utc
    return TAIPEI_TZ


def parse_session_minute(value: Any) -> int:
    hour_text, minute_text = str(value).split(":", 1)
    return int(hour_text) * 60 + int(minute_text)


def minute_in_session(now_minute: int, start_minute: int, end_minute: int) -> bool:
    if start_minute <= end_minute:
        return start_minute <= now_minute <= end_minute
    return now_minute >= start_minute or now_minute <= end_minute


def current_session_label(
    now: dt.datetime | None = None,
    presentation: dict[str, Any] | None = None,
) -> str:
    presentation = presentation or {}
    timezone_name = str(presentation.get("display_timezone", "Asia/Taipei"))
    local_now = (now or utc_now()).astimezone(resolve_display_timezone(timezone_name))
    now_minute = local_now.hour * 60 + local_now.minute
    session_labels = presentation.get("session_labels") or DEFAULT_SESSION_LABELS
    for session in session_labels:
        start_text = str(session.get("start", "00:00"))
        end_text = str(session.get("end", "23:59"))
        if minute_in_session(now_minute, parse_session_minute(start_text), parse_session_minute(end_text)):
            label = str(session.get("label", "交易時段"))
            prefix = "台灣時間" if timezone_name == "Asia/Taipei" else timezone_name
            return f"{prefix} {start_text}-{end_text}｜{label}"
    prefix = "台灣時間" if timezone_name == "Asia/Taipei" else timezone_name
    return f"{prefix} {local_now:%H:%M}｜未標記時段"


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_state_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            event_type TEXT NOT NULL,
            level REAL,
            last_sent_at TEXT NOT NULL,
            last_price REAL NOT NULL,
            message TEXT NOT NULL
        )
        """
    )
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
            UNIQUE(event_id, horizon_label),
            FOREIGN KEY(event_id) REFERENCES alert_events(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_states (
            symbol TEXT PRIMARY KEY,
            long_stage TEXT NOT NULL,
            short_stage TEXT NOT NULL,
            long_breakout_seen_at TEXT,
            short_breakout_seen_at TEXT,
            long_retest_seen_at TEXT,
            short_retest_seen_at TEXT,
            updated_at TEXT NOT NULL,
            state_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_alerts (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            last_sent_at TEXT NOT NULL,
            message TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
    if "protections_json" not in existing:
        conn.execute("ALTER TABLE alert_events ADD COLUMN protections_json TEXT")
    conn.commit()
    return conn


def load_runtime_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM telegram_runtime_state WHERE key = ?",
        (key,),
    ).fetchone()
    if not row:
        return None
    return str(row["value"])


def save_runtime_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO telegram_runtime_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, value, utc_now().isoformat()),
    )
    conn.commit()


def parse_telegram_command(text: str) -> tuple[str | None, list[str]]:
    first_line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    if not first_line:
        return None, []
    parts = first_line.split()
    command = parts[0].strip().lower().split("@")[0]
    args = [part.strip().upper() for part in parts[1:] if part.strip()]
    aliases = {
        "/market": "market",
        "/outlook": "market",
        "/trend": "market",
        "/市場": "market",
        "市場": "market",
        "趨勢": "market",
        "/help": "help",
    }
    return aliases.get(command), args


def build_market_command_help() -> str:
    return "\n".join(
        [
            "[check_price]",
            "可用指令",
            "/market",
            "/market BTC ETH",
            "/outlook",
            "/trend",
            "市場",
        ]
    )


def process_telegram_commands(
    conn: sqlite3.Connection,
    config_path: Path,
    timeout: int,
    llama_mode: str,
    llama_model: str,
    quote: str,
    reports_dir: Path,
    telegram_script: Path,
) -> int:
    chat_id = resolve_env("TELEGRAM_CHAT_ID")
    if not chat_id:
        return 0

    offset_value = load_runtime_state(conn, "telegram_update_offset")
    offset = int(offset_value) if offset_value else None
    updates = telegram_api_get_updates(offset=offset)
    if not updates:
        return 0

    processed = 0
    latest_offset = offset
    for update in updates:
        update_id = int(update.get("update_id", 0))
        latest_offset = update_id + 1
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != str(chat_id):
            continue
        command, symbols = parse_telegram_command(str(message.get("text", "")))
        if not command:
            continue
        try:
            if command == "help":
                send_telegram(telegram_script, build_market_command_help())
                processed += 1
                continue
            if command == "market":
                explicit_symbols = [normalize_symbol(symbol, quote) for symbol in symbols] if symbols else None
                payload = build_market_outlook(
                    config_path=config_path,
                    timeout=timeout,
                    llama_mode=llama_mode,
                    llama_model=llama_model,
                    quote=quote,
                    explicit_symbols=explicit_symbols,
                )
                _, md_path = save_market_outlook(payload, reports_dir)
                message_text = "\n".join(
                    [
                        build_market_outlook_message(payload),
                        "",
                        f"已生成報告 {md_path.name}",
                    ]
                )
                send_telegram(telegram_script, message_text)
                processed += 1
        except Exception as exc:  # noqa: BLE001
            send_telegram(telegram_script, f"[check_price]\n指令執行失敗\n{command}\n{exc}")
            processed += 1

    if latest_offset is not None:
        save_runtime_state(conn, "telegram_update_offset", str(latest_offset))
    return processed


def should_send(conn: sqlite3.Connection, event_key: str, cooldown_minutes: int) -> bool:
    row = conn.execute(
        "SELECT last_sent_at FROM alerts WHERE event_key = ?",
        (event_key,),
    ).fetchone()
    if not row:
        return True
    last_sent = dt.datetime.fromisoformat(row["last_sent_at"])
    return utc_now() - last_sent >= dt.timedelta(minutes=cooldown_minutes)


def should_send_system_alert(conn: sqlite3.Connection, event_key: str, cooldown_minutes: int) -> bool:
    row = conn.execute(
        "SELECT last_sent_at FROM system_alerts WHERE event_key = ?",
        (event_key,),
    ).fetchone()
    if not row:
        return True
    last_sent = dt.datetime.fromisoformat(row["last_sent_at"])
    return utc_now() - last_sent >= dt.timedelta(minutes=cooldown_minutes)


def record_system_alert(conn: sqlite3.Connection, event_key: str, event_type: str, message: str) -> None:
    conn.execute(
        """
        INSERT INTO system_alerts (event_key, event_type, last_sent_at, message)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
            last_sent_at=excluded.last_sent_at,
            message=excluded.message
        """,
        (
            event_key,
            event_type,
            utc_now().isoformat(),
            message,
        ),
    )
    conn.commit()


def build_bull_trend_pullback_alert(
    result: dict[str, Any],
    protections: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    settings = settings or {}
    if not settings.get("enable_bull_pullback_alerts", True):
        return None
    signal = result.get("short_term_signal", {}) or {}
    regime = str(signal.get("market_regime", "range_or_mixed"))
    bias = str(signal.get("bias", "neutral"))
    if regime != "bull_trend" and bias != "long":
        return None

    returns = result.get("returns", {}) or {}
    drop_15m = float(settings.get("bull_pullback_15m_drop_pct", 1.0))
    drop_1h = float(settings.get("bull_pullback_1h_drop_pct", 0.8))
    r15 = float(returns.get("15m", 0.0) or 0.0)
    r1h = float(returns.get("1h", 0.0) or 0.0)
    if r15 > -drop_15m and r1h > -drop_1h:
        return None

    symbol = str(result.get("symbol", "UNKNOWN"))
    price = fmt_price(result.get("price"))
    levels = result.get("actionable_levels", {}) or {}
    plan = result.get("long_short_plan", {}) or {}
    long_setup = plan.get("long_setup", {}) or {}
    short_setup = plan.get("short_setup", {}) or {}
    tp_long = (long_setup.get("take_profit") or [None])[0]
    support = levels.get("price_map", {}).get("primary_support") or levels.get("short_ready_zone") or ["-", "-"]
    event_key = f"bull_pullback:{symbol}"

    lines = [
        "[check_price]",
        f"{symbol} 牛趨勢回撤提醒",
        f"現價 {price}",
        f"15m 漲跌 {fmt_price(r15)}% | 1h 漲跌 {fmt_price(r1h)}%",
        f"結構 {regime} | 目前先視為回撤，不是直接翻空。",
        f"支撐區 {fmt_range(support)}",
        f"上方轉強 {fmt_price(long_setup.get('trigger_price'))} | 目標1 {fmt_price(tp_long)}",
        f"失守轉弱 {fmt_price(levels.get('breakout_down'))} | 空方失效 {fmt_price(short_setup.get('stop_loss'))}",
        "建議：先不要追空，先看支撐是否守住，再等 1h 結構重新同步。",
    ]
    summaries = [str(x).strip() for x in protections.get("summaries", []) if str(x).strip()]
    if summaries:
        lines.append("保護層 " + "；".join(summaries[:2]))
    return {
        "event_key": event_key,
        "event_type": "bull_trend_pullback_alert",
        "message": "\n".join(lines),
    }


def build_shock_15m_alert(
    result: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    settings = settings or {}
    if not settings.get("enable_15m_shock_alerts", True):
        return None
    returns = result.get("returns", {}) or {}
    shock_threshold = float(settings.get("shock_15m_pct", 1.3))
    r15 = float(returns.get("15m", 0.0) or 0.0)
    if abs(r15) < shock_threshold:
        return None

    symbol = str(result.get("symbol", "UNKNOWN"))
    price = fmt_price(result.get("price"))
    direction = "急漲" if r15 > 0 else "急跌"
    levels = result.get("actionable_levels", {}) or {}
    plan = result.get("long_short_plan", {}) or {}
    long_setup = plan.get("long_setup", {}) or {}
    short_setup = plan.get("short_setup", {}) or {}
    event_key = f"shock15m:{symbol}:{'up' if r15 > 0 else 'down'}"
    lines = [
        "[check_price]",
        f"{symbol} 15m {direction}提醒",
        f"現價 {price}",
        f"15m 漲跌 {fmt_price(r15)}% | 1h 漲跌 {fmt_price(returns.get('1h', '-'))}%",
        f"上方劇本 {fmt_price(long_setup.get('trigger_price'))} | 止損 {fmt_price(long_setup.get('stop_loss'))}",
        f"下方劇本 {fmt_price(short_setup.get('trigger_price'))} | 止損 {fmt_price(short_setup.get('stop_loss'))}",
        "建議：這是 15 分鐘級別的快速移動，先看是否延續，不要把單根波動誤當結構反轉。",
    ]
    return {
        "event_key": event_key,
        "event_type": "shock_15m_alert",
        "message": "\n".join(lines),
    }


def default_symbol_state(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "long_stage": "idle",
        "short_stage": "idle",
        "long_breakout_seen_at": None,
        "short_breakout_seen_at": None,
        "long_retest_seen_at": None,
        "short_retest_seen_at": None,
        "long_context": None,
        "short_context": None,
    }


def parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except Exception:
        return None


def reset_side_state(state: dict[str, Any], side: str) -> None:
    if side == "long":
        state["long_stage"] = "idle"
        state["long_breakout_seen_at"] = None
        state["long_retest_seen_at"] = None
        state["long_context"] = None
        return
    state["short_stage"] = "idle"
    state["short_breakout_seen_at"] = None
    state["short_retest_seen_at"] = None
    state["short_context"] = None


def load_symbol_state(conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT state_json FROM symbol_states WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    if not row:
        return default_symbol_state(symbol)
    try:
        state = json.loads(row["state_json"])
        if isinstance(state, dict):
            return {**default_symbol_state(symbol), **state, "symbol": symbol}
    except Exception:
        pass
    return default_symbol_state(symbol)


def save_symbol_state(conn: sqlite3.Connection, symbol: str, state: dict[str, Any]) -> None:
    payload = {**default_symbol_state(symbol), **state, "symbol": symbol}
    conn.execute(
        """
        INSERT INTO symbol_states (
            symbol, long_stage, short_stage, long_breakout_seen_at, short_breakout_seen_at,
            long_retest_seen_at, short_retest_seen_at, updated_at, state_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            long_stage=excluded.long_stage,
            short_stage=excluded.short_stage,
            long_breakout_seen_at=excluded.long_breakout_seen_at,
            short_breakout_seen_at=excluded.short_breakout_seen_at,
            long_retest_seen_at=excluded.long_retest_seen_at,
            short_retest_seen_at=excluded.short_retest_seen_at,
            updated_at=excluded.updated_at,
            state_json=excluded.state_json
        """,
        (
            symbol,
            str(payload["long_stage"]),
            str(payload["short_stage"]),
            payload["long_breakout_seen_at"],
            payload["short_breakout_seen_at"],
            payload["long_retest_seen_at"],
            payload["short_retest_seen_at"],
            utc_now().isoformat(),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()


def capture_side_context(result: dict[str, Any], side: str) -> dict[str, Any]:
    levels = result["actionable_levels"]
    plan = result["long_short_plan"]
    if side == "long":
        setup = plan["long_setup"]
        return {
            "captured_at": utc_now().isoformat(),
            "breakout": float(levels["breakout_up"]),
            "ready_zone": [float(x) for x in levels["long_ready_zone"]],
            "retest_zone": [float(x) for x in setup["confirmation"]["retest_zone"]],
            "trigger_price": float(setup["trigger_price"]),
            "entry_zone": [float(x) for x in setup["entry_zone"]],
            "stop_loss": float(setup["stop_loss"]),
            "take_profit": [float(x) for x in setup["take_profit"]],
            "second_trigger": float(setup["confirmation"]["second_breakout_trigger"]),
            "failure_level": float(setup["confirmation"]["retest_failure_level"]),
            "breakeven_trigger": float(setup["management"]["breakeven_trigger"]),
            "breakeven_stop": float(setup["management"]["breakeven_stop"]),
            "scale_out_zone": [float(x) for x in setup["management"]["scale_out_zone"]],
            "runner_zone": [float(x) for x in setup["management"]["runner_zone"]],
        }
    setup = plan["short_setup"]
    return {
        "captured_at": utc_now().isoformat(),
        "breakout": float(levels["breakout_down"]),
        "ready_zone": [float(x) for x in levels["short_ready_zone"]],
        "retest_zone": [float(x) for x in setup["confirmation"]["retest_zone"]],
        "trigger_price": float(setup["trigger_price"]),
        "entry_zone": [float(x) for x in setup["entry_zone"]],
        "stop_loss": float(setup["stop_loss"]),
        "take_profit": [float(x) for x in setup["take_profit"]],
        "second_trigger": float(setup["confirmation"]["second_breakout_trigger"]),
        "failure_level": float(setup["confirmation"]["retest_failure_level"]),
        "breakeven_trigger": float(setup["management"]["breakeven_trigger"]),
        "breakeven_stop": float(setup["management"]["breakeven_stop"]),
        "scale_out_zone": [float(x) for x in setup["management"]["scale_out_zone"]],
        "runner_zone": [float(x) for x in setup["management"]["runner_zone"]],
    }


def unlock_stale_contexts(
    result: dict[str, Any],
    state: dict[str, Any],
    lock_timeout_minutes: int,
    lock_drift_pct: float,
) -> dict[str, Any]:
    latest_levels = result["actionable_levels"]
    latest_plan = result["long_short_plan"]
    if not isinstance(latest_plan, dict):
        return state
    now = utc_now()

    def evaluate(side: str, latest_trigger: float, latest_failure: float) -> None:
        context_key = f"{side}_context"
        stage_key = f"{side}_stage"
        context = state.get(context_key)
        if not isinstance(context, dict):
            return

        stage = str(state.get(stage_key, "idle"))
        if stage == "reconfirmed":
            return

        captured_at = parse_iso_datetime(context.get("captured_at"))
        if captured_at and now - captured_at >= dt.timedelta(minutes=lock_timeout_minutes):
            reset_side_state(state, side)
            return

        locked_trigger = float(context.get("trigger_price", latest_trigger))
        locked_failure = float(context.get("failure_level", latest_failure))
        drift_pct = abs(latest_trigger - locked_trigger) / locked_trigger * 100 if locked_trigger else 0.0
        failure_drift_pct = abs(latest_failure - locked_failure) / abs(locked_failure) * 100 if locked_failure else 0.0
        if stage in {"idle", "touched"} and (
            drift_pct >= lock_drift_pct or failure_drift_pct >= max(lock_drift_pct, 0.75)
        ):
            reset_side_state(state, side)
            return
        if stage in {"confirmed", "retest"} and (
            drift_pct >= max(lock_drift_pct * 2, lock_drift_pct + 0.5)
            or failure_drift_pct >= max(lock_drift_pct * 2, lock_drift_pct + 0.75)
        ):
            reset_side_state(state, side)

    evaluate(
        side="long",
        latest_trigger=float(latest_plan["long_setup"]["trigger_price"]),
        latest_failure=float(latest_plan["long_setup"]["confirmation"]["retest_failure_level"]),
    )
    evaluate(
        side="short",
        latest_trigger=float(latest_plan["short_setup"]["trigger_price"]),
        latest_failure=float(latest_plan["short_setup"]["confirmation"]["retest_failure_level"]),
    )
    return state


def apply_locked_context(result: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    locked = copy.deepcopy(result)
    levels = locked["actionable_levels"]
    plan = locked["long_short_plan"]
    if not isinstance(plan, dict):
        return locked

    long_context = state.get("long_context")
    if isinstance(long_context, dict):
        levels["breakout_up"] = round(float(long_context["breakout"]), 4)
        levels["long_ready_zone"] = [round(float(x), 4) for x in long_context["ready_zone"]]
        levels["long_retest_zone"] = [round(float(x), 4) for x in long_context["retest_zone"]]
        setup = plan["long_setup"]
        setup["trigger_price"] = round(float(long_context["trigger_price"]), 4)
        setup["entry_zone"] = [round(float(x), 4) for x in long_context["entry_zone"]]
        setup["stop_loss"] = round(float(long_context["stop_loss"]), 4)
        setup["take_profit"] = [round(float(x), 4) for x in long_context["take_profit"]]
        setup["confirmation"]["first_breakout_watch"] = round(float(long_context["trigger_price"]), 4)
        setup["confirmation"]["retest_zone"] = [round(float(x), 4) for x in long_context["retest_zone"]]
        setup["confirmation"]["second_breakout_trigger"] = round(float(long_context["second_trigger"]), 4)
        setup["confirmation"]["retest_failure_level"] = round(float(long_context["failure_level"]), 4)
        setup["management"]["breakeven_trigger"] = round(float(long_context["breakeven_trigger"]), 4)
        setup["management"]["breakeven_stop"] = round(float(long_context["breakeven_stop"]), 4)
        setup["management"]["scale_out_zone"] = [round(float(x), 4) for x in long_context["scale_out_zone"]]
        setup["management"]["runner_zone"] = [round(float(x), 4) for x in long_context["runner_zone"]]

    short_context = state.get("short_context")
    if isinstance(short_context, dict):
        levels["breakout_down"] = round(float(short_context["breakout"]), 4)
        levels["short_ready_zone"] = [round(float(x), 4) for x in short_context["ready_zone"]]
        levels["short_retest_zone"] = [round(float(x), 4) for x in short_context["retest_zone"]]
        setup = plan["short_setup"]
        setup["trigger_price"] = round(float(short_context["trigger_price"]), 4)
        setup["entry_zone"] = [round(float(x), 4) for x in short_context["entry_zone"]]
        setup["stop_loss"] = round(float(short_context["stop_loss"]), 4)
        setup["take_profit"] = [round(float(x), 4) for x in short_context["take_profit"]]
        setup["confirmation"]["first_breakout_watch"] = round(float(short_context["trigger_price"]), 4)
        setup["confirmation"]["retest_zone"] = [round(float(x), 4) for x in short_context["retest_zone"]]
        setup["confirmation"]["second_breakout_trigger"] = round(float(short_context["second_trigger"]), 4)
        setup["confirmation"]["retest_failure_level"] = round(float(short_context["failure_level"]), 4)
        setup["management"]["breakeven_trigger"] = round(float(short_context["breakeven_trigger"]), 4)
        setup["management"]["breakeven_stop"] = round(float(short_context["breakeven_stop"]), 4)
        setup["management"]["scale_out_zone"] = [round(float(x), 4) for x in short_context["scale_out_zone"]]
        setup["management"]["runner_zone"] = [round(float(x), 4) for x in short_context["runner_zone"]]

    return locked
def extract_risk_levels(result: dict[str, Any], event_type: str) -> tuple[float | None, float | None, float | None]:
    plan = result["long_short_plan"]
    if event_type in {"effective_long_breakout", "retest_hold_long", "second_breakout_long"}:
        setup = plan["long_setup"]
        tp = setup["take_profit"]
        return float(setup["stop_loss"]), float(tp[0]), float(tp[1])
    if event_type in {"effective_short_breakdown", "retest_hold_short", "second_breakdown_short"}:
        setup = plan["short_setup"]
        tp = setup["take_profit"]
        return float(setup["stop_loss"]), float(tp[0]), float(tp[1])
    return None, None, None


def record_alert(
    conn: sqlite3.Connection,
    event_key: str,
    symbol: str,
    event_type: str,
    level: float,
    price: float,
    message: str,
    result: dict[str, Any],
) -> None:
    sent_at = utc_now().isoformat()
    conn.execute(
        """
        INSERT INTO alerts (event_key, symbol, event_type, level, last_sent_at, last_price, message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
            last_sent_at=excluded.last_sent_at,
            last_price=excluded.last_price,
            message=excluded.message
        """,
        (
            event_key,
            symbol,
            event_type,
            level,
            sent_at,
            price,
            message,
        ),
    )
    stop_loss, tp1, tp2 = extract_risk_levels(result, event_type)
    conn.execute(
        """
        INSERT INTO alert_events (
            event_key, symbol, event_type, direction, level, sent_at, last_price,
            stop_loss, take_profit_1, take_profit_2, timeframe_view_json,
            actionable_levels_json, short_term_signal_json, protections_json,
            long_short_plan_json, message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_key,
            symbol,
            event_type,
            event_direction(event_type),
            level,
            sent_at,
            price,
            stop_loss,
            tp1,
            tp2,
            json.dumps(result["timeframe_view"], ensure_ascii=False),
            json.dumps(result["actionable_levels"], ensure_ascii=False),
            json.dumps(result["short_term_signal"], ensure_ascii=False),
            json.dumps(result.get("protections", {}), ensure_ascii=False),
            json.dumps(result["long_short_plan"], ensure_ascii=False),
            message,
        ),
    )
    conn.commit()


def process_symbol_events(
    conn: sqlite3.Connection,
    symbol: str,
    result: dict[str, Any],
    events: list[dict[str, Any]],
    next_state: dict[str, Any],
    cooldown_minutes: int,
    telegram_script: Path,
) -> int:
    sent_count = 0
    should_commit_state = True
    for event in events:
        event_key = f"{symbol}:{event['event_type']}:{round(float(event['level']), 4)}"
        if not should_send(conn, event_key, cooldown_minutes):
            continue
        try:
            send_telegram(telegram_script, event["message"])
            record_alert(
                conn,
                event_key=event_key,
                symbol=symbol,
                event_type=event["event_type"],
                level=float(event["level"]),
                price=float(result["price"]),
                message=event["message"],
                result=result,
            )
            sent_count += 1
        except Exception as exc:  # noqa: BLE001
            should_commit_state = False
            print(
                f"[{utc_now().isoformat()}] alert_send_error "
                f"symbol={symbol} event_type={event['event_type']} error={exc}"
            )
            break
    if should_commit_state:
        save_symbol_state(conn, symbol, next_state)
    return sent_count


def render_message(language: str, zh_lines: list[str], en_lines: list[str]) -> str:
    if language == "zh":
        if len(zh_lines) >= 6 and "觀察" in zh_lines[1]:
            compact = [
                zh_lines[0],
                zh_lines[1],
                zh_lines[2] if len(zh_lines) > 2 else "",
            ]
            if len(zh_lines) > 3:
                compact.append(zh_lines[3])
            if len(zh_lines) > 4:
                compact.append(zh_lines[4].replace("若上破成立：", "").replace("若下破成立：", ""))
            compact.append("先觀察，等首破/首跌確認。")
            return "\n".join([line for line in compact if line])
        return "\n".join(zh_lines)
    if language == "en":
        return "\n".join(en_lines)
    return "\n".join(zh_lines + ["---"] + en_lines)


def rewrite_events_for_paper_notices(
    events: list[dict[str, Any]],
    result: dict[str, Any],
    language: str,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if language != "zh":
        return events
    settings = settings or {}
    rewritten: list[dict[str, Any]] = []
    for event in events:
        rewritten.append(
            {
                **event,
                "message": build_paper_event_message(
                    str(event.get("event_type", "")),
                    result,
                    float(event.get("level", 0.0)),
                    settings,
                ),
            }
        )
    return rewritten


def apply_protection_layer_to_events(
    events: list[dict[str, Any]],
    protections: dict[str, Any],
) -> list[dict[str, Any]]:
    if not events:
        return events

    filtered: list[dict[str, Any]] = []
    notes = [str(x).strip() for x in protections.get("summaries", []) if str(x).strip()]
    for event in events:
        event_type = str(event.get("event_type", ""))
        role = event_role(event_type)
        direction = event_direction(event_type)
        if role != "watch":
            if protections.get("hard_block"):
                continue
            if direction == "up" and protections.get("pause_long"):
                continue
            if direction == "down" and protections.get("pause_short"):
                continue
        if notes:
            message = str(event.get("message", "")).rstrip()
            event = {**event, "message": message + "\n保護層：" + "；".join(notes)}
        filtered.append(event)
    return filtered


def build_events(
    result: dict[str, Any],
    settings: dict[str, Any],
    symbol_state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state = {**default_symbol_state(result["symbol"]), **symbol_state, "symbol": result["symbol"]}
    lock_timeout_minutes = int(settings.get("lock_timeout_minutes", 120))
    lock_drift_pct = float(settings.get("lock_drift_pct", 1.0))
    state = unlock_stale_contexts(result, state, lock_timeout_minutes, lock_drift_pct)
    result = apply_locked_context(result, state)
    symbol = result["symbol"]
    price = float(result["price"])
    levels = result["actionable_levels"]
    long_short = result["long_short_plan"]
    if not isinstance(long_short, dict):
        return [], state
    short_signal = result["short_term_signal"]
    tf5 = result["timeframe_view"]["5m"]
    tf_exec = result["timeframe_view"].get("1h", tf5)
    tf4 = result["timeframe_view"].get("4h", tf_exec)
    false_breakout = short_signal.get("false_breakout", {})
    proximity_pct = float(settings.get("approach_proximity_pct", 0.15))
    approach_edge_ratio = float(settings.get("approach_edge_ratio", 0.35))
    approach_up_max_trigger_distance_pct = float(settings.get("approach_up_max_trigger_distance_pct", 999.0))
    approach_down_max_trigger_distance_pct = float(settings.get("approach_down_max_trigger_distance_pct", 999.0))
    effective_long_volume_ratio_min = float(settings.get("effective_long_volume_ratio_min", 1.2))
    effective_short_volume_ratio_min = float(settings.get("effective_short_volume_ratio_min", 1.2))
    retest_volume_ratio_min = float(settings.get("retest_volume_ratio_min", 1.05))
    long_symbol_tuning = settings.get("long_symbol_tuning", {}) or {}
    language = str(settings.get("notification_language", "bilingual")).lower()
    events: list[dict[str, Any]] = []

    long_setup = long_short["long_setup"]
    short_setup = long_short["short_setup"]
    analysis_bias = str(long_short.get("analysis_bias", "neutral"))
    market_regime = str(short_signal.get("market_regime", "range_or_mixed"))
    risk_level = str(result.get("risk_level", "medium")).lower()
    market_state = result.get("market_state", {}) or {}
    warnings = [str(w) for w in result.get("warnings", [])]
    protections = result.get("protections", {}) or {}
    gate_open = short_signal.get("gate_open", {})
    long_core_score = int(short_signal.get("long_core_score", 0))
    short_core_score = int(short_signal.get("short_core_score", 0))
    long_watch_allowed = bool(gate_open.get("long")) and long_core_score >= short_core_score
    short_watch_allowed = bool(gate_open.get("short")) and short_core_score >= long_core_score
    if analysis_bias == "long_bias":
        long_watch_allowed = bool(gate_open.get("long"))
        short_watch_allowed = False
    elif analysis_bias == "short_bias":
        long_watch_allowed = False
        short_watch_allowed = bool(gate_open.get("short"))
    # In mixed, noisy markets a down-side watch can be valuable before the
    # higher-timeframe short gate fully opens. Keep this as watch-only by
    # widening approach_down eligibility, while leaving all actionable short
    # events on the stricter bias/volume confirmation path below.
    short_watch_override = (
        market_regime == "range_or_mixed"
        and not bool(protections.get("pause_short"))
        and not bool(protections.get("hard_block"))
        and (
            short_core_score >= 1
            or (
                bool(market_state.get("is_sideways"))
                and str(tf_exec.get("trend")) in {"bearish", "mixed"}
                and not bool(tf_exec.get("above_vwap"))
                and float(tf_exec.get("rsi14", 50.0)) <= 50.0
            )
            or risk_level == "high"
            or any("不建議追多" in warning or "偏空" in warning for warning in warnings)
        )
    )
    if short_watch_override:
        short_watch_allowed = True
    long_confirm = long_setup.get("confirmation", {})
    short_confirm = short_setup.get("confirmation", {})
    long_manage = long_setup.get("management", {})
    short_manage = short_setup.get("management", {})
    long_executor = long_setup.get("executor_plan", {})
    short_executor = short_setup.get("executor_plan", {})
    long_take_profit = [float(x) for x in long_setup.get("take_profit", [])]
    short_take_profit = [float(x) for x in short_setup.get("take_profit", [])]
    symbol_long_tuning = long_symbol_tuning.get(symbol, {}) or {}
    symbol_long_effective_volume_ratio_min = float(
        symbol_long_tuning.get("effective_volume_ratio_min", effective_long_volume_ratio_min)
    )
    symbol_long_min_core_score = int(symbol_long_tuning.get("min_core_score", 0))
    symbol_long_require_4h_bullish = bool(symbol_long_tuning.get("require_4h_bullish", False))
    symbol_long_require_1h_above_vwap = bool(symbol_long_tuning.get("require_1h_above_vwap", False))

    long_trigger = float(long_setup["trigger_price"])
    short_trigger = float(short_setup["trigger_price"])
    breakout_up = float(levels["breakout_up"])
    breakout_down = float(levels["breakout_down"])
    long_ready_zone = [float(x) for x in levels.get("long_ready_zone", [breakout_up, breakout_up])]
    short_ready_zone = [float(x) for x in levels.get("short_ready_zone", [breakout_down, breakout_down])]
    long_retest_zone = [float(x) for x in long_confirm.get("retest_zone", [long_trigger, long_trigger])]
    short_retest_zone = [float(x) for x in short_confirm.get("retest_zone", [short_trigger, short_trigger])]
    long_second_trigger = float(long_confirm.get("second_breakout_trigger", long_trigger))
    short_second_trigger = float(short_confirm.get("second_breakout_trigger", short_trigger))
    long_failure = float(long_confirm.get("retest_failure_level", long_setup["stop_loss"]))
    short_failure = float(short_confirm.get("retest_failure_level", short_setup["stop_loss"]))

    long_symbol_actionable_ok = (
        long_core_score >= symbol_long_min_core_score
        and (not symbol_long_require_4h_bullish or str(tf4.get("trend")) == "bullish")
        and (not symbol_long_require_1h_above_vwap or bool(tf_exec.get("above_vwap")))
    )
    long_valid = (
        short_signal["bias"] == "long"
        and tf_exec["volume_ratio"] >= symbol_long_effective_volume_ratio_min
        and not false_breakout.get("false_breakout_up", False)
        and price >= long_trigger
        and long_symbol_actionable_ok
    )
    prev_long_stage = state["long_stage"]
    prev_short_stage = state["short_stage"]
    short_valid = (
        short_signal["bias"] == "short"
        and tf_exec["volume_ratio"] >= effective_short_volume_ratio_min
        and not false_breakout.get("false_breakout_down", False)
        and price <= short_trigger
    )
    long_retest_valid = (
        tf_exec["volume_ratio"] >= retest_volume_ratio_min
        and bool(tf_exec.get("above_vwap"))
        and str(tf_exec.get("trend")) == "bullish"
    )
    short_retest_valid = (
        tf_exec["volume_ratio"] >= retest_volume_ratio_min
        and not bool(tf_exec.get("above_vwap"))
        and str(tf_exec.get("trend")) == "bearish"
    )
    long_trigger_distance_pct = abs(long_trigger - price) / price * 100 if price > 0 else 0.0
    short_trigger_distance_pct = abs(short_trigger - price) / price * 100 if price > 0 else 0.0
    long_ready_width = max(long_ready_zone[1] - long_ready_zone[0], 0.0)
    short_ready_width = max(short_ready_zone[1] - short_ready_zone[0], 0.0)
    long_edge_floor = long_ready_zone[1] - long_ready_width * approach_edge_ratio
    short_edge_ceiling = short_ready_zone[0] + short_ready_width * approach_edge_ratio

    def add_event(event_type: str, level: float, zh_lines: list[str], en_lines: list[str]) -> None:
        events.append(
            {
                "event_type": event_type,
                "level": round(level, 4),
                "message": render_message(language, zh_lines, en_lines),
            }
        )

    def executor_quality_labels(quality: str) -> tuple[str, str]:
        mapping = {
            "tradable": ("可執行", "tradable"),
            "caution": ("保守", "caution"),
            "observe_only": ("只觀察", "observe_only"),
        }
        return mapping.get(quality, (quality or "-", quality or "-"))

    if price < long_failure:
        state["long_stage"] = "idle"
        state["long_breakout_seen_at"] = None
        state["long_retest_seen_at"] = None
        state["long_context"] = None
    if price > short_failure:
        state["short_stage"] = "idle"
        state["short_breakout_seen_at"] = None
        state["short_retest_seen_at"] = None
        state["short_context"] = None

    if settings.get("enable_approach_alerts", True):
        upper_ready = (
            long_edge_floor <= price <= breakout_up * (1 + proximity_pct / 100)
            and not long_valid
            and long_trigger_distance_pct <= approach_up_max_trigger_distance_pct
            and str(tf_exec.get("trend")) in {"bullish", "mixed"}
        )
        if upper_ready and long_watch_allowed:
            state["long_context"] = state.get("long_context") or capture_side_context(result, "long")
            add_event(
                "approach_up",
                breakout_up,
                [
                    "[check_price]",
                    f"{symbol} 波段轉強觀察",
                    f"現價 {fmt_price(price)}",
                    f"上破價 {fmt_price(breakout_up)}",
                    f"做多區 {fmt_range(long_ready_zone)}",
                    f"若上破成立：止損 {fmt_price(long_setup['stop_loss'])}",
                    f"若上破成立：目標 {fmt_range(long_take_profit[:2])}",
                    "戰備層：先看價位、止損與目標，不追價。",
                ],
                [
                    "[check_price]",
                    f"{symbol} entered the upper breakout watch zone",
                    f"Current price: {fmt_price(price)}",
                    f"Breakout price: {fmt_price(breakout_up)}",
                    f"Long execution zone: {fmt_range(long_ready_zone)}",
                    f"If breakout confirms: stop loss {fmt_price(long_setup['stop_loss'])}",
                    f"If breakout confirms: targets {fmt_range(long_take_profit[:2])}",
                    "Watch layer: review the plan first and wait for the first breakout.",
                ],
            )
        lower_ready = (
            breakout_down * (1 - proximity_pct / 100) <= price <= short_edge_ceiling
            and not short_valid
            and short_trigger_distance_pct <= approach_down_max_trigger_distance_pct
            and str(tf_exec.get("trend")) in {"bearish", "mixed"}
        )
        if lower_ready and short_watch_allowed:
            state["short_context"] = state.get("short_context") or capture_side_context(result, "short")
            add_event(
                "approach_down",
                breakout_down,
                [
                    "[check_price]",
                    f"{symbol} 波段轉弱觀察",
                    f"現價 {fmt_price(price)}",
                    f"下破價 {fmt_price(breakout_down)}",
                    f"做空區 {fmt_range(short_ready_zone)}",
                    f"若下破成立：止損 {fmt_price(short_setup['stop_loss'])}",
                    f"若下破成立：目標 {fmt_range(short_take_profit[:2])}",
                    "戰備層：先看價位、止損與目標，不提前空。",
                ],
                [
                    "[check_price]",
                    f"{symbol} entered the lower breakdown watch zone",
                    f"Current price: {fmt_price(price)}",
                    f"Breakdown price: {fmt_price(breakout_down)}",
                    f"Short execution zone: {fmt_range(short_ready_zone)}",
                    f"If breakdown confirms: stop loss {fmt_price(short_setup['stop_loss'])}",
                    f"If breakdown confirms: targets {fmt_range(short_take_profit[:2])}",
                    "Watch layer: review the plan first and wait for the first breakdown.",
                ],
            )

    if settings.get("enable_trigger_alerts", True) and price >= long_trigger and not long_valid:
        if state["long_stage"] == "idle":
            state["long_stage"] = "touched"
            state["long_breakout_seen_at"] = utc_now().isoformat()
            state["long_context"] = state.get("long_context") or capture_side_context(result, "long")
            add_event(
                "breakout_touch_up",
                long_trigger,
                [
                    "[check_price]",
                    f"{symbol} 首破觀察（試單層）",
                    f"現價 {fmt_price(price)}",
                    f"試單價 {fmt_price(long_trigger)} | 量比 {fmt_price(tf_exec['volume_ratio'])}",
                    f"失效 {fmt_price(long_setup['stop_loss'])} | 目標 {fmt_price(long_take_profit[0] if long_take_profit else '-')}",
                    "試單層：可小倉觀察，主倉仍等回踩或收線確認。",
                ],
                [
                    "[check_price]",
                    f"{symbol} touched the long breakout price",
                    f"Current price: {fmt_price(price)}",
                    f"Breakout price: {fmt_price(long_trigger)}",
                    f"Probe entry {fmt_price(long_trigger)} | Volume ratio: {fmt_price(tf_exec['volume_ratio'])}",
                    f"Invalidation {fmt_price(long_setup['stop_loss'])} | Target {fmt_price(long_take_profit[0] if long_take_profit else '-')}",
                    "Probe layer: small size only. Main confirmation still waits for retest or candle close.",
                ],
            )

    if settings.get("enable_trigger_alerts", True) and price <= short_trigger and not short_valid:
        if state["short_stage"] == "idle":
            state["short_stage"] = "touched"
            state["short_breakout_seen_at"] = utc_now().isoformat()
            state["short_context"] = state.get("short_context") or capture_side_context(result, "short")
            add_event(
                "breakout_touch_down",
                short_trigger,
                [
                    "[check_price]",
                    f"{symbol} 首跌觀察（試單層）",
                    f"現價 {fmt_price(price)}",
                    f"下破價 {fmt_price(short_trigger)}",
                    f"量比 {fmt_price(tf_exec['volume_ratio'])}",
                    "試單層：先不追，等反抽或收線確認。",
                ],
                [
                    "[check_price]",
                    f"{symbol} touched the short breakdown price",
                    f"Current price: {fmt_price(price)}",
                    f"Breakdown price: {fmt_price(short_trigger)}",
                    f"Volume ratio: {fmt_price(tf_exec['volume_ratio'])}",
                    "Probe layer: the first breakdown appeared, but the trade thesis is not confirmed yet.",
                ],
            )

    long_in_retest = long_retest_zone[0] <= price <= long_retest_zone[1]
    if prev_long_stage in {"touched", "confirmed"} and long_in_retest and price >= long_failure and long_retest_valid:
        state["long_stage"] = "retest"
        state["long_retest_seen_at"] = utc_now().isoformat()
        add_event(
            "retest_hold_long",
            long_retest_zone[0],
            [
                "[check_price]",
                f"{symbol} 回踩未壞（資訊層）",
                f"現價 {fmt_price(price)}",
                f"回踩區 {fmt_range(long_retest_zone)}",
                f"量比 {fmt_price(tf_exec['volume_ratio'])}",
                f"二突價 {fmt_price(long_second_trigger)}",
                f"失敗位 {fmt_price(long_failure)}",
                "資訊層：劇本未壞，但不代表現在就是好進場。",
            ],
            [
                "[check_price]",
                f"{symbol} long retest has not failed",
                f"Current price: {fmt_price(price)}",
                f"Retest zone: {fmt_range(long_retest_zone)}",
                f"Volume ratio: {fmt_price(tf_exec['volume_ratio'])}",
                f"Second breakout trigger: {fmt_price(long_second_trigger)}",
                f"Failure level: {fmt_price(long_failure)}",
                "Info layer: the thesis is not broken, but this is not a strong standalone entry.",
            ],
        )

    short_in_retest = short_retest_zone[0] <= price <= short_retest_zone[1]
    if prev_short_stage in {"touched", "confirmed"} and short_in_retest and price <= short_failure and short_retest_valid:
        state["short_stage"] = "retest"
        state["short_retest_seen_at"] = utc_now().isoformat()
        add_event(
            "retest_hold_short",
            short_retest_zone[1],
            [
                "[check_price]",
                f"{symbol} 反抽確認（setup）",
                f"現價 {fmt_price(price)}",
                f"反抽區 {fmt_range(short_retest_zone)}",
                f"量比 {fmt_price(tf_exec['volume_ratio'])}",
                f"二跌價 {fmt_price(short_second_trigger)}",
                f"失敗位 {fmt_price(short_failure)}",
            ],
            [
                "[check_price]",
                f"{symbol} short retest is holding",
                f"Current price: {fmt_price(price)}",
                f"Retest zone: {fmt_range(short_retest_zone)}",
                f"Volume ratio: {fmt_price(tf_exec['volume_ratio'])}",
                f"Second breakdown trigger: {fmt_price(short_second_trigger)}",
                f"Failure level: {fmt_price(short_failure)}",
                "Setup layer: retest failed. Wait for the second breakdown before treating it as a conservative entry.",
            ],
        )

    if settings.get("enable_trigger_alerts", True) and long_valid:
        if state["long_stage"] in {"idle", "touched"}:
            state["long_stage"] = "confirmed"
            state["long_breakout_seen_at"] = state["long_breakout_seen_at"] or utc_now().isoformat()
            state["long_context"] = state.get("long_context") or capture_side_context(result, "long")
            add_event(
                "effective_long_breakout",
                long_trigger,
                [
                    "[check_price]",
                    f"{symbol} 波段轉強確認（首層）",
                    f"現價 {fmt_price(price)}",
                    f"上破價 {fmt_price(long_trigger)}",
                    f"回踩區 {fmt_range(long_retest_zone)}",
                    f"二突價 {fmt_price(long_second_trigger)}",
                    f"止損 {fmt_price(long_setup['stop_loss'])}",
                ],
                [
                    "[check_price]",
                    f"{symbol} first long breakout confirmed",
                    f"Current price: {fmt_price(price)}",
                    f"Trigger price: {fmt_price(long_trigger)}",
                    f"Volume ratio: {fmt_price(tf_exec['volume_ratio'])}",
                    f"Retest zone: {fmt_range(long_retest_zone)}",
                    f"Breakeven trigger: {fmt_price(long_manage.get('breakeven_trigger', '-'))}",
                    f"Stop loss: {fmt_price(long_setup['stop_loss'])}",
                ],
            )
        if state["long_stage"] == "retest" and price >= long_second_trigger:
            state["long_stage"] = "reconfirmed"
            long_quality = str(long_executor.get("quality", "tradable"))
            if long_quality != "observe_only":
                long_quality_zh, long_quality_en = executor_quality_labels(long_quality)
                long_notes = [str(x).strip() for x in long_executor.get("notes", []) if str(x).strip()]
                add_event(
                    "second_breakout_long",
                    long_second_trigger,
                    [
                        "[check_price]",
                    f"{symbol} 波段二次轉強（保守掛單）",
                        f"現價 {fmt_price(price)}",
                        f"掛單價 {fmt_price(long_second_trigger)}",
                        f"止損 {fmt_price(long_setup['stop_loss'])}",
                        f"RR(TP1) {fmt_price(long_executor.get('rr_to_tp1', '-'))} | 品質 {long_quality_zh}",
                        f"單型 {long_executor.get('order_type', 'stop_market')} | 取消單 {long_executor.get('cancel_after_minutes', '-')} 分鐘",
                        f"保本 {fmt_price(long_manage.get('breakeven_trigger', '-'))} -> {fmt_price(long_manage.get('breakeven_stop', '-'))}",
                        f"分批 {fmt_range(long_manage.get('scale_out_zone', ['-', '-']))}",
                    ] + ([f"提醒 {'；'.join(long_notes)}"] if long_notes else []),
                    [
                        "[check_price]",
                        f"{symbol} second long breakout confirmed",
                        f"Current price: {fmt_price(price)}",
                        f"Second breakout trigger: {fmt_price(long_second_trigger)}",
                        f"Stop loss: {fmt_price(long_setup['stop_loss'])}",
                        f"RR(TP1): {fmt_price(long_executor.get('rr_to_tp1', '-'))} | Quality: {long_quality_en}",
                        f"Order type: {long_executor.get('order_type', 'stop_market')} | Cancel after: {long_executor.get('cancel_after_minutes', '-')} minutes",
                        f"Breakeven: {fmt_price(long_manage.get('breakeven_trigger', '-'))} -> {fmt_price(long_manage.get('breakeven_stop', '-'))}",
                        f"Scale-out zone: {fmt_range(long_manage.get('scale_out_zone', ['-', '-']))}",
                    ] + ([f"Notes: {'; '.join(long_notes)}"] if long_notes else []),
                )

    if settings.get("enable_trigger_alerts", True) and short_valid:
        if state["short_stage"] in {"idle", "touched"}:
            state["short_stage"] = "confirmed"
            state["short_breakout_seen_at"] = state["short_breakout_seen_at"] or utc_now().isoformat()
            state["short_context"] = state.get("short_context") or capture_side_context(result, "short")
            add_event(
                "effective_short_breakdown",
                short_trigger,
                [
                    "[check_price]",
                    f"{symbol} 波段轉弱確認（首層）",
                    f"現價 {fmt_price(price)}",
                    f"下破價 {fmt_price(short_trigger)}",
                    f"反抽區 {fmt_range(short_retest_zone)}",
                    f"二跌價 {fmt_price(short_second_trigger)}",
                    f"止損 {fmt_price(short_setup['stop_loss'])}",
                ],
                [
                    "[check_price]",
                    f"{symbol} first short breakdown confirmed",
                    f"Current price: {fmt_price(price)}",
                    f"Trigger price: {fmt_price(short_trigger)}",
                    f"Volume ratio: {fmt_price(tf_exec['volume_ratio'])}",
                    f"Retest zone: {fmt_range(short_retest_zone)}",
                    f"Breakeven trigger: {fmt_price(short_manage.get('breakeven_trigger', '-'))}",
                    f"Stop loss: {fmt_price(short_setup['stop_loss'])}",
                ],
            )
        if state["short_stage"] == "retest" and price <= short_second_trigger:
            state["short_stage"] = "reconfirmed"
            short_quality = str(short_executor.get("quality", "tradable"))
            if short_quality != "observe_only":
                short_quality_zh, short_quality_en = executor_quality_labels(short_quality)
                short_notes = [str(x).strip() for x in short_executor.get("notes", []) if str(x).strip()]
                add_event(
                    "second_breakdown_short",
                    short_second_trigger,
                    [
                        "[check_price]",
                        f"{symbol} 波段二次轉弱（保守掛單）",
                        f"現價 {fmt_price(price)}",
                        f"掛單價 {fmt_price(short_second_trigger)}",
                        f"止損 {fmt_price(short_setup['stop_loss'])}",
                        f"RR(TP1) {fmt_price(short_executor.get('rr_to_tp1', '-'))} | 品質 {short_quality_zh}",
                        f"單型 {short_executor.get('order_type', 'stop_market')} | 取消單 {short_executor.get('cancel_after_minutes', '-')} 分鐘",
                        f"保本 {fmt_price(short_manage.get('breakeven_trigger', '-'))} -> {fmt_price(short_manage.get('breakeven_stop', '-'))}",
                        f"分批 {fmt_range(short_manage.get('scale_out_zone', ['-', '-']))}",
                    ] + ([f"提醒 {'；'.join(short_notes)}"] if short_notes else []),
                    [
                        "[check_price]",
                        f"{symbol} second short breakdown confirmed",
                        f"Current price: {fmt_price(price)}",
                        f"Second breakdown trigger: {fmt_price(short_second_trigger)}",
                        f"Stop loss: {fmt_price(short_setup['stop_loss'])}",
                        f"RR(TP1): {fmt_price(short_executor.get('rr_to_tp1', '-'))} | Quality: {short_quality_en}",
                        f"Order type: {short_executor.get('order_type', 'stop_market')} | Cancel after: {short_executor.get('cancel_after_minutes', '-')} minutes",
                        f"Breakeven: {fmt_price(short_manage.get('breakeven_trigger', '-'))} -> {fmt_price(short_manage.get('breakeven_stop', '-'))}",
                        f"Scale-out zone: {fmt_range(short_manage.get('scale_out_zone', ['-', '-']))}",
                    ] + ([f"Notes: {'; '.join(short_notes)}"] if short_notes else []),
                )

    events = rewrite_events_for_paper_notices(events, result, language, settings)
    return events, state


def run_cycle(
    config: dict[str, Any],
    state_conn: sqlite3.Connection,
    timeout: int,
    llama_mode: str,
    llama_model: str,
    quote: str,
    telegram_script: Path,
) -> int:
    rss_items = fetch_rss_items(timeout)
    symbols = [normalize_symbol(s, quote) for s in config.get("symbols", [])]
    if not symbols:
        raise RuntimeError("watchlist has no symbols")

    sent_count = 0
    cooldown = int(config.get("cooldown_minutes", 30))
    profile = str(config.get("risk_profile", "conservative"))
    settings = dict(config.get("alerts", {}) or {})
    settings["presentation"] = {
        **(config.get("presentation", {}) or {}),
        **(settings.get("presentation", {}) or {}),
    }
    volatility_alert_cooldown = int(settings.get("volatility_alert_cooldown_minutes", max(cooldown, 90)))
    pullback_alert_cooldown = int(settings.get("pullback_alert_cooldown_minutes", max(cooldown, 90)))
    shock_alert_cooldown = int(settings.get("shock_alert_cooldown_minutes", max(cooldown, 60)))
    macro_alert_cooldown = int(settings.get("macro_alert_cooldown_minutes", max(cooldown, 360)))
    protection_settings = normalize_protection_config(config.get("protections", {}))
    funding_settings = config.get("funding", {})
    macro_alert = build_macro_news_alert(rss_items, symbols)
    if macro_alert and should_send_system_alert(state_conn, macro_alert["event_key"], macro_alert_cooldown):
        try:
            send_telegram(telegram_script, macro_alert["message"])
            record_system_alert(
                state_conn,
                event_key=macro_alert["event_key"],
                event_type=macro_alert["event_type"],
                message=macro_alert["message"],
            )
            sent_count += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[{utc_now().isoformat()}] macro_alert_send_error error={exc}")

    for symbol in symbols:
        try:
            analysis = build_symbol_analysis(
                symbol=symbol,
                profile=profile,
                timeout=timeout,
                llama_mode=llama_mode,
                llama_model=llama_model,
                rss_items=rss_items,
                protection_settings=protection_settings,
                funding_settings=funding_settings,
            )
            result = analysis_to_dict(analysis)
            symbol_state = load_symbol_state(state_conn, symbol)
            market_protections = result.get("protections") or evaluate_market_protections(
                symbol=result["symbol"],
                returns=result.get("returns", {"5m": 0.0}),
                volatility=result.get("volatility", {"realized_24h": 0.0}),
                short_term_signal=result.get("short_term_signal", {"market_regime": "range_or_mixed"}),
                risk_level=result.get("risk_level", "medium"),
                config=protection_settings,
            )
            volatility_alert = build_volatility_system_alert(result, market_protections, settings)
            if volatility_alert and should_send_system_alert(
                state_conn,
                volatility_alert["event_key"],
                volatility_alert_cooldown,
            ):
                try:
                    send_telegram(telegram_script, volatility_alert["message"])
                    record_system_alert(
                        state_conn,
                        event_key=volatility_alert["event_key"],
                        event_type=volatility_alert["event_type"],
                        message=volatility_alert["message"],
                    )
                    sent_count += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[{utc_now().isoformat()}] volatility_alert_send_error symbol={symbol} error={exc}")
            pullback_alert = build_bull_trend_pullback_alert(result, market_protections, settings)
            if pullback_alert and should_send_system_alert(
                state_conn,
                pullback_alert["event_key"],
                pullback_alert_cooldown,
            ):
                try:
                    send_telegram(telegram_script, pullback_alert["message"])
                    record_system_alert(
                        state_conn,
                        event_key=pullback_alert["event_key"],
                        event_type=pullback_alert["event_type"],
                        message=pullback_alert["message"],
                    )
                    sent_count += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[{utc_now().isoformat()}] pullback_alert_send_error symbol={symbol} error={exc}")
            shock_alert = build_shock_15m_alert(result, settings)
            if shock_alert and should_send_system_alert(
                state_conn,
                shock_alert["event_key"],
                shock_alert_cooldown,
            ):
                try:
                    send_telegram(telegram_script, shock_alert["message"])
                    record_system_alert(
                        state_conn,
                        event_key=shock_alert["event_key"],
                        event_type=shock_alert["event_type"],
                        message=shock_alert["message"],
                    )
                    sent_count += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[{utc_now().isoformat()}] shock_alert_send_error symbol={symbol} error={exc}")
            events, next_state = build_events(result, settings, symbol_state)
            performance_protections = evaluate_performance_protections(
                conn=state_conn,
                symbol=symbol,
                config=protection_settings,
            )
            runtime_protections = merge_protections(market_protections, performance_protections)
            events = apply_protection_layer_to_events(events, runtime_protections)
            sent_count += process_symbol_events(
                conn=state_conn,
                symbol=symbol,
                result=result,
                events=events,
                next_state=next_state,
                cooldown_minutes=cooldown,
                telegram_script=telegram_script,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{utc_now().isoformat()}] symbol_error symbol={symbol} error={exc}")
    return sent_count


def paper_action_label(event_type: str) -> str:
    mapping = {
        "approach_up": "上方觀察",
        "approach_down": "下方觀察",
        "breakout_touch_up": "首破觀察",
        "breakout_touch_down": "首跌觀察",
        "effective_long_breakout": "確認做多",
        "effective_short_breakdown": "確認做空",
        "retest_hold_long": "多方延續觀察",
        "retest_hold_short": "空方延續觀察",
        "second_breakout_long": "保守做多確認",
        "second_breakdown_short": "保守做空確認",
    }
    return mapping.get(event_type, "事件提醒")


def paper_direction_label(event_type: str) -> str:
    direction = event_direction(event_type)
    if direction == "up":
        return "多"
    if direction == "down":
        return "空"
    return "觀察"


def macro_bias_label(macro_bias: str) -> str:
    mapping = {
        "risk_on": "偏 risk-on",
        "risk_off": "偏 risk-off",
        "neutral": "中性",
    }
    return mapping.get(macro_bias, macro_bias)


def build_paper_event_message(
    event_type: str,
    result: dict[str, Any],
    level: float,
    settings: dict[str, Any] | None = None,
) -> str:
    symbol = str(result.get("symbol", "UNKNOWN"))
    price = fmt_price(result.get("price"))
    presentation = (settings or {}).get("presentation", {}) or {}
    plan = result.get("long_short_plan", {}) or {}
    tf_exec = (result.get("timeframe_view", {}) or {}).get("1h", {}) or {}
    long_setup = plan.get("long_setup", {}) or {}
    short_setup = plan.get("short_setup", {}) or {}
    setup = long_setup if event_direction(event_type) == "up" else short_setup if event_direction(event_type) == "down" else {}
    if event_type in TRADE_TICKET_EVENTS:
        take_profit = list(setup.get("take_profit") or [])
        runner_zone = list((setup.get("management") or {}).get("runner_zone") or [])
        targets = take_profit[:2]
        if runner_zone:
            targets.append(runner_zone[0])
        return "\n".join(
            [
                display_symbol(symbol),
                f"方向：{paper_direction_label(event_type)}",
                f"建倉：{fmt_compact_range(setup.get('entry_zone', ['-', '-']))}",
                f"止損：{fmt_price(setup.get('stop_loss'))}",
                f"止盈：{fmt_compact_targets(targets)}",
                f"時間區：{current_session_label(presentation=presentation)}",
                "備註：進場靈活，不追價；失守就撤",
            ]
        )

    volume_ratio = tf_exec.get("volume_ratio", "-")
    lines = [
        display_symbol(symbol),
        f"{paper_action_label(event_type)}",
        f"方向：{paper_direction_label(event_type)}觀察 | 現價：{price}",
        f"關鍵價：{fmt_price(level)}",
        f"時間區：{current_session_label(presentation=presentation)}",
    ]
    if event_type in {"retest_hold_long", "retest_hold_short"}:
        lines.append(f"量比：{fmt_price(volume_ratio)}")
    if event_type in {"breakout_touch_up", "breakout_touch_down"}:
        lines.append(f"提醒權重：{fmt_price(event_risk_multiplier(event_type))}x（觀察用）")
    lines.append("備註：觀察提醒，不建倉；等首破/首跌確認層訊號。")
    return "\n".join(lines)


def build_macro_news_alert(rss_items: list[dict[str, Any]], symbols: list[str]) -> dict[str, str] | None:
    affected_symbols: list[str] = []
    merged_headlines: list[str] = []
    macro_bias = "neutral"
    for symbol in symbols:
        analysis = analyze_news_event_context(symbol, rss_items)
        if str(analysis.get("event_risk_level", "low")) != "high":
            continue
        if symbol not in affected_symbols:
            affected_symbols.append(symbol)
        if macro_bias == "neutral" and analysis.get("macro_bias") in {"risk_on", "risk_off"}:
            macro_bias = str(analysis["macro_bias"])
        for headline in analysis.get("headline_summary", [])[:2]:
            headline_text = str(headline).strip()
            if headline_text and headline_text not in merged_headlines:
                merged_headlines.append(headline_text)
    if not affected_symbols:
        return None

    canonical = []
    for headline in merged_headlines:
        normalized = " ".join(
            token
            for token in str(headline).lower().replace(",", " ").replace(".", " ").split()
            if len(token) > 3
        )
        canonical.append(normalized[:120])
    key_source = "|".join(sorted(affected_symbols) + canonical).lower()
    event_key = "macro_news:" + hashlib.sha1(key_source.encode("utf-8")).hexdigest()[:16]
    lines = [
        "[check_price]",
        "宏觀消息提醒",
        f"風險 high | {macro_bias_label(macro_bias)}",
        f"影響標的 {', '.join(sorted(affected_symbols))}",
        "先降低追價意願，等價格與結構重新同步。",
    ]
    for idx, headline in enumerate(merged_headlines[:2], start=1):
        lines.append(f"{idx}. {headline}")
    return {"event_key": event_key, "event_type": "macro_news_high_risk", "message": "\n".join(lines)}


def build_volatility_system_alert(
    result: dict[str, Any],
    protections: dict[str, Any],
    settings: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    settings = settings or {}
    if not settings.get("enable_volatility_alerts", True):
        return None
    rules = protections.get("rules", [])
    if not any(str(rule.get("code", "")) == "high_volatility_pause" for rule in rules):
        return None

    symbol = str(result.get("symbol", "UNKNOWN"))
    price = fmt_price(result.get("price"))
    returns = result.get("returns", {})
    volatility = result.get("volatility", {})
    risk_level = str(result.get("risk_level", "medium"))
    market_regime = str(result.get("short_term_signal", {}).get("market_regime", "range_or_mixed"))
    plan = result.get("long_short_plan", {}) or {}
    long_setup = plan.get("long_setup", {}) or {}
    short_setup = plan.get("short_setup", {}) or {}
    summaries = [str(x).strip() for x in protections.get("summaries", []) if str(x).strip()]
    lines = [
        "[check_price]",
        f"{symbol} 大波動提醒",
        f"現價 {price}",
        f"5m 漲跌 {fmt_price(returns.get('5m', '-'))}%",
        f"24h 波動 {fmt_price(volatility.get('realized_24h', '-'))}%",
        f"風險 {risk_level} | 結構 {market_regime}",
        "現在先停看，不追價；若要出手，只看下面兩套關鍵價。",
    ]
    if long_setup:
        long_tp = (long_setup.get("take_profit") or [None])[0]
        lines.append(
            "上方劇本 "
            f"轉強 {fmt_price(long_setup.get('trigger_price'))} | "
            f"失效 {fmt_price(long_setup.get('stop_loss'))} | "
            f"目標1 {fmt_price(long_tp)}"
        )
    if short_setup:
        short_tp = (short_setup.get("take_profit") or [None])[0]
        lines.append(
            "下方劇本 "
            f"轉弱 {fmt_price(short_setup.get('trigger_price'))} | "
            f"失效 {fmt_price(short_setup.get('stop_loss'))} | "
            f"目標1 {fmt_price(short_tp)}"
        )
    if summaries:
        lines.append("原因：" + "；".join(summaries[:2]))
    return {"event_key": f"volatility_alert:{symbol}", "event_type": "high_volatility_alert", "message": "\n".join(lines)}


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    telegram_script = Path(args.telegram_script)
    state_db = Path(args.state_db)
    reports_dir = Path("reports")
    state_conn = ensure_state_db(state_db)
    try:
        while True:
            try:
                config = load_config(config_path)
                command_count = process_telegram_commands(
                    conn=state_conn,
                    config_path=config_path,
                    timeout=args.timeout,
                    llama_mode=args.llama,
                    llama_model=args.llama_model,
                    quote=args.quote,
                    reports_dir=reports_dir,
                    telegram_script=telegram_script,
                )
                sent = run_cycle(
                    config=config,
                    state_conn=state_conn,
                    timeout=args.timeout,
                    llama_mode=args.llama,
                    llama_model=args.llama_model,
                    quote=args.quote,
                    telegram_script=telegram_script,
                )
                print(f"[{utc_now().isoformat()}] cycle complete, alerts_sent={sent}, commands_processed={command_count}")
                if args.once:
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"[{utc_now().isoformat()}] cycle_error error={exc}")
                if args.once:
                    raise
            time.sleep(args.interval_seconds)
    finally:
        state_conn.close()


if __name__ == "__main__":
    main()
