from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from event_types import event_role


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def protection_defaults() -> dict[str, Any]:
    return {
        "enable": True,
        "high_volatility_pause": {
            "enabled": True,
            "spike_5m_pct": 1.8,
            "realized_24h_pct": 4.5,
        },
        "countertrend_pause": {
            "enabled": True,
            "block_long_in_bear_trend": True,
            "block_short_in_bull_trend": True,
        },
        "loss_streak_cooldown": {
            "enabled": True,
            "horizon_label": "1h",
            "loss_streak_count": 3,
            "cooldown_hours": 6,
            "lookback_rows": 12,
        },
    }


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_protection_config(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = protection_defaults()
    if not config:
        return cfg
    return _merge_dict(cfg, config)


def _build_result(
    pause_long: bool,
    pause_short: bool,
    hard_block: bool,
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    active = bool(rules)
    blocks_new_entries = hard_block or (pause_long and pause_short)
    summaries = [str(rule.get("summary", "")).strip() for rule in rules if str(rule.get("summary", "")).strip()]
    return {
        "active": active,
        "pause_long": pause_long,
        "pause_short": pause_short,
        "hard_block": hard_block,
        "blocks_new_entries": blocks_new_entries,
        "rules": rules,
        "summaries": summaries,
        "status": "guarded" if active else "normal",
    }


def evaluate_market_protections(
    symbol: str,
    returns: dict[str, float],
    volatility: dict[str, float],
    short_term_signal: dict[str, Any],
    risk_level: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = normalize_protection_config(config)
    if not cfg.get("enable", True):
        return _build_result(False, False, False, [])

    rules: list[dict[str, Any]] = []
    pause_long = False
    pause_short = False
    hard_block = False

    market_regime = str(short_term_signal.get("market_regime", "range_or_mixed"))

    hv_cfg = cfg.get("high_volatility_pause", {})
    if hv_cfg.get("enabled", True):
        spike_5m_pct = float(hv_cfg.get("spike_5m_pct", 1.8))
        realized_24h_pct = float(hv_cfg.get("realized_24h_pct", 4.5))
        spike_hit = abs(float(returns.get("5m", 0.0))) >= spike_5m_pct
        realized_hit = float(volatility.get("realized_24h", 0.0)) >= realized_24h_pct
        noisy_hit = risk_level in {"high", "extreme"} and market_regime == "range_or_mixed"
        if spike_hit or realized_hit or noisy_hit:
            pause_long = True
            pause_short = True
            hard_block = True
            details: list[str] = []
            if spike_hit:
                details.append(f"5m 波動 {returns.get('5m')}%")
            if realized_hit:
                details.append(f"24h 波動 {volatility.get('realized_24h')}%")
            if noisy_hit:
                details.append("高風險震盪盤")
            rules.append(
                {
                    "code": "high_volatility_pause",
                    "scope": "both",
                    "summary": f"{symbol} 高波動保護啟動，暫停新倉：{' / '.join(details)}",
                }
            )

    ct_cfg = cfg.get("countertrend_pause", {})
    if ct_cfg.get("enabled", True):
        if market_regime == "bear_trend" and ct_cfg.get("block_long_in_bear_trend", True):
            pause_long = True
            rules.append(
                {
                    "code": "countertrend_long_pause",
                    "scope": "long",
                    "summary": f"{symbol} 處於 bear_trend，暫停逆勢做多確認訊號。",
                }
            )
        if market_regime == "bull_trend" and ct_cfg.get("block_short_in_bull_trend", True):
            pause_short = True
            rules.append(
                {
                    "code": "countertrend_short_pause",
                    "scope": "short",
                    "summary": f"{symbol} 處於 bull_trend，暫停逆勢做空確認訊號。",
                }
            )

    return _build_result(pause_long, pause_short, hard_block, rules)


def _recent_direction_rows(
    conn: sqlite3.Connection,
    symbol: str,
    direction: str,
    horizon_label: str,
    lookback_rows: int,
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            e.event_type,
            e.direction,
            e.sent_at,
            p.horizon_label,
            p.close_return_pct,
            p.window_end
        FROM alert_event_performance p
        JOIN alert_events e ON e.id = p.event_id
        WHERE e.symbol = ?
          AND e.direction = ?
          AND p.horizon_label = ?
        ORDER BY p.window_end DESC
        LIMIT ?
        """,
        (symbol, direction, horizon_label, lookback_rows),
    ).fetchall()
    return [row for row in rows if event_role(str(row["event_type"])) == "actionable"]


def evaluate_performance_protections(
    conn: sqlite3.Connection | None,
    symbol: str,
    config: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    cfg = normalize_protection_config(config)
    ls_cfg = cfg.get("loss_streak_cooldown", {})
    if (
        not cfg.get("enable", True)
        or not ls_cfg.get("enabled", True)
        or conn is None
        or not hasattr(conn, "execute")
    ):
        return _build_result(False, False, False, [])

    now = now or utc_now()
    horizon_label = str(ls_cfg.get("horizon_label", "1h"))
    loss_streak_count = int(ls_cfg.get("loss_streak_count", 3))
    cooldown_hours = int(ls_cfg.get("cooldown_hours", 6))
    lookback_rows = int(ls_cfg.get("lookback_rows", 12))

    pause_long = False
    pause_short = False
    rules: list[dict[str, Any]] = []

    for direction, side_name in (("up", "long"), ("down", "short")):
        rows = _recent_direction_rows(conn, symbol, direction, horizon_label, lookback_rows)
        if len(rows) < loss_streak_count:
            continue
        streak = rows[:loss_streak_count]
        if not all(float(row["close_return_pct"]) < 0 for row in streak):
            continue
        latest_end = dt.datetime.fromisoformat(str(streak[0]["window_end"]))
        resume_at = latest_end + dt.timedelta(hours=cooldown_hours)
        if now >= resume_at:
            continue
        if direction == "up":
            pause_long = True
        else:
            pause_short = True
        rules.append(
            {
                "code": f"{side_name}_loss_streak_cooldown",
                "scope": side_name,
                "summary": (
                    f"{symbol} {side_name} 方向最近 {loss_streak_count} 筆 {horizon_label} 連敗，"
                    f"冷卻至 {resume_at.strftime('%Y-%m-%d %H:%M UTC')}"
                ),
            }
        )

    return _build_result(pause_long, pause_short, False, rules)


def merge_protections(*items: dict[str, Any]) -> dict[str, Any]:
    pause_long = any(bool(item.get("pause_long")) for item in items)
    pause_short = any(bool(item.get("pause_short")) for item in items)
    hard_block = any(bool(item.get("hard_block")) for item in items)
    rules: list[dict[str, Any]] = []
    for item in items:
        for rule in item.get("rules", []):
            if rule not in rules:
                rules.append(rule)
    return _build_result(pause_long, pause_short, hard_block, rules)


def apply_protections_to_decision(
    decision_data: dict[str, Any],
    protections: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(decision_data)
    warnings = [str(x) for x in updated.get("warnings", [])]
    for summary in protections.get("summaries", []):
        if summary not in warnings:
            warnings.append(summary)
    updated["warnings"] = warnings
    updated["protections"] = protections
    if protections.get("hard_block"):
        updated["decision"] = "avoid"
        return updated
    if protections.get("active") and updated.get("decision") == "scale_in_test":
        updated["decision"] = "watch"
    return updated


def protection_summary_text(protections: dict[str, Any]) -> str:
    summaries = [str(x).strip() for x in protections.get("summaries", []) if str(x).strip()]
    if not summaries:
        return "保護層：目前未啟動。"
    return "保護層：" + "；".join(summaries)
