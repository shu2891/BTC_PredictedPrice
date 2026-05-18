#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

from market_alert_daemon import (
    apply_protection_layer_to_events,
    build_events,
    default_symbol_state,
    event_direction,
    extract_risk_levels,
    load_config,
)
from protections import evaluate_market_protections, normalize_protection_config
from shadow_mode import (
    Candle,
    build_rule_based_decision,
    build_swing_long_short_plan,
    build_swing_mtf_score,
    build_swing_signal,
    calc_atr_pct,
    calc_rsi,
    ema,
    normalize_symbol,
    pct_change_from_closes,
    realized_vol_pct,
    summarize_funding_summary,
    summarize_timeframe,
)


USER_AGENT = "historical-replay-backtest/1.0"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
INTERVAL_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}
HORIZON_MINUTES = {
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "24h": 1440,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay alert rules on longer Binance history.")
    parser.add_argument("--config", default="watchlist.json")
    parser.add_argument("--symbols", nargs="*", help="Override symbols from watchlist, e.g. BTC ETH")
    parser.add_argument("--quote", default="USDT")
    parser.add_argument("--start-date", required=True, help="UTC date in YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="UTC date in YYYY-MM-DD")
    parser.add_argument("--horizons", default="15m,1h,4h,24h")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--macro-calendar", default="macro_event_calendar_2025_2026.json")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--output-md", default="")
    return parser.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_utc_date(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)


def interval_minutes(label: str) -> int:
    if label not in HORIZON_MINUTES:
        raise ValueError(f"Unsupported horizon: {label}")
    return HORIZON_MINUTES[label]


def to_iso(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).isoformat()


def load_macro_calendar(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    calendar: list[dict[str, Any]] = []
    for item in raw:
        ts = dt.datetime.fromisoformat(str(item["timestamp_utc"]))
        calendar.append(
            {
                "name": str(item["name"]),
                "label": str(item.get("label", item["name"])),
                "severity": str(item.get("severity", "medium")).lower(),
                "pre_minutes": int(item.get("pre_minutes", 360)),
                "post_minutes": int(item.get("post_minutes", 180)),
                "timestamp_utc": ts,
                "timestamp_ms": int(ts.timestamp() * 1000),
            }
        )
    return calendar


def synthesize_macro_news_summary(current_ts_ms: int, calendar: list[dict[str, Any]]) -> dict[str, Any]:
    active: list[dict[str, Any]] = []
    for item in calendar:
        start_ms = int(item["timestamp_ms"]) - int(item["pre_minutes"]) * 60 * 1000
        end_ms = int(item["timestamp_ms"]) + int(item["post_minutes"]) * 60 * 1000
        if start_ms <= current_ts_ms <= end_ms:
            active.append(item)

    if not active:
        return {
            "sentiment": "neutral",
            "score": 0.0,
            "confidence": 0.4,
            "conflict": False,
            "macro_bias": "neutral",
            "macro_confidence": 0.35,
            "macro_conflict": False,
            "event_risk_level": "low",
            "event_risk_score": 1,
            "expires_hours": 0,
            "key_event_headlines": [],
        }

    max_severity = "medium"
    if any(item["severity"] == "high" for item in active):
        max_severity = "high"
    event_risk_score = 10 if max_severity == "high" else 6
    max_end_ms = max(int(item["timestamp_ms"]) + int(item["post_minutes"]) * 60 * 1000 for item in active)
    expires_hours = max(1, round((max_end_ms - current_ts_ms) / (60 * 60 * 1000)))
    return {
        "sentiment": "neutral",
        "score": 0.0,
        "confidence": 0.4,
        "conflict": False,
        "macro_bias": "neutral",
        "macro_confidence": 0.75,
        "macro_conflict": False,
        "event_risk_level": max_severity,
        "event_risk_score": event_risk_score,
        "expires_hours": expires_hours,
        "key_event_headlines": [str(item["label"]) for item in active[:3]],
    }


def load_funding_history(
    symbol: str,
    start_dt: dt.datetime,
    end_dt: dt.datetime,
    timeout: int,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    settings = settings or {}
    if not bool(settings.get("enabled", False)):
        return []
    symbols = {str(item).upper() for item in settings.get("symbols", ["BTCUSDT", "ETHUSDT"])}
    if symbol.upper() not in symbols:
        return []

    headers = {"User-Agent": USER_AGENT}
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    cursor = start_ms
    rows: list[dict[str, Any]] = []
    while cursor < end_ms:
        resp = requests.get(
            BINANCE_FUNDING_URL,
            params={
                "symbol": symbol.upper(),
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for item in batch:
            rows.append(
                {
                    "fundingTime": int(item["fundingTime"]),
                    "fundingRate": float(item["fundingRate"]),
                }
            )
        cursor = int(batch[-1]["fundingTime"]) + 1
        if len(batch) < 1000:
            break
        time.sleep(0.05)
    return rows


def summarize_replay_funding(
    symbol: str,
    current_ts_ms: int,
    funding_history: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = settings or {}
    if not bool(settings.get("enabled", False)):
        return {
            "enabled": False,
            "available": False,
            "provider": "binance_futures",
            "symbol": symbol,
            "signal": "NORMAL",
            "bias": "neutral",
            "current_rate": 0.0,
            "current_rate_pct": 0.0,
            "avg_recent_rate": 0.0,
            "avg_recent_rate_pct": 0.0,
            "summary": ["Funding overlay 未啟用。"],
        }
    if not funding_history:
        return {
            "enabled": True,
            "available": False,
            "provider": "binance_futures",
            "symbol": symbol,
            "signal": "NORMAL",
            "bias": "neutral",
            "current_rate": 0.0,
            "current_rate_pct": 0.0,
            "avg_recent_rate": 0.0,
            "avg_recent_rate_pct": 0.0,
            "summary": ["Funding 歷史資料不可用，回退中性。"],
        }

    timestamps = [row["fundingTime"] for row in funding_history]
    idx = bisect_right(timestamps, current_ts_ms) - 1
    if idx < 0:
        return {
            "enabled": True,
            "available": False,
            "provider": "binance_futures",
            "symbol": symbol,
            "signal": "NORMAL",
            "bias": "neutral",
            "current_rate": 0.0,
            "current_rate_pct": 0.0,
            "avg_recent_rate": 0.0,
            "avg_recent_rate_pct": 0.0,
            "summary": ["Funding 視窗前沒有有效資料，回退中性。"],
        }

    current_rate = float(funding_history[idx]["fundingRate"])
    history_limit = max(3, min(int(settings.get("history_limit", 12)), 50))
    recent = funding_history[max(0, idx - history_limit + 1) : idx + 1]
    avg_recent_rate = sum(float(item["fundingRate"]) for item in recent) / len(recent)
    summary = summarize_funding_summary(symbol, current_rate, avg_recent_rate, settings)
    summary["source"] = "historical_binance_funding"
    return summary


def fetch_klines_range(symbol: str, interval: str, start_dt: dt.datetime, end_dt: dt.datetime, timeout: int) -> list[Candle]:
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    step_ms = INTERVAL_MS[interval]
    cursor = start_ms
    candles: list[Candle] = []
    while cursor < end_ms:
        resp = requests.get(
            BINANCE_KLINES_URL,
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        for row in data:
            candles.append(
                Candle(
                    ts_ms=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        cursor = int(data[-1][0]) + step_ms
        time.sleep(0.03)
    return candles


def build_trend_from_1h(candles_1h: list[Candle]) -> tuple[dict[str, Any], dict[str, float], float]:
    closes_1h = [c.close for c in candles_1h]
    price = closes_1h[-1]
    ema20 = ema(closes_1h, 20)
    ema50 = ema(closes_1h, 50)
    rsi14 = calc_rsi(closes_1h, 14)
    trend_view = "mixed"
    if ema20 > ema50 and rsi14 >= 52:
        trend_view = "bullish"
    elif ema20 < ema50 and rsi14 <= 48:
        trend_view = "bearish"
    last_vol = candles_1h[-1].volume
    prev24 = [c.volume for c in candles_1h[-25:-1]]
    avg_vol = sum(prev24) / len(prev24) if prev24 else last_vol
    volume_ratio = round(last_vol / avg_vol, 3) if avg_vol else 1.0
    trend = {
        "ema20": round(ema20, 4),
        "ema50": round(ema50, 4),
        "ema20_gt_ema50": ema20 > ema50,
        "rsi14": round(rsi14, 3),
        "trend_view": trend_view,
    }
    volatility = {
        "atr_pct": round(calc_atr_pct(candles_1h, 14), 3),
        "realized_24h": 0.0,
    }
    return trend, volatility, volume_ratio


def derive_actionable_levels_from_5m(
    candles_5m: list[Candle],
    price: float,
    volatility: dict[str, float],
    returns: dict[str, float],
    trend_view: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reference_candles = candles_5m[:-1] if len(candles_5m) >= 2 else candles_5m
    recent_4h = reference_candles[-48:] if len(reference_candles) >= 48 else reference_candles
    high_4h = max(c.high for c in recent_4h)
    low_4h = min(c.low for c in recent_4h)
    range_pct_4h = ((high_4h - low_4h) / price * 100) if price > 0 else 0.0
    atr_pct = max(volatility["atr_pct"], 0.1)
    atr_abs = max(price * atr_pct / 100, price * 0.004)
    range_abs = max(high_4h - low_4h, price * 0.002)
    is_sideways = range_pct_4h <= max(1.8 * atr_pct, 1.2) and abs(returns["1h"]) <= 0.65
    breakout_up = high_4h * 1.0015
    breakout_down = low_4h * 0.9985
    execution_band = max(price * 0.0015, min(0.55 * atr_abs, 0.22 * range_abs))
    long_ready_zone = [breakout_up - execution_band, breakout_up]
    short_ready_zone = [breakout_down, breakout_down + execution_band]
    retest_band = max(price * 0.001, min(0.35 * atr_abs, 0.14 * range_abs))
    long_retest_zone = [breakout_up - retest_band, breakout_up + 0.18 * retest_band]
    short_retest_zone = [breakout_down - 0.18 * retest_band, breakout_down + retest_band]
    noise_band = range_abs * 0.3
    noise_low = low_4h + noise_band
    noise_high = high_4h - noise_band
    if is_sideways:
        if range_pct_4h <= 0.9 and atr_pct <= 1.2:
            timing = {"window": "約 12-36 小時", "confidence": "中", "reason": "波動壓縮明顯，通常需要時間累積方向。"}
        else:
            timing = {"window": "約 6-18 小時", "confidence": "中", "reason": "區間震盪中，接近箱體邊界時容易出方向。"}
    elif trend_view in {"bullish", "bearish"} and max(abs(returns["1h"]), abs(returns["4h"])) >= 1.2:
        timing = {"window": "約 1-8 小時", "confidence": "中高", "reason": "已出現趨勢動能，短時間續行或回檔機率較高。"}
    else:
        timing = {"window": "約 8-24 小時", "confidence": "中低", "reason": "方向仍在確認，需等待價格突破關鍵位。"}
    market_state = {
        "is_sideways": is_sideways,
        "state": "sideways" if is_sideways else "trending",
        "range_4h": [round(low_4h, 4), round(high_4h, 4)],
        "range_pct_4h": round(range_pct_4h, 3),
    }
    actionable_levels = {
        "range_low": round(low_4h, 4),
        "range_high": round(high_4h, 4),
        "breakout_up": round(breakout_up, 4),
        "breakout_down": round(breakout_down, 4),
        "long_ready_zone": [round(long_ready_zone[0], 4), round(long_ready_zone[1], 4)],
        "short_ready_zone": [round(short_ready_zone[0], 4), round(short_ready_zone[1], 4)],
        "long_retest_zone": [round(long_retest_zone[0], 4), round(long_retest_zone[1], 4)],
        "short_retest_zone": [round(short_retest_zone[0], 4), round(short_retest_zone[1], 4)],
        "noise_zone": [round(noise_low, 4), round(noise_high, 4)],
        "execution_band_pct": round(execution_band / price * 100, 3) if price > 0 else 0.0,
        "timing_window": timing["window"],
        "timing_confidence": timing["confidence"],
        "timing_reason": timing["reason"],
    }
    return market_state, actionable_levels


def build_replay_analysis(
    symbol: str,
    candles_5m: list[Candle],
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    macro_calendar: list[dict[str, Any]],
    profile: str,
    protection_settings: dict[str, Any] | None = None,
    funding_history: list[dict[str, Any]] | None = None,
    funding_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    closes_5m = [c.close for c in candles_5m]
    closes_1h = [c.close for c in candles_1h]
    price = closes_5m[-1]
    returns = {
        "5m": round(pct_change_from_closes(closes_5m, 1), 3),
        "1h": round(pct_change_from_closes(closes_5m, 12), 3),
        "4h": round(pct_change_from_closes(closes_5m, 48), 3),
        "24h": round(pct_change_from_closes(closes_5m, 288), 3),
    }
    trend, volatility, volume_ratio = build_trend_from_1h(candles_1h)
    vol_window = min(288, len(closes_5m) - 1)
    rv_24h = realized_vol_pct(closes_5m, vol_window) if vol_window >= 30 else 0.0
    if rv_24h == 0.0 and len(closes_1h) > 24:
        rv_24h = realized_vol_pct(closes_1h, 24)
    volatility["realized_24h"] = round(rv_24h, 3)
    market_state, actionable_levels = derive_actionable_levels_from_5m(
        candles_5m=candles_5m,
        price=price,
        volatility=volatility,
        returns=returns,
        trend_view=str(trend["trend_view"]),
    )
    tf5 = summarize_timeframe(candles_5m[-300:], "5m")
    tf15m = summarize_timeframe(candles_15m[-300:], "15m")
    tf4h = summarize_timeframe(candles_4h[-300:], "4h")
    false_breakout = {
        "false_breakout_up": any(c.high >= actionable_levels["breakout_up"] and c.close < actionable_levels["breakout_up"] for c in candles_5m[-3:]),
        "false_breakout_down": any(c.low <= actionable_levels["breakout_down"] and c.close > actionable_levels["breakout_down"] for c in candles_5m[-3:]),
    }
    onchain_summary = {
        "enabled": False,
        "available": False,
        "provider": "coinmetrics",
        "asset": None,
        "bias": "neutral",
        "confidence": "low",
        "summary": ["回放未接歷史鏈上 time series，鏈上濾網以 metadata_only 處理。"],
        "metrics": {},
    }
    tf1h = summarize_timeframe(candles_1h[-300:], "1h")
    funding_summary = summarize_replay_funding(symbol, candles_5m[-1].ts_ms, funding_history or [], funding_settings)
    swing_mtf_score = build_swing_mtf_score(tf15m, tf1h, tf4h)
    short_term_signal = build_swing_signal(
        tf1h,
        tf4h,
        price,
        false_breakout,
        returns,
        onchain_summary,
        funding_summary=funding_summary,
        swing_mtf_score=swing_mtf_score,
    )
    long_short_plan = build_swing_long_short_plan(
        price=price,
        atr_pct=volatility["atr_pct"],
        levels=actionable_levels,
        tf1h=tf1h,
        tf4h=tf4h,
        swing_signal=short_term_signal,
        onchain_summary=onchain_summary,
    )
    macro_context = synthesize_macro_news_summary(candles_5m[-1].ts_ms, macro_calendar)
    decision_data = build_rule_based_decision(
        symbol=symbol,
        price=price,
        returns=returns,
        volatility=volatility,
        trend=trend,
        volume_ratio=volume_ratio,
        news_summary=macro_context,
        profile=profile,
    )
    protections = evaluate_market_protections(
        symbol=symbol,
        returns=returns,
        volatility=volatility,
        short_term_signal=short_term_signal,
        risk_level=str(decision_data["risk_level"]),
        config=protection_settings,
    )
    return {
        "symbol": symbol,
        "price": round(price, 6),
        "returns": returns,
        "volatility": volatility,
        "trend": trend,
        "volume": {"vol_ratio": volume_ratio},
        "macro_context": macro_context,
        "market_state": market_state,
        "actionable_levels": actionable_levels,
        "timeframe_view": {"5m": tf5, "15m": tf15m, "1h": tf1h, "4h": tf4h},
        "short_term_signal": short_term_signal,
        "long_short_plan": long_short_plan,
        "protections": protections,
        "onchain_summary": onchain_summary,
        "funding_summary": funding_summary,
    }


def evaluate_event_window(event: dict[str, Any], future_candles: list[Candle], horizon_label: str) -> dict[str, Any] | None:
    minutes = interval_minutes(horizon_label)
    if not future_candles:
        return None
    bars_needed = max(1, minutes // 5)
    window = future_candles[:bars_needed]
    if len(window) < bars_needed:
        return None
    entry = float(event["price"])
    direction = event["direction"]
    highs = [c.high for c in window]
    lows = [c.low for c in window]
    close_price = window[-1].close
    if direction == "up":
        close_return_pct = (close_price / entry - 1.0) * 100.0
        max_runup_pct = (max(highs) / entry - 1.0) * 100.0
        max_drawdown_pct = (min(lows) / entry - 1.0) * 100.0
        tp1_hit = int(event["take_profit_1"] is not None and max(highs) >= float(event["take_profit_1"]))
        tp2_hit = int(event["take_profit_2"] is not None and max(highs) >= float(event["take_profit_2"]))
        stop_loss_hit = int(event["stop_loss"] is not None and min(lows) <= float(event["stop_loss"]))
    elif direction == "down":
        close_return_pct = (entry / close_price - 1.0) * 100.0
        max_runup_pct = (entry / min(lows) - 1.0) * 100.0
        max_drawdown_pct = -((max(highs) / entry - 1.0) * 100.0)
        tp1_hit = int(event["take_profit_1"] is not None and min(lows) <= float(event["take_profit_1"]))
        tp2_hit = int(event["take_profit_2"] is not None and min(lows) <= float(event["take_profit_2"]))
        stop_loss_hit = int(event["stop_loss"] is not None and max(highs) >= float(event["stop_loss"]))
    else:
        close_return_pct = (close_price / entry - 1.0) * 100.0
        max_runup_pct = (max(highs) / entry - 1.0) * 100.0
        max_drawdown_pct = (min(lows) / entry - 1.0) * 100.0
        tp1_hit = 0
        tp2_hit = 0
        stop_loss_hit = 0
    direction_hit = int(close_return_pct > 0) if direction == "up" else int(close_return_pct > 0) if direction == "down" else None
    return {
        "horizon_label": horizon_label,
        "bars": len(window),
        "close_price": round(close_price, 6),
        "close_return_pct": round(close_return_pct, 3),
        "max_runup_pct": round(max_runup_pct, 3),
        "max_drawdown_pct": round(max_drawdown_pct, 3),
        "direction_hit": direction_hit,
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
        "stop_loss_hit": stop_loss_hit,
    }


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_report(events: list[dict[str, Any]], horizons: list[str], start_dt: dt.datetime, end_dt: dt.datetime) -> dict[str, Any]:
    by_type = Counter(event["event_type"] for event in events)
    by_symbol = Counter(event["symbol"] for event in events)
    by_protection = Counter(event.get("protection_status", "unknown") for event in events)
    by_horizon_type: dict[str, dict[str, Any]] = {}
    for horizon in horizons:
        for event_type in sorted(by_type.keys()):
            evals = [e["evaluations"][horizon] for e in events if horizon in e["evaluations"] and e["event_type"] == event_type]
            if not evals:
                continue
            dir_hits = [int(e["direction_hit"]) for e in evals if e["direction_hit"] is not None]
            by_horizon_type[f"{horizon}:{event_type}"] = {
                "count": len(evals),
                "direction_hit_rate": (sum(dir_hits) / len(dir_hits)) if dir_hits else None,
                "avg_close_return_pct": avg([float(e["close_return_pct"]) for e in evals]),
                "avg_max_runup_pct": avg([float(e["max_runup_pct"]) for e in evals]),
                "avg_max_drawdown_pct": avg([float(e["max_drawdown_pct"]) for e in evals]),
                "tp1_rate": avg([float(e["tp1_hit"]) for e in evals]),
                "tp2_rate": avg([float(e["tp2_hit"]) for e in evals]),
                "stop_loss_rate": avg([float(e["stop_loss_hit"]) for e in evals]),
            }
    overall_by_horizon: dict[str, Any] = {}
    for horizon in horizons:
        evals = [e["evaluations"][horizon] for e in events if horizon in e["evaluations"]]
        if not evals:
            continue
        dir_hits = [int(e["direction_hit"]) for e in evals if e["direction_hit"] is not None]
        overall_by_horizon[horizon] = {
            "count": len(evals),
            "direction_hit_rate": (sum(dir_hits) / len(dir_hits)) if dir_hits else None,
            "avg_close_return_pct": avg([float(e["close_return_pct"]) for e in evals]),
            "avg_max_runup_pct": avg([float(e["max_runup_pct"]) for e in evals]),
            "avg_max_drawdown_pct": avg([float(e["max_drawdown_pct"]) for e in evals]),
        }
    return {
        "generated_at": utc_now().isoformat(),
        "start_date_utc": start_dt.date().isoformat(),
        "end_date_utc": end_dt.date().isoformat(),
        "total_events": len(events),
        "event_distribution": dict(by_type),
        "symbol_distribution": dict(by_symbol),
        "protection_distribution": dict(by_protection),
        "overall_by_horizon": overall_by_horizon,
        "by_horizon_type": by_horizon_type,
    }


def write_md(path: Path, report: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# 歷史重播回測報告")
    lines.append("")
    lines.append(f"- 產生時間(UTC): `{report['generated_at']}`")
    lines.append(f"- 回測區間(UTC): `{report['start_date_utc']}` ~ `{report['end_date_utc']}`")
    lines.append(f"- 事件總數: `{report['total_events']}`")
    if report.get("macro_calendar_mode"):
        lines.append(f"- 宏觀日曆模式: `{report['macro_calendar_mode']}`")
    if report.get("protection_mode"):
        lines.append(f"- 保護層模式: `{report['protection_mode']}`")
    lines.append("")
    lines.append("## 事件分布")
    if not report["event_distribution"]:
        lines.append("- 無事件")
    else:
        for event_type, count in sorted(report["event_distribution"].items()):
            lines.append(f"- `{event_type}`: `{count}`")
    lines.append("")
    lines.append("## 幣種分布")
    if not report["symbol_distribution"]:
        lines.append("- 無資料")
    else:
        for symbol, count in sorted(report["symbol_distribution"].items()):
            lines.append(f"- `{symbol}`: `{count}`")
    lines.append("")
    lines.append("## 保護層狀態分布")
    if not report.get("protection_distribution"):
        lines.append("- 無資料")
    else:
        for status, count in sorted(report["protection_distribution"].items()):
            lines.append(f"- `{status}`: `{count}`")
    lines.append("")
    lines.append("## 各 Horizon 整體表現")
    if not report["overall_by_horizon"]:
        lines.append("- 無可評估資料")
    else:
        lines.append("| Horizon | 筆數 | 方向命中率 | 平均收盤報酬 | 平均最大順向 | 平均最大逆向 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for horizon, stats in report["overall_by_horizon"].items():
            lines.append(
                f"| {horizon} | {stats['count']} | {pct(stats['direction_hit_rate'])} | "
                f"{stats['avg_close_return_pct']:.3f}% | {stats['avg_max_runup_pct']:.3f}% | {stats['avg_max_drawdown_pct']:.3f}% |"
            )
    lines.append("")
    lines.append("## 各事件類型表現")
    if not report["by_horizon_type"]:
        lines.append("- 無可評估資料")
    else:
        lines.append("| Horizon | Event | 筆數 | 方向命中率 | 平均收盤報酬 | TP1 | TP2 | SL |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
        for key, stats in sorted(report["by_horizon_type"].items()):
            horizon, event_type = key.split(":", 1)
            tp1 = "n/a" if stats["tp1_rate"] is None else pct(stats["tp1_rate"])
            tp2 = "n/a" if stats["tp2_rate"] is None else pct(stats["tp2_rate"])
            sl = "n/a" if stats["stop_loss_rate"] is None else pct(stats["stop_loss_rate"])
            lines.append(
                f"| {horizon} | {event_type} | {stats['count']} | {pct(stats['direction_hit_rate'])} | "
                f"{stats['avg_close_return_pct']:.3f}% | {tp1} | {tp2} | {sl} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    config = load_config(Path(args.config))
    symbols = [normalize_symbol(s, args.quote) for s in (args.symbols or config.get("symbols", []))]
    if not symbols:
        raise RuntimeError("No symbols provided.")
    settings = dict(config.get("alerts", {}))
    profile = str(config.get("risk_profile", "conservative"))
    protection_settings = normalize_protection_config(config.get("protections", {}))
    funding_settings = dict(config.get("funding", {}))
    start_dt = parse_utc_date(args.start_date)
    end_dt = parse_utc_date(args.end_date)
    if end_dt <= start_dt:
        raise ValueError("end-date must be after start-date")
    max_horizon_minutes = max(interval_minutes(h) for h in args.horizons.split(",") if h.strip())
    warmup_dt = start_dt - dt.timedelta(days=12)
    replay_end_dt = end_dt - dt.timedelta(minutes=max_horizon_minutes)
    horizons = [h.strip() for h in args.horizons.split(",") if h.strip()]
    macro_calendar = load_macro_calendar(Path(args.macro_calendar))
    range_label = f"{start_dt.date().isoformat()}_{end_dt.date().isoformat()}"
    output_json = Path(args.output_json) if args.output_json else Path("reports") / f"historical_replay_{range_label}.json"
    output_md = Path(args.output_md) if args.output_md else Path(f"歷史重播回測報告_{range_label}.md")
    output_json.parent.mkdir(parents=True, exist_ok=True)

    events: list[dict[str, Any]] = []
    for symbol in symbols:
        candles_5m = fetch_klines_range(symbol, "5m", warmup_dt, end_dt, args.timeout)
        candles_15m = fetch_klines_range(symbol, "15m", warmup_dt, end_dt, args.timeout)
        candles_1h = fetch_klines_range(symbol, "1h", warmup_dt, end_dt, args.timeout)
        candles_4h = fetch_klines_range(symbol, "4h", warmup_dt, end_dt, args.timeout)
        funding_history = load_funding_history(symbol, warmup_dt, end_dt, args.timeout, funding_settings)
        ts_15m = [c.ts_ms for c in candles_15m]
        ts_1h = [c.ts_ms for c in candles_1h]
        ts_4h = [c.ts_ms for c in candles_4h]
        cooldown_by_key: dict[str, int] = {}
        symbol_state = default_symbol_state(symbol)
        for idx, candle in enumerate(candles_5m):
            current_dt = dt.datetime.fromtimestamp(candle.ts_ms / 1000, tz=dt.timezone.utc)
            if current_dt < start_dt or current_dt > replay_end_dt:
                continue
            i15 = bisect_right(ts_15m, candle.ts_ms) - 1
            i1 = bisect_right(ts_1h, candle.ts_ms) - 1
            i4 = bisect_right(ts_4h, candle.ts_ms) - 1
            if i15 < 49 or i1 < 49 or i4 < 49 or idx < 288:
                continue
            analysis = build_replay_analysis(
                symbol=symbol,
                candles_5m=candles_5m[: idx + 1],
                candles_15m=candles_15m[: i15 + 1],
                candles_1h=candles_1h[: i1 + 1],
                candles_4h=candles_4h[: i4 + 1],
                macro_calendar=macro_calendar,
                profile=profile,
                protection_settings=protection_settings,
                funding_history=funding_history,
                funding_settings=funding_settings,
            )
            replay_events, symbol_state = build_events(analysis, settings, symbol_state)
            replay_events = apply_protection_layer_to_events(replay_events, analysis.get("protections", {}))
            for event in replay_events:
                event_key = f"{symbol}:{event['event_type']}:{round(float(event['level']), 4)}"
                cooldown_minutes = int(config.get("cooldown_minutes", 30))
                last_sent_ts = cooldown_by_key.get(event_key)
                if last_sent_ts is not None and candle.ts_ms - last_sent_ts < cooldown_minutes * 60 * 1000:
                    continue
                cooldown_by_key[event_key] = candle.ts_ms
                stop_loss, tp1, tp2 = extract_risk_levels(analysis, event["event_type"])
                record = {
                    "symbol": symbol,
                    "timestamp": to_iso(candle.ts_ms),
                    "event_type": event["event_type"],
                    "direction": event_direction(event["event_type"]),
                    "level": round(float(event["level"]), 6),
                    "price": round(float(analysis["price"]), 6),
                    "stop_loss": stop_loss,
                    "take_profit_1": tp1,
                    "take_profit_2": tp2,
                    "short_term_bias": analysis["short_term_signal"]["bias"],
                    "signal_strength": analysis["short_term_signal"]["strength"],
                    "macro_event_risk_level": analysis["macro_context"].get("event_risk_level", "low"),
                    "macro_event_headlines": analysis["macro_context"].get("key_event_headlines", []),
                    "onchain_bias": analysis.get("onchain_summary", {}).get("bias", "neutral"),
                    "onchain_available": bool(analysis.get("onchain_summary", {}).get("available", False)),
                    "funding_bias": analysis.get("funding_summary", {}).get("bias", "neutral"),
                    "funding_available": bool(analysis.get("funding_summary", {}).get("available", False)),
                    "funding_signal": analysis.get("funding_summary", {}).get("signal", "NORMAL"),
                    "swing_mtf_score": analysis.get("short_term_signal", {}).get("swing_mtf_score", 50.0),
                    "swing_mtf_bias": analysis.get("short_term_signal", {}).get("swing_mtf_bias", "neutral"),
                    "protection_status": analysis.get("protections", {}).get("status", "unknown"),
                    "protection_active": bool(analysis.get("protections", {}).get("active", False)),
                    "protection_summaries": analysis.get("protections", {}).get("summaries", []),
                    "timeframe_view": analysis["timeframe_view"],
                    "actionable_levels": analysis["actionable_levels"],
                    "evaluations": {},
                }
                future = candles_5m[idx + 1 :]
                for horizon in horizons:
                    evaluation = evaluate_event_window(record, future, horizon)
                    if evaluation is not None:
                        record["evaluations"][horizon] = evaluation
                if record["evaluations"]:
                    events.append(record)

    report = build_report(events, horizons, start_dt, end_dt)
    report["macro_calendar_mode"] = "metadata_only"
    report["onchain_mode"] = "metadata_only"
    report["funding_mode"] = "historical_overlay"
    report["protection_mode"] = "market_only"
    payload = {
        "report": report,
        "meta": {
            "macro_calendar_mode": "metadata_only",
            "onchain_mode": "metadata_only",
            "funding_mode": "historical_overlay",
            "protection_mode": "market_only",
            "macro_calendar_path": args.macro_calendar,
            "macro_high_risk_event_count": sum(
                1 for event in events if event.get("macro_event_risk_level") == "high"
            ),
            "protection_active_event_count": sum(1 for event in events if event.get("protection_active")),
        },
        "events": events,
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_md(output_md, report)
    print(json.dumps({"events": len(events), "output_json": str(output_json), "output_md": str(output_md)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
