#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import math
import os
import sqlite3
import statistics
import textwrap
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from protections import apply_protections_to_decision, evaluate_market_protections, protection_summary_text


USER_AGENT = "shadow-mode-advisor/1.0"
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]
POSITIVE_WORDS = {
    "surge",
    "rally",
    "approval",
    "inflow",
    "partnership",
    "adoption",
    "bullish",
    "upgrade",
    "record",
    "gain",
    "rise",
    "突破",
    "上漲",
    "利多",
    "增長",
}
NEGATIVE_WORDS = {
    "hack",
    "lawsuit",
    "ban",
    "outflow",
    "bearish",
    "crash",
    "drop",
    "decline",
    "risk",
    "liquidation",
    "fraud",
    "監管",
    "下跌",
    "利空",
    "風險",
}
MACRO_EVENT_KEYWORDS = {
    "fed",
    "fomc",
    "powell",
    "cpi",
    "ppi",
    "inflation",
    "nonfarm",
    "nfp",
    "interest rate",
    "rate cut",
    "rate hike",
    "sec",
    "etf",
    "tariff",
    "war",
    "geopolitical",
    "liquidation",
    "liquidations",
    "美聯儲",
    "聯準會",
    "鮑威爾",
    "非農",
    "通膨",
    "降息",
    "升息",
    "關稅",
    "戰爭",
    "監管",
    "清算",
}
RISK_ON_WORDS = {
    "dovish",
    "easing",
    "rate cut",
    "stimulus",
    "approval",
    "inflow",
    "ceasefire",
    "降息",
    "寬鬆",
    "刺激",
    "核准",
    "流入",
    "停火",
    "利多",
}
RISK_OFF_WORDS = {
    "hawkish",
    "rate hike",
    "hot cpi",
    "inflation",
    "tariff",
    "war",
    "lawsuit",
    "ban",
    "outflow",
    "liquidation",
    "selloff",
    "監管",
    "升息",
    "通膨",
    "關稅",
    "戰爭",
    "清算",
    "利空",
}
GENERIC_MARKET_KEYWORDS = {
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "crypto",
    "cryptocurrency",
    "digital asset",
    "token",
    "market",
    "比特幣",
    "以太坊",
    "加密",
    "幣市",
    "市場",
}


@dataclass
class Candle:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SymbolAnalysis:
    symbol: str
    market_source: str
    timestamp: str
    price: float
    returns: dict[str, float]
    volatility: dict[str, float]
    trend: dict[str, Any]
    volume: dict[str, float]
    news: list[dict[str, Any]]
    news_summary: dict[str, Any]
    onchain_summary: dict[str, Any]
    funding_summary: dict[str, Any]
    risk_score: int
    risk_level: str
    decision: str
    confidence: float
    trend_view: str
    thesis: list[str]
    trade_plan: dict[str, Any]
    warnings: list[str]
    model_source: str
    beginner_summary: dict[str, Any]
    position_examples: dict[str, Any]
    market_state: dict[str, Any]
    actionable_levels: dict[str, Any]
    timeframe_view: dict[str, Any]
    long_short_plan: dict[str, Any]
    short_term_signal: dict[str, Any]
    protections: dict[str, Any]


class MarketDataError(Exception):
    pass


DEFAULT_LEVERAGE = 20.0
DEFAULT_ACCOUNT_ALLOCATION_PCT = 50.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Shadow mode advisor: 不下單，只輸出風險評估與交易建議。"
    )
    parser.add_argument("symbols", nargs="+", help="幣種代號，例如 BTC ETH SOL")
    parser.add_argument("--quote", default="USDT", help="報價幣別，預設 USDT")
    parser.add_argument(
        "--risk-profile",
        choices=["conservative", "balanced", "aggressive"],
        default="conservative",
    )
    parser.add_argument(
        "--llama",
        choices=["auto", "on", "off"],
        default="auto",
        help="是否呼叫本地 Ollama。auto: 可用就用，失敗自動退回規則引擎。",
    )
    parser.add_argument("--llama-model", default="llama3.1:8b")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--db-path", default="shadow_mode.db")
    parser.add_argument("--timeout", type=int, default=15)
    return parser.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fetch_json(url: str, params: dict[str, Any], timeout: int) -> Any:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def pct_change(new_value: float, old_value: float) -> float:
    if old_value == 0:
        return 0.0
    return (new_value / old_value - 1.0) * 100.0


def fetch_onchain_summary(symbol: str, timeout: int) -> dict[str, Any]:
    asset_map = {
        "BTCUSDT": "btc",
        "ETHUSDT": "eth",
        "BTC": "btc",
        "ETH": "eth",
    }
    asset = asset_map.get(symbol.upper())
    api_key = os.environ.get("COINMETRICS_API_KEY", "").strip()
    if not asset:
        return {
            "enabled": False,
            "available": False,
            "provider": "coinmetrics",
            "asset": None,
            "bias": "neutral",
            "confidence": "low",
            "summary": ["此標的暫不納入鏈上分析。"],
            "metrics": {},
        }
    if not api_key:
        return {
            "enabled": False,
            "available": False,
            "provider": "coinmetrics",
            "asset": asset,
            "bias": "neutral",
            "confidence": "low",
            "summary": ["未設定 COINMETRICS_API_KEY，鏈上分析已停用。"],
            "metrics": {},
        }

    url = "https://api.coinmetrics.io/v4/timeseries/asset-metrics"
    params = {
        "assets": asset,
        "metrics": "AdrActCnt,TxCnt",
        "frequency": "1d",
        "page_size": 21,
        "api_key": api_key,
    }
    try:
        data = fetch_json(url, params, timeout)
        rows = data.get("data", [])
        if len(rows) < 15:
            raise MarketDataError("insufficient onchain rows")
        addr_values = [float(r.get("AdrActCnt") or 0.0) for r in rows if r.get("AdrActCnt") is not None]
        tx_values = [float(r.get("TxCnt") or 0.0) for r in rows if r.get("TxCnt") is not None]
        if len(addr_values) < 15 or len(tx_values) < 15:
            raise MarketDataError("insufficient onchain metrics")

        recent_addr = statistics.fmean(addr_values[-7:])
        base_addr = statistics.fmean(addr_values[-14:-7])
        recent_tx = statistics.fmean(tx_values[-7:])
        base_tx = statistics.fmean(tx_values[-14:-7])
        addr_change = pct_change(recent_addr, base_addr)
        tx_change = pct_change(recent_tx, base_tx)

        bias = "neutral"
        confidence = "medium"
        if addr_change >= 8.0 and tx_change >= 8.0:
            bias = "bullish"
        elif addr_change <= -8.0 and tx_change <= -8.0:
            bias = "bearish"
        if abs(addr_change) >= 15.0 and abs(tx_change) >= 15.0:
            confidence = "high"
        elif abs(addr_change) < 5.0 and abs(tx_change) < 5.0:
            confidence = "low"

        summary = [
            f"活躍地址 7日均值相對前7日 {addr_change:+.1f}%",
            f"鏈上交易筆數 7日均值相對前7日 {tx_change:+.1f}%",
        ]
        if bias == "bullish":
            summary.append("鏈上活動擴張，較支持中期偏多。")
        elif bias == "bearish":
            summary.append("鏈上活動收縮，較支持中期偏空或觀望。")
        else:
            summary.append("鏈上活動中性，暫不額外放大方向判斷。")

        return {
            "enabled": True,
            "available": True,
            "provider": "coinmetrics",
            "asset": asset,
            "bias": bias,
            "confidence": confidence,
            "summary": summary,
            "metrics": {
                "active_addresses_change_7d_pct": round(addr_change, 3),
                "tx_count_change_7d_pct": round(tx_change, 3),
                "recent_active_addresses": round(recent_addr, 3),
                "recent_tx_count": round(recent_tx, 3),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": True,
            "available": False,
            "provider": "coinmetrics",
            "asset": asset,
            "bias": "neutral",
            "confidence": "low",
            "summary": [f"鏈上資料抓取失敗：{exc}"],
            "metrics": {},
        }


def fetch_binance_klines(symbol: str, interval: str, limit: int, timeout: int) -> list[Candle]:
    url = "https://api.binance.com/api/v3/klines"
    data = fetch_json(url, {"symbol": symbol, "interval": interval, "limit": limit}, timeout)
    candles: list[Candle] = []
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
    if not candles:
        raise MarketDataError(f"Binance 無資料: {symbol} {interval}")
    return candles


def fetch_bybit_klines(symbol: str, interval: str, limit: int, timeout: int) -> list[Candle]:
    bybit_interval_map = {"1m": "1", "5m": "5", "1h": "60", "4h": "240"}
    mapped = bybit_interval_map.get(interval)
    if not mapped:
        raise MarketDataError(f"Bybit 不支援 interval: {interval}")
    url = "https://api.bybit.com/v5/market/kline"
    data = fetch_json(
        url,
        {"category": "spot", "symbol": symbol, "interval": mapped, "limit": limit},
        timeout,
    )
    if data.get("retCode") != 0:
        raise MarketDataError(f"Bybit 回傳錯誤: {data.get('retMsg')}")
    rows = data.get("result", {}).get("list", [])
    candles = [
        Candle(
            ts_ms=int(r[0]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
            volume=float(r[5]),
        )
        for r in rows
    ]
    candles.sort(key=lambda x: x.ts_ms)
    if not candles:
        raise MarketDataError(f"Bybit 無資料: {symbol} {interval}")
    return candles


def fetch_klines_with_fallback(symbol: str, interval: str, limit: int, timeout: int) -> tuple[str, list[Candle]]:
    providers = [
        ("binance", fetch_binance_klines),
        ("bybit", fetch_bybit_klines),
    ]
    last_err: Exception | None = None
    for name, fn in providers:
        try:
            return name, fn(symbol, interval, limit, timeout)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise MarketDataError(f"所有交易所查詢失敗: {symbol} {interval}, last_error={last_err}")


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return values[-1]
    alpha = 2 / (period + 1)
    e = statistics.fmean(values[:period])
    for v in values[period:]:
        e = alpha * v + (1 - alpha) * e
    return e


def calc_rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for i in range(-period, 0):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-delta)
    avg_gain = statistics.fmean(gains) if gains else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_atr_pct(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        c = candles[i]
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
    atr = statistics.fmean(trs[-period:])
    last_close = candles[-1].close
    if last_close == 0:
        return 0.0
    return atr / last_close * 100


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new / old - 1) * 100


def pct_change_from_closes(closes: list[float], lookback_bars: int) -> float:
    if len(closes) <= lookback_bars:
        return 0.0
    return pct_change(closes[-1], closes[-(lookback_bars + 1)])


def realized_vol_pct(closes: list[float], window: int) -> float:
    if len(closes) < window + 1:
        return 0.0
    logs: list[float] = []
    for i in range(-window, 0):
        prev = closes[i - 1]
        curr = closes[i]
        if prev <= 0 or curr <= 0:
            continue
        logs.append(math.log(curr / prev))
    if len(logs) < 2:
        return 0.0
    std = statistics.stdev(logs)
    return std * math.sqrt(len(logs)) * 100


def rolling_vwap(candles: list[Candle], window: int) -> float:
    sample = candles[-window:] if len(candles) >= window else candles
    if not sample:
        return 0.0
    total_pv = 0.0
    total_v = 0.0
    for c in sample:
        typical = (c.high + c.low + c.close) / 3
        total_pv += typical * c.volume
        total_v += c.volume
    if total_v == 0:
        return sample[-1].close
    return total_pv / total_v


def symbol_keywords(symbol: str) -> list[str]:
    base = symbol.replace("USDT", "").replace("USD", "")
    mapping = {
        "BTC": ["bitcoin", "btc"],
        "ETH": ["ethereum", "eth", "ether"],
        "SOL": ["solana", "sol"],
        "XRP": ["ripple", "xrp"],
        "ADA": ["cardano", "ada"],
    }
    return mapping.get(base, [base.lower()])


def fetch_rss_items(timeout: int) -> list[dict[str, Any]]:
    headers = {"User-Agent": USER_AGENT}
    items: list[dict[str, Any]] = []
    for feed in RSS_FEEDS:
        try:
            r = requests.get(feed, headers=headers, timeout=timeout)
            r.raise_for_status()
            root = ET.fromstring(r.text)
        except Exception:  # noqa: BLE001
            continue
        for node in root.findall(".//item"):
            title = (node.findtext("title") or "").strip()
            desc = (node.findtext("description") or "").strip()
            link = (node.findtext("link") or "").strip()
            pub_date = (node.findtext("pubDate") or "").strip()
            items.append(
                {
                    "title": title,
                    "description": desc,
                    "link": link,
                    "pub_date": pub_date,
                }
            )
    return items


def sentiment_from_text(text: str) -> tuple[float, float]:
    lower = text.lower()
    pos_hits = sum(1 for w in POSITIVE_WORDS if w in lower)
    neg_hits = sum(1 for w in NEGATIVE_WORDS if w in lower)
    total_hits = pos_hits + neg_hits
    if total_hits == 0:
        return 0.0, 0.4
    score = (pos_hits - neg_hits) / total_hits
    confidence = min(0.9, 0.45 + total_hits * 0.1)
    return score, confidence


def analyze_news_event_context(symbol: str, rss_items: list[dict[str, Any]]) -> dict[str, Any]:
    symbol_kws = symbol_keywords(symbol)
    event_candidates: list[dict[str, Any]] = []
    risk_on_hits = 0
    risk_off_hits = 0
    macro_hits = 0

    for item in rss_items:
        text = f"{item['title']} {item['description']}"
        lower = text.lower()
        if not (
            any(kw in lower for kw in symbol_kws)
            or any(kw in lower for kw in GENERIC_MARKET_KEYWORDS)
            or any(kw in lower for kw in MACRO_EVENT_KEYWORDS)
        ):
            continue
        macro_count = sum(1 for kw in MACRO_EVENT_KEYWORDS if kw in lower)
        on_count = sum(1 for kw in RISK_ON_WORDS if kw in lower)
        off_count = sum(1 for kw in RISK_OFF_WORDS if kw in lower)
        if macro_count == 0 and on_count == 0 and off_count == 0:
            continue
        macro_hits += macro_count
        risk_on_hits += on_count
        risk_off_hits += off_count
        event_candidates.append(
            {
                "headline": item["title"][:160],
                "link": item["link"],
                "macro_hits": macro_count,
                "risk_on_hits": on_count,
                "risk_off_hits": off_count,
            }
        )

    if not event_candidates:
        return {
            "macro_bias": "neutral",
            "macro_confidence": 0.35,
            "macro_conflict": False,
            "event_risk_level": "low",
            "event_risk_score": 1,
            "expires_hours": 0,
            "key_event_headlines": [],
        }

    net_bias = risk_on_hits - risk_off_hits
    macro_bias = "neutral"
    if net_bias >= 2:
        macro_bias = "risk_on"
    elif net_bias <= -2:
        macro_bias = "risk_off"
    macro_conflict = risk_on_hits > 0 and risk_off_hits > 0
    macro_confidence = clamp(0.45 + (abs(net_bias) + macro_hits) * 0.08, 0.35, 0.9)

    raw_event_score = macro_hits * 2 + max(risk_on_hits, risk_off_hits)
    if macro_conflict:
        raw_event_score += 2
    if raw_event_score >= 6:
        event_risk_level = "high"
        expires_hours = 12
        event_risk_score = 10
    elif raw_event_score >= 3:
        event_risk_level = "medium"
        expires_hours = 24
        event_risk_score = 6
    else:
        event_risk_level = "low"
        expires_hours = 12 if macro_hits > 0 else 0
        event_risk_score = 2 if macro_hits > 0 else 1

    ranked_headlines = sorted(
        event_candidates,
        key=lambda item: (item["macro_hits"] + item["risk_on_hits"] + item["risk_off_hits"]),
        reverse=True,
    )
    return {
        "macro_bias": macro_bias,
        "macro_confidence": round(macro_confidence, 2),
        "macro_conflict": macro_conflict,
        "event_risk_level": event_risk_level,
        "event_risk_score": event_risk_score,
        "expires_hours": expires_hours,
        "key_event_headlines": [item["headline"] for item in ranked_headlines[:3]],
    }


def summarize_news(symbol: str, rss_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    event_context = analyze_news_event_context(symbol, rss_items)
    kws = symbol_keywords(symbol)
    matched: list[dict[str, Any]] = []
    for it in rss_items:
        text = f"{it['title']} {it['description']}"
        lower = text.lower()
        if not any(kw in lower for kw in kws):
            continue
        score, conf = sentiment_from_text(text)
        label = "neutral"
        if score > 0.2:
            label = "positive"
        elif score < -0.2:
            label = "negative"
        matched.append(
            {
                "headline": it["title"][:160],
                "sentiment": label,
                "score": round(score, 3),
                "confidence": round(conf, 2),
                "link": it["link"],
            }
        )
    if not matched:
        return [], {
            "sentiment": "neutral",
            "score": 0.0,
            "confidence": 0.35,
            "conflict": False,
            **event_context,
        }
    scores = [x["score"] for x in matched]
    confs = [x["confidence"] for x in matched]
    avg_score = statistics.fmean(scores)
    avg_conf = statistics.fmean(confs)
    conflict = any(s > 0.15 for s in scores) and any(s < -0.15 for s in scores)
    sentiment = "neutral"
    if avg_score > 0.2:
        sentiment = "positive"
    elif avg_score < -0.2:
        sentiment = "negative"
    return matched[:6], {
        "sentiment": sentiment,
        "score": round(avg_score, 3),
        "confidence": round(avg_conf, 2),
        "conflict": conflict,
        **event_context,
    }


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def score_risk(
    returns: dict[str, float],
    volatility: dict[str, float],
    trend: dict[str, Any],
    volume_ratio: float,
    news_summary: dict[str, Any],
) -> tuple[int, dict[str, float]]:
    vol_risk = clamp(
        volatility["realized_24h"] * 4
        + abs(returns["1h"]) * 2.5
        + abs(returns["5m"]) * 3.5,
        0,
        35,
    )
    if volume_ratio >= 1.0:
        liq_risk = 3
    elif volume_ratio >= 0.7:
        liq_risk = 7
    elif volume_ratio >= 0.4:
        liq_risk = 11
    else:
        liq_risk = 15

    news_risk = 0

    trend_view = trend["trend_view"]
    rsi = trend["rsi14"]
    if trend_view == "mixed":
        trend_risk = 10
    elif trend_view == "bullish" and rsi > 75:
        trend_risk = 14
    elif trend_view == "bearish" and rsi < 25:
        trend_risk = 14
    else:
        trend_risk = 4

    spike = max(abs(returns["5m"]), abs(returns["1h"]), abs(returns["4h"]))
    if spike >= 4:
        event_risk = 10
    elif spike >= 2.5:
        event_risk = 7
    elif spike >= 1.5:
        event_risk = 4
    else:
        event_risk = 1

    total = int(round(clamp(vol_risk + liq_risk + news_risk + trend_risk + event_risk, 0, 100)))
    return total, {
        "volatility": round(vol_risk, 2),
        "liquidity": round(liq_risk, 2),
        "news_uncertainty": round(news_risk, 2),
        "trend_conflict": round(trend_risk, 2),
        "event_risk": round(event_risk, 2),
        "news_event_overlay": 0.0,
    }


def classify_risk(score: int) -> str:
    if score <= 24:
        return "low"
    if score <= 49:
        return "medium"
    if score <= 74:
        return "high"
    return "extreme"


def thresholds(profile: str) -> tuple[int, int, float]:
    if profile == "conservative":
        return 45, 70, 0.8
    if profile == "aggressive":
        return 60, 85, 1.5
    return 50, 75, 1.0


def decision_zh(decision: str) -> str:
    mapping = {
        "scale_in_test": "可小額分批試單",
        "watch": "先觀望，不急著下單",
        "avoid": "先不要下單",
    }
    return mapping.get(decision, decision)


def risk_zh(risk_level: str) -> str:
    mapping = {
        "low": "低",
        "medium": "中",
        "high": "高",
        "extreme": "極高",
    }
    return mapping.get(risk_level, risk_level)


def sentiment_zh(sentiment: str) -> str:
    mapping = {
        "positive": "偏正向",
        "negative": "偏負向",
        "neutral": "中性",
    }
    return mapping.get(sentiment, sentiment)


def macro_bias_zh(macro_bias: str) -> str:
    mapping = {
        "risk_on": "偏 risk-on",
        "risk_off": "偏 risk-off",
        "neutral": "中性",
    }
    return mapping.get(macro_bias, macro_bias)


def estimate_direction_timing(
    is_sideways: bool,
    range_pct_4h: float,
    atr_pct: float,
    returns: dict[str, float],
    trend_view: str,
) -> dict[str, str]:
    momentum = max(abs(returns["1h"]), abs(returns["4h"]))
    if is_sideways:
        if range_pct_4h <= 0.9 and atr_pct <= 1.2:
            return {"window": "約 12-36 小時", "confidence": "中", "reason": "波動壓縮明顯，通常需要時間累積方向。"}
        return {"window": "約 6-18 小時", "confidence": "中", "reason": "區間震盪中，接近箱體邊界時容易出方向。"}
    if trend_view in {"bullish", "bearish"} and momentum >= 1.2:
        return {"window": "約 1-8 小時", "confidence": "中高", "reason": "已出現趨勢動能，短時間續行或回檔機率較高。"}
    return {"window": "約 8-24 小時", "confidence": "中低", "reason": "方向仍在確認，需等待價格突破關鍵位。"}


def derive_actionable_levels(
    candles_1m: list[Candle],
    price: float,
    volatility: dict[str, float],
    returns: dict[str, float],
    trend_view: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reference_candles = candles_1m[:-1] if len(candles_1m) >= 2 else candles_1m
    recent_4h = reference_candles[-240:] if len(reference_candles) >= 240 else reference_candles
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
    timing = estimate_direction_timing(is_sideways, range_pct_4h, atr_pct, returns, trend_view)
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


def summarize_timeframe(candles: list[Candle], timeframe: str) -> dict[str, Any]:
    closes = [c.close for c in candles]
    fast_period = 9 if timeframe == "5m" else 20
    slow_period = 21 if timeframe == "5m" else 50
    ema_fast = ema(closes, fast_period)
    ema_slow = ema(closes, slow_period)
    rsi14 = calc_rsi(closes, 14)
    trend = "mixed"
    if ema_fast > ema_slow and rsi14 >= 52:
        trend = "bullish"
    elif ema_fast < ema_slow and rsi14 <= 48:
        trend = "bearish"
    recent = candles[-20:] if len(candles) >= 20 else candles
    high = max(c.high for c in recent)
    low = min(c.low for c in recent)
    price = closes[-1]
    range_pct = ((high - low) / price * 100) if price > 0 else 0.0
    one_bar_ret = pct_change_from_closes(closes, 1)
    six_bar_ret = pct_change_from_closes(closes, 6)
    last_vol = recent[-1].volume
    avg_vol = statistics.fmean([c.volume for c in recent[:-1]]) if len(recent) > 1 else last_vol
    vol_ratio = round(last_vol / avg_vol, 3) if avg_vol else 1.0
    vwap20 = rolling_vwap(candles, 20)
    return {
        "timeframe": timeframe,
        "trend": trend,
        "rsi14": round(rsi14, 2),
        "ema_fast": round(ema_fast, 4),
        "ema_slow": round(ema_slow, 4),
        "range_pct_20bars": round(range_pct, 3),
        "return_1bar_pct": round(one_bar_ret, 3),
        "return_6bar_pct": round(six_bar_ret, 3),
        "volume_ratio": vol_ratio,
        "vwap20": round(vwap20, 4),
        "above_vwap": price > vwap20,
    }


def detect_false_breakout(candles: list[Candle], upper_trigger: float, lower_trigger: float) -> dict[str, bool]:
    recent = candles[-3:] if len(candles) >= 3 else candles
    false_up = any(c.high >= upper_trigger and c.close < upper_trigger for c in recent)
    false_down = any(c.low <= lower_trigger and c.close > lower_trigger for c in recent)
    return {"false_breakout_up": false_up, "false_breakout_down": false_down}


def classify_market_regime(tf4h: dict[str, Any], returns: dict[str, float]) -> str:
    bullish_pressure = tf4h["trend"] == "bullish" and returns["24h"] >= 3.0 and returns["4h"] >= 0.5
    bearish_pressure = tf4h["trend"] == "bearish" and returns["24h"] <= -3.0 and returns["4h"] <= -0.5
    if bullish_pressure:
        return "bull_trend"
    if bearish_pressure:
        return "bear_trend"
    return "range_or_mixed"


def build_short_term_signal(
    tf5: dict[str, Any],
    tf4h: dict[str, Any],
    price: float,
    false_breakout: dict[str, bool],
    returns: dict[str, float],
) -> dict[str, Any]:
    market_regime = classify_market_regime(tf4h, returns)
    long_core_checks = {
        "4h_trend": tf4h["trend"] == "bullish",
        "4h_return": returns["4h"] >= 0.2,
        "24h_return": returns["24h"] >= 0.5,
    }
    short_core_checks = {
        "4h_trend": tf4h["trend"] == "bearish",
        "4h_return": returns["4h"] <= -0.2,
        "24h_return": returns["24h"] <= -0.5,
    }
    long_checks = {
        "5m_trend": tf5["trend"] == "bullish",
        "5m_rsi": 52 <= tf5["rsi14"] <= 72,
        "5m_above_vwap": tf5["above_vwap"],
        "5m_volume": tf5["volume_ratio"] > 1.2,
        "no_false_breakout": not false_breakout["false_breakout_up"],
    }
    short_checks = {
        "5m_trend": tf5["trend"] == "bearish",
        "5m_rsi": 28 <= tf5["rsi14"] <= 48,
        "5m_below_vwap": not tf5["above_vwap"],
        "5m_volume": tf5["volume_ratio"] > 1.2,
        "no_false_breakout": not false_breakout["false_breakout_down"],
    }
    long_core_score = sum(1 for ok in long_core_checks.values() if ok)
    short_core_score = sum(1 for ok in short_core_checks.values() if ok)
    long_micro_score = sum(1 for ok in long_checks.values() if ok)
    short_micro_score = sum(1 for ok in short_checks.values() if ok)
    long_score = long_core_score * 2 + long_micro_score
    short_score = short_core_score * 2 + short_micro_score
    long_threshold = 6
    short_threshold = 6
    if market_regime == "bull_trend":
        short_threshold = 7
    elif market_regime == "bear_trend":
        long_threshold = 7
    strong_long_micro = long_checks["5m_trend"] and long_checks["5m_volume"] and long_checks["5m_above_vwap"]
    strong_short_micro = short_checks["5m_trend"] and short_checks["5m_volume"] and short_checks["5m_below_vwap"]
    long_gate_open = long_core_score >= 2 or (market_regime == "range_or_mixed" and long_core_score >= 1 and strong_long_micro)
    short_gate_open = short_core_score >= 2 or (market_regime == "range_or_mixed" and short_core_score >= 1 and strong_short_micro)
    bias = "neutral"
    strength = "low"
    thesis: list[str] = []
    if long_gate_open and long_score >= long_threshold and short_score <= max(short_threshold - 2, 3):
        bias = "long"
    elif short_gate_open and short_score >= short_threshold and long_score <= max(long_threshold - 2, 3):
        bias = "short"
    if max(long_score, short_score) >= 8:
        strength = "high"
    elif max(long_score, short_score) >= 6:
        strength = "medium"
    regime_zh = {
        "bull_trend": "多頭主導",
        "bear_trend": "空頭主導",
        "range_or_mixed": "盤整/混合",
    }[market_regime]
    if bias == "long":
        thesis.append("4h 與 24h 偏多，5m 只拿來等進場節奏。")
    elif bias == "short":
        thesis.append("4h 與 24h 偏空，5m 只拿來等進場節奏。")
    else:
        thesis.append("4h/24h 沒有先給出乾淨方向，先把 5m 當節奏資訊，不急著交易。")
    thesis.append(
        f"市場狀態={regime_zh}，4h核心 long {long_core_score}/3、short {short_core_score}/3；"
        f"做多門檻={long_threshold}，做空門檻={short_threshold}"
    )
    thesis.append(
        f"5m 微結構 long {long_micro_score}/5、short {short_micro_score}/5；"
        f"量能比={tf5['volume_ratio']}, RSI={tf5['rsi14']}, VWAP位置={'上方' if tf5['above_vwap'] else '下方'}"
    )
    if false_breakout["false_breakout_up"] or false_breakout["false_breakout_down"]:
        thesis.append(
            f"假突破過濾：up={false_breakout['false_breakout_up']}, down={false_breakout['false_breakout_down']}"
        )
    return {
        "bias": bias,
        "strength": strength,
        "long_score": long_score,
        "short_score": short_score,
        "long_core_score": long_core_score,
        "short_core_score": short_core_score,
        "long_micro_score": long_micro_score,
        "short_micro_score": short_micro_score,
        "market_regime": market_regime,
        "long_threshold": long_threshold,
        "short_threshold": short_threshold,
        "gate_open": {"long": long_gate_open, "short": short_gate_open},
        "checks": {
            "long_core": long_core_checks,
            "short_core": short_core_checks,
            "long_micro": long_checks,
            "short_micro": short_checks,
        },
        "thesis": thesis,
        "reference_price": round(price, 4),
        "false_breakout": false_breakout,
    }


def build_swing_signal(
    tf1h: dict[str, Any],
    tf4h: dict[str, Any],
    price: float,
    false_breakout: dict[str, bool],
    returns: dict[str, float],
    onchain_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_regime = classify_market_regime(tf4h, returns)
    onchain_summary = onchain_summary or {}
    onchain_bias = str(onchain_summary.get("bias", "neutral"))
    onchain_available = bool(onchain_summary.get("available"))

    long_core_checks = {
        "4h_trend": tf4h["trend"] == "bullish",
        "1h_trend": tf1h["trend"] == "bullish",
        "24h_return": returns["24h"] >= 1.0,
        "1h_above_vwap": tf1h["above_vwap"],
        "onchain_not_bearish": (not onchain_available) or onchain_bias != "bearish",
    }
    short_core_checks = {
        "4h_trend": tf4h["trend"] == "bearish",
        "1h_trend": tf1h["trend"] == "bearish",
        "24h_return": returns["24h"] <= -1.0,
        "1h_below_vwap": not tf1h["above_vwap"],
        "onchain_not_bullish": (not onchain_available) or onchain_bias != "bullish",
    }
    long_micro_checks = {
        "1h_rsi": 50 <= tf1h["rsi14"] <= 70,
        "1h_volume": tf1h["volume_ratio"] > 1.05,
        "4h_return": returns["4h"] >= 0.3,
        "no_false_breakout": not false_breakout["false_breakout_up"],
    }
    short_micro_checks = {
        "1h_rsi": 30 <= tf1h["rsi14"] <= 50,
        "1h_volume": tf1h["volume_ratio"] > 1.05,
        "4h_return": returns["4h"] <= -0.3,
        "no_false_breakout": not false_breakout["false_breakout_down"],
    }

    long_core_score = sum(1 for ok in long_core_checks.values() if ok)
    short_core_score = sum(1 for ok in short_core_checks.values() if ok)
    long_micro_score = sum(1 for ok in long_micro_checks.values() if ok)
    short_micro_score = sum(1 for ok in short_micro_checks.values() if ok)
    long_score = long_core_score * 2 + long_micro_score
    short_score = short_core_score * 2 + short_micro_score

    if onchain_bias == "bearish":
        long_score -= 2
    elif onchain_bias == "bullish":
        short_score -= 2

    long_threshold = 8
    short_threshold = 8
    if market_regime == "bull_trend":
        short_threshold = 9
    elif market_regime == "bear_trend":
        long_threshold = 9

    long_gate_open = bool(long_core_checks["4h_trend"]) and long_core_score >= 3
    short_gate_open = bool(short_core_checks["4h_trend"]) and short_core_score >= 3
    bias = "neutral"
    if long_gate_open and long_score >= long_threshold and short_score <= max(short_threshold - 2, 4):
        bias = "long"
    elif short_gate_open and short_score >= short_threshold and long_score <= max(long_threshold - 2, 4):
        bias = "short"
    if onchain_available and onchain_bias == "bearish" and bias == "long":
        bias = "neutral"
    if onchain_available and onchain_bias == "bullish" and bias == "short":
        bias = "neutral"

    strength = "low"
    if max(long_score, short_score) >= 11:
        strength = "high"
    elif max(long_score, short_score) >= 8:
        strength = "medium"

    thesis = []
    if bias == "long":
        thesis.append("以 4h 與 1h 共振為主，等待波段轉強。")
    elif bias == "short":
        thesis.append("以 4h 與 1h 共振為主，等待波段轉弱。")
    else:
        thesis.append("高週期尚未同向，先以波段觀察為主。")
    thesis.append(
        f"市場狀態 {market_regime}；核心分數 long {long_core_score}/5、short {short_core_score}/5；"
        f"1h 結構分數 long {long_micro_score}/4、short {short_micro_score}/4。"
    )
    thesis.append(
        f"1h 量比 {tf1h['volume_ratio']}、RSI {tf1h['rsi14']}、VWAP {'上方' if tf1h['above_vwap'] else '下方'}。"
    )
    if onchain_available:
        thesis.append(
            f"鏈上濾網 {onchain_bias}（{onchain_summary.get('confidence', 'low')}），"
            f"{'；'.join(onchain_summary.get('summary', [])[:2])}"
        )

    return {
        "bias": bias,
        "strength": strength,
        "long_score": long_score,
        "short_score": short_score,
        "long_core_score": long_core_score,
        "short_core_score": short_core_score,
        "long_micro_score": long_micro_score,
        "short_micro_score": short_micro_score,
        "market_regime": market_regime,
        "long_threshold": long_threshold,
        "short_threshold": short_threshold,
        "gate_open": {"long": long_gate_open, "short": short_gate_open},
        "checks": {
            "long_core": long_core_checks,
            "short_core": short_core_checks,
            "long_micro": long_micro_checks,
            "short_micro": short_micro_checks,
        },
        "thesis": thesis,
        "reference_price": round(price, 4),
        "false_breakout": false_breakout,
        "strategy_style": "swing",
        "timeframe_anchor": "1h/4h",
        "onchain_bias": onchain_bias,
    }

def build_swing_signal(
    tf1h: dict[str, Any],
    tf4h: dict[str, Any],
    price: float,
    false_breakout: dict[str, bool],
    returns: dict[str, float],
    onchain_summary: dict[str, Any] | None = None,
    funding_summary: dict[str, Any] | None = None,
    swing_mtf_score: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_regime = classify_market_regime(tf4h, returns)
    onchain_summary = onchain_summary or {}
    funding_summary = funding_summary or {}
    swing_mtf_score = swing_mtf_score or {"score": 50.0, "bias": "neutral", "components": {}, "weights": {}}
    onchain_bias = str(onchain_summary.get("bias", "neutral"))
    onchain_available = bool(onchain_summary.get("available"))
    funding_bias = str(funding_summary.get("bias", "neutral"))
    funding_available = bool(funding_summary.get("available"))
    mtf_bias = str(swing_mtf_score.get("bias", "neutral"))
    mtf_score_value = float(swing_mtf_score.get("score", 50.0))

    long_core_checks = {
        "4h_trend": tf4h["trend"] == "bullish",
        "1h_trend": tf1h["trend"] == "bullish",
        "24h_return": returns["24h"] >= 1.0,
        "1h_above_vwap": tf1h["above_vwap"],
        "onchain_not_bearish": (not onchain_available) or onchain_bias != "bearish",
        "funding_not_bearish": (not funding_available) or funding_bias != "bearish",
    }
    short_core_checks = {
        "4h_trend": tf4h["trend"] == "bearish",
        "1h_trend": tf1h["trend"] == "bearish",
        "24h_return": returns["24h"] <= -1.0,
        "1h_below_vwap": not tf1h["above_vwap"],
        "onchain_not_bullish": (not onchain_available) or onchain_bias != "bullish",
        "funding_not_bullish": (not funding_available) or funding_bias != "bullish",
    }
    long_micro_checks = {
        "1h_rsi": 50 <= tf1h["rsi14"] <= 70,
        "1h_volume": tf1h["volume_ratio"] > 1.05,
        "4h_return": returns["4h"] >= 0.3,
        "no_false_breakout": not false_breakout["false_breakout_up"],
    }
    short_micro_checks = {
        "1h_rsi": 30 <= tf1h["rsi14"] <= 50,
        "1h_volume": tf1h["volume_ratio"] > 1.05,
        "4h_return": returns["4h"] <= -0.3,
        "no_false_breakout": not false_breakout["false_breakout_down"],
    }

    long_core_score = sum(1 for ok in long_core_checks.values() if ok)
    short_core_score = sum(1 for ok in short_core_checks.values() if ok)
    long_micro_score = sum(1 for ok in long_micro_checks.values() if ok)
    short_micro_score = sum(1 for ok in short_micro_checks.values() if ok)
    long_score = long_core_score * 2 + long_micro_score
    short_score = short_core_score * 2 + short_micro_score

    if onchain_bias == "bearish":
        long_score -= 2
    elif onchain_bias == "bullish":
        short_score -= 2
    if funding_bias == "bearish":
        long_score -= 2
    elif funding_bias == "bullish":
        short_score -= 2
    if mtf_bias == "long":
        long_score += 1
    elif mtf_bias == "short":
        short_score += 1

    long_threshold = 8
    short_threshold = 8
    if market_regime == "bull_trend":
        short_threshold = 9
    elif market_regime == "bear_trend":
        long_threshold = 9

    long_gate_open = bool(long_core_checks["4h_trend"]) and long_core_score >= 3
    short_gate_open = bool(short_core_checks["4h_trend"]) and short_core_score >= 3
    bias = "neutral"
    if long_gate_open and long_score >= long_threshold and short_score <= max(short_threshold - 2, 4):
        bias = "long"
    elif short_gate_open and short_score >= short_threshold and long_score <= max(long_threshold - 2, 4):
        bias = "short"
    if onchain_available and onchain_bias == "bearish" and bias == "long":
        bias = "neutral"
    if onchain_available and onchain_bias == "bullish" and bias == "short":
        bias = "neutral"
    if funding_available and funding_bias == "bearish" and bias == "long":
        bias = "neutral"
    if funding_available and funding_bias == "bullish" and bias == "short":
        bias = "neutral"

    strength = "low"
    if max(long_score, short_score) >= 11:
        strength = "high"
    elif max(long_score, short_score) >= 8:
        strength = "medium"

    thesis: list[str] = []
    if bias == "long":
        thesis.append("4h 與 1h 同步偏多，優先看上方波段延續。")
    elif bias == "short":
        thesis.append("4h 與 1h 同步偏空，優先看下方波段延續。")
    else:
        thesis.append("高週期尚未給出乾淨方向，先維持中性等待。")
    thesis.append(
        f"市場狀態 {market_regime}；核心 long {long_core_score}/6、short {short_core_score}/6；"
        f"1h 微結構 long {long_micro_score}/4、short {short_micro_score}/4。"
    )
    thesis.append(
        f"1h 量比 {tf1h['volume_ratio']}、RSI {tf1h['rsi14']}、VWAP {'上方' if tf1h['above_vwap'] else '下方'}。"
    )
    thesis.append(f"swing_mtf_score {round(mtf_score_value, 2)} / 偏向 {mtf_bias}")
    if onchain_available:
        thesis.append(
            f"鏈上濾網 {onchain_bias}：{' / '.join(str(x) for x in onchain_summary.get('summary', [])[:2])}"
        )
    if funding_available:
        thesis.append(
            f"Funding {funding_summary.get('current_rate_pct', 0.0)}% / avg {funding_summary.get('avg_recent_rate_pct', 0.0)}% / {funding_bias}"
        )

    return {
        "bias": bias,
        "strength": strength,
        "long_score": long_score,
        "short_score": short_score,
        "long_core_score": long_core_score,
        "short_core_score": short_core_score,
        "long_micro_score": long_micro_score,
        "short_micro_score": short_micro_score,
        "market_regime": market_regime,
        "swing_mtf_score": round(mtf_score_value, 2),
        "swing_mtf_bias": mtf_bias,
        "long_threshold": long_threshold,
        "short_threshold": short_threshold,
        "gate_open": {"long": long_gate_open, "short": short_gate_open},
        "checks": {
            "long_core": long_core_checks,
            "short_core": short_core_checks,
            "long_micro": long_micro_checks,
            "short_micro": short_micro_checks,
        },
        "thesis": thesis,
        "reference_price": round(price, 4),
        "false_breakout": false_breakout,
        "strategy_style": "swing",
        "timeframe_anchor": "1h/4h",
        "onchain_bias": onchain_bias,
        "funding_bias": funding_bias,
        "funding_available": funding_available,
        "funding_signal": str(funding_summary.get("signal", "NORMAL")),
    }


def build_long_short_plan(
    price: float,
    atr_pct: float,
    levels: dict[str, Any],
    tf5: dict[str, Any],
    tf4h: dict[str, Any],
    short_signal: dict[str, Any],
) -> dict[str, Any]:
    def summarize_trigger_distance(trigger_price: float) -> dict[str, Any]:
        distance_abs = abs(trigger_price - price)
        distance_pct = (distance_abs / price * 100.0) if price > 0 else 0.0
        distance_atr = (distance_abs / atr_abs) if atr_abs > 0 else 0.0
        readiness = "ready"
        if distance_atr >= 1.25 or distance_pct >= 1.8:
            readiness = "far"
        elif distance_atr >= 0.9 or distance_pct >= 1.0:
            readiness = "caution"
        return {
            "distance_abs": round(distance_abs, 4),
            "distance_pct": round(distance_pct, 3),
            "distance_atr": round(distance_atr, 3),
            "readiness": readiness,
        }

    atr_abs = max(price * max(atr_pct, 0.8) / 100, price * 0.004)
    direction_map = {"long": "long_bias", "short": "short_bias", "neutral": "neutral"}
    analysis_direction = direction_map.get(short_signal["bias"], "neutral")
    direction = analysis_direction

    long_trigger = float(levels["breakout_up"])
    short_trigger = float(levels["breakout_down"])
    long_retest_zone = [float(x) for x in levels.get("long_retest_zone", [long_trigger, long_trigger])]
    short_retest_zone = [float(x) for x in levels.get("short_retest_zone", [short_trigger, short_trigger])]
    long_trigger_distance = summarize_trigger_distance(long_trigger)
    short_trigger_distance = summarize_trigger_distance(short_trigger)

    long_second_breakout = long_trigger + 0.25 * atr_abs
    short_second_breakdown = short_trigger - 0.25 * atr_abs
    long_retest_failure = long_trigger - 0.6 * atr_abs
    short_retest_failure = short_trigger + 0.6 * atr_abs
    long_breakeven_trigger = long_trigger + 0.95 * atr_abs
    short_breakeven_trigger = short_trigger - 0.95 * atr_abs
    long_scale_out_zone = [long_trigger + 1.2 * atr_abs, long_trigger + 1.6 * atr_abs]
    short_scale_out_zone = [short_trigger - 1.6 * atr_abs, short_trigger - 1.2 * atr_abs]
    long_runner_zone = [long_trigger + 1.8 * atr_abs, long_trigger + 2.6 * atr_abs]
    short_runner_zone = [short_trigger - 2.6 * atr_abs, short_trigger - 1.8 * atr_abs]

    long_plan = {
        "trigger_price": round(long_trigger, 4),
        "entry_zone": [round(long_trigger, 4), round(long_trigger + 0.35 * atr_abs, 4)],
        "stop_loss": round(long_trigger - 1.0 * atr_abs, 4),
        "take_profit": [round(long_trigger + 1.5 * atr_abs, 4), round(long_trigger + 2.5 * atr_abs, 4)],
        "condition": "5m 收線站上觸發價，且 volume_ratio > 1.2，且最近3根沒有假突破回落。",
        "setup_type": "突破後等回踩，再看第二次突破",
        "confirmation": {
            "first_breakout_watch": round(long_trigger, 4),
            "retest_zone": [round(long_retest_zone[0], 4), round(long_retest_zone[1], 4)],
            "second_breakout_trigger": round(long_second_breakout, 4),
            "retest_failure_level": round(long_retest_failure, 4),
            "notes": "第一次突破先觀察；回踩守住再等第二次突破，這比直接追第一下更穩。",
        },
        "management": {
            "breakeven_trigger": round(long_breakeven_trigger, 4),
            "breakeven_stop": round(long_trigger, 4),
            "scale_out_zone": [round(long_scale_out_zone[0], 4), round(long_scale_out_zone[1], 4)],
            "runner_zone": [round(long_runner_zone[0], 4), round(long_runner_zone[1], 4)],
            "notes": "先到分批區落袋部分倉位，站穩後再把止損上移到保本。",
        },
    }
    short_plan = {
        "trigger_price": round(short_trigger, 4),
        "entry_zone": [round(short_trigger - 0.35 * atr_abs, 4), round(short_trigger, 4)],
        "stop_loss": round(short_trigger + 1.0 * atr_abs, 4),
        "take_profit": [round(short_trigger - 1.5 * atr_abs, 4), round(short_trigger - 2.5 * atr_abs, 4)],
        "condition": "5m 收線跌破觸發價，且 volume_ratio > 1.2，且最近3根沒有假突破回落。",
        "setup_type": "跌破後等反抽，再看第二次跌破",
        "confirmation": {
            "first_breakout_watch": round(short_trigger, 4),
            "retest_zone": [round(short_retest_zone[0], 4), round(short_retest_zone[1], 4)],
            "second_breakout_trigger": round(short_second_breakdown, 4),
            "retest_failure_level": round(short_retest_failure, 4),
            "notes": "第一次跌破先觀察；反抽站不回去，再等第二次跌破。",
        },
        "management": {
            "breakeven_trigger": round(short_breakeven_trigger, 4),
            "breakeven_stop": round(short_trigger, 4),
            "scale_out_zone": [round(short_scale_out_zone[0], 4), round(short_scale_out_zone[1], 4)],
            "runner_zone": [round(short_runner_zone[0], 4), round(short_runner_zone[1], 4)],
            "notes": "先到分批區回補部分倉位，延續時再把止損下移到保本。",
        },
    }

    preferred_side = "neutral"
    preferred_distance = long_trigger_distance if long_trigger_distance["distance_abs"] <= short_trigger_distance["distance_abs"] else short_trigger_distance
    if analysis_direction == "long_bias":
        preferred_side = "long"
        preferred_distance = long_trigger_distance
    elif analysis_direction == "short_bias":
        preferred_side = "short"
        preferred_distance = short_trigger_distance

    execution_readiness = preferred_distance["readiness"] if analysis_direction != "neutral" else (
        "balanced" if preferred_distance["readiness"] == "far" else preferred_distance["readiness"]
    )
    readiness_label = {
        "ready": "可執行",
        "caution": "等待靠近",
        "far": "距離過遠",
        "balanced": "雙向等待",
    }[execution_readiness]
    signal_strength = short_signal["strength"]

    recommendation = "先把這輪當雙劇本觀察，等一側條件成立再執行。"
    if analysis_direction == "long_bias":
        if long_trigger_distance["readiness"] == "far":
            direction = "neutral"
            signal_strength = "low" if signal_strength != "low" else signal_strength
            recommendation = "上方劇本存在，但上破價離現價偏遠；先觀察，不追價。"
        elif long_trigger_distance["readiness"] == "caution":
            if signal_strength == "high":
                signal_strength = "medium"
            recommendation = "上方劇本較優先，但先等價格靠近做多區再評估。"
        else:
            recommendation = "上方劇本優先，等做多觸發成立再執行。"
    elif analysis_direction == "short_bias":
        if short_trigger_distance["readiness"] == "far":
            direction = "neutral"
            signal_strength = "low" if signal_strength != "low" else signal_strength
            recommendation = "下方劇本存在，但下破價離現價偏遠；先觀察，不提前空。"
        elif short_trigger_distance["readiness"] == "caution":
            if signal_strength == "high":
                signal_strength = "medium"
            recommendation = "下方劇本較優先，但先等價格靠近做空區再評估。"
        else:
            recommendation = "下方劇本優先，等做空觸發成立再執行。"
    else:
        if preferred_distance["readiness"] == "far":
            recommendation = "雙劇本都還遠，先把這輪當區間觀察。"
        elif preferred_distance["readiness"] == "caution":
            recommendation = "先看價格靠近上下破區，再決定往哪邊執行。"

    def build_executor_plan(side: str, setup: dict[str, Any]) -> dict[str, Any]:
        entry_trigger = float(setup["confirmation"]["second_breakout_trigger"])
        stop_loss = float(setup["stop_loss"])
        tp1 = float(setup["take_profit"][0])
        tp2 = float(setup["take_profit"][1])
        if side == "long":
            risk_pct = abs((entry_trigger - stop_loss) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp1_pct = abs((tp1 - entry_trigger) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp2_pct = abs((tp2 - entry_trigger) / entry_trigger * 100.0) if entry_trigger else 0.0
        else:
            risk_pct = abs((stop_loss - entry_trigger) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp1_pct = abs((entry_trigger - tp1) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp2_pct = abs((entry_trigger - tp2) / entry_trigger * 100.0) if entry_trigger else 0.0
        rr_to_tp1 = (reward_tp1_pct / risk_pct) if risk_pct > 0 else 0.0
        rr_to_tp2 = (reward_tp2_pct / risk_pct) if risk_pct > 0 else 0.0
        cancel_after_minutes = {"ready": 90, "caution": 120, "far": 180, "balanced": 120}.get(execution_readiness, 120)
        quality = "tradable"
        notes: list[str] = []
        if rr_to_tp1 < 0.8 or risk_pct > 2.2:
            quality = "observe_only"
        elif rr_to_tp1 < 0.95 or risk_pct > 1.8:
            quality = "caution"
        if rr_to_tp1 < 0.95:
            notes.append(f"TP1 風報比偏低 ({rr_to_tp1:.2f})")
        if risk_pct > 1.8:
            notes.append(f"止損距離偏大 ({risk_pct:.2f}%)")
        if execution_readiness == "far":
            notes.append("觸發點距現價偏遠")
        return {
            "order_type": "stop_market",
            "entry_trigger": round(entry_trigger, 4),
            "cancel_after_minutes": cancel_after_minutes,
            "risk_pct": round(risk_pct, 3),
            "reward_tp1_pct": round(reward_tp1_pct, 3),
            "reward_tp2_pct": round(reward_tp2_pct, 3),
            "rr_to_tp1": round(rr_to_tp1, 3),
            "rr_to_tp2": round(rr_to_tp2, 3),
            "quality": quality,
            "notes": notes,
        }

    long_plan["executor_plan"] = build_executor_plan("long", long_plan)
    short_plan["executor_plan"] = build_executor_plan("short", short_plan)

    return {
        "analysis_bias": analysis_direction,
        "direction_bias": direction,
        "recommendation": recommendation,
        "timeframe_weight": "4h/24h:65%, 5m:35%",
        "tf_5m_trend": tf5["trend"],
        "tf_4h_trend": tf4h["trend"],
        "signal_strength": signal_strength,
        "analysis_signal_strength": short_signal["strength"],
        "execution_readiness": execution_readiness,
        "execution_readiness_label": readiness_label,
        "preferred_setup": preferred_side,
        "trigger_distance": {
            "long": long_trigger_distance,
            "short": short_trigger_distance,
            "preferred": preferred_distance,
        },
        "confirmation_priority": "second_breakout_retest",
        "long_setup": long_plan,
        "short_setup": short_plan,
    }


def build_swing_long_short_plan(
    price: float,
    atr_pct: float,
    levels: dict[str, Any],
    tf1h: dict[str, Any],
    tf4h: dict[str, Any],
    swing_signal: dict[str, Any],
    onchain_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del tf4h
    onchain_summary = onchain_summary or {}

    def summarize_trigger_distance(trigger_price: float) -> dict[str, Any]:
        distance_abs = abs(trigger_price - price)
        distance_pct = (distance_abs / price * 100.0) if price > 0 else 0.0
        distance_atr = (distance_abs / atr_abs) if atr_abs > 0 else 0.0
        readiness = "ready"
        if distance_atr >= 1.8 or distance_pct >= 3.2:
            readiness = "far"
        elif distance_atr >= 1.2 or distance_pct >= 2.0:
            readiness = "caution"
        return {
            "distance_abs": round(distance_abs, 4),
            "distance_pct": round(distance_pct, 3),
            "distance_atr": round(distance_atr, 3),
            "readiness": readiness,
        }

    atr_abs = max(price * max(atr_pct, 1.0) / 100, price * 0.008)
    direction_map = {"long": "long_bias", "short": "short_bias", "neutral": "neutral"}
    analysis_direction = direction_map.get(swing_signal["bias"], "neutral")
    direction = analysis_direction

    long_trigger = float(levels["breakout_up"])
    short_trigger = float(levels["breakout_down"])
    long_retest_zone = [float(x) for x in levels.get("long_retest_zone", [long_trigger, long_trigger])]
    short_retest_zone = [float(x) for x in levels.get("short_retest_zone", [short_trigger, short_trigger])]
    long_trigger_distance = summarize_trigger_distance(long_trigger)
    short_trigger_distance = summarize_trigger_distance(short_trigger)

    long_second_breakout = long_trigger + 0.45 * atr_abs
    short_second_breakdown = short_trigger - 0.45 * atr_abs
    long_retest_failure = long_trigger - 0.85 * atr_abs
    short_retest_failure = short_trigger + 0.85 * atr_abs
    long_breakeven_trigger = long_trigger + 1.6 * atr_abs
    short_breakeven_trigger = short_trigger - 1.6 * atr_abs
    long_scale_out_zone = [long_trigger + 2.0 * atr_abs, long_trigger + 2.8 * atr_abs]
    short_scale_out_zone = [short_trigger - 2.8 * atr_abs, short_trigger - 2.0 * atr_abs]
    long_runner_zone = [long_trigger + 3.0 * atr_abs, long_trigger + 4.5 * atr_abs]
    short_runner_zone = [short_trigger - 4.5 * atr_abs, short_trigger - 3.0 * atr_abs]

    long_plan = {
        "trigger_price": round(long_trigger, 4),
        "entry_zone": [round(long_trigger, 4), round(long_trigger + 0.55 * atr_abs, 4)],
        "stop_loss": round(long_trigger - 1.25 * atr_abs, 4),
        "take_profit": [round(long_trigger + 2.4 * atr_abs, 4), round(long_trigger + 3.8 * atr_abs, 4)],
        "condition": "以 1h 收線站上轉強價，4h 結構不轉弱，量能至少高於近 20 根均量。",
        "setup_type": "波段轉強",
        "confirmation": {
            "first_breakout_watch": round(long_trigger, 4),
            "retest_zone": [round(long_retest_zone[0], 4), round(long_retest_zone[1], 4)],
            "second_breakout_trigger": round(long_second_breakout, 4),
            "retest_failure_level": round(long_retest_failure, 4),
            "notes": "先看 1h 收線站上，再等回踩守住或第二次轉強。",
        },
        "management": {
            "breakeven_trigger": round(long_breakeven_trigger, 4),
            "breakeven_stop": round(long_trigger, 4),
            "scale_out_zone": [round(long_scale_out_zone[0], 4), round(long_scale_out_zone[1], 4)],
            "runner_zone": [round(long_runner_zone[0], 4), round(long_runner_zone[1], 4)],
            "notes": "中線單以分批減碼為主，不追求最短線保本。 ",
        },
    }


def default_funding_summary(symbol: str, *, enabled: bool, reason: str, available: bool = False) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "available": available,
        "provider": "binance_futures",
        "symbol": symbol,
        "signal": "NORMAL",
        "bias": "neutral",
        "current_rate": 0.0,
        "current_rate_pct": 0.0,
        "avg_recent_rate": 0.0,
        "avg_recent_rate_pct": 0.0,
        "summary": [reason],
    }


def summarize_funding_summary(
    symbol: str,
    current_rate: float,
    avg_recent_rate: float,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = settings or {}
    extreme_positive = float(settings.get("extreme_positive", 0.0003))
    extreme_negative = float(settings.get("extreme_negative", -0.0001))
    block_long_above = float(settings.get("block_long_above", 0.0005))
    block_short_below = float(settings.get("block_short_below", -0.0003))

    signal = "NORMAL"
    bias = "neutral"
    summary: list[str] = []
    if current_rate >= block_long_above:
        signal = "BLOCK_LONG"
        bias = "bearish"
        summary.append("Funding 過熱偏多，做多降級。")
    elif current_rate >= extreme_positive:
        signal = "EXTREME_POSITIVE"
        bias = "bearish"
        summary.append("Funding 偏高，追多風險上升。")
    elif current_rate <= block_short_below:
        signal = "BLOCK_SHORT"
        bias = "bullish"
        summary.append("Funding 過熱偏空，做空降級。")
    elif current_rate <= extreme_negative:
        signal = "EXTREME_NEGATIVE"
        bias = "bullish"
        summary.append("Funding 偏低，追空風險上升。")
    else:
        summary.append("Funding 中性，暫不干擾方向。")

    summary.append(
        f"當前 Funding {current_rate * 100:.4f}% / 近期均值 {avg_recent_rate * 100:.4f}%"
    )
    return {
        "enabled": True,
        "available": True,
        "provider": "binance_futures",
        "symbol": symbol,
        "signal": signal,
        "bias": bias,
        "current_rate": round(current_rate, 8),
        "current_rate_pct": round(current_rate * 100, 4),
        "avg_recent_rate": round(avg_recent_rate, 8),
        "avg_recent_rate_pct": round(avg_recent_rate * 100, 4),
        "summary": summary,
    }


def fetch_funding_summary(symbol: str, timeout: int, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}
    enabled = bool(settings.get("enabled", False))
    symbols = {str(item).upper() for item in settings.get("symbols", ["BTCUSDT", "ETHUSDT"])}
    if not enabled:
        return default_funding_summary(symbol, enabled=False, available=False, reason="Funding overlay 未啟用。")
    if symbol.upper() not in symbols:
        return default_funding_summary(symbol, enabled=True, available=False, reason="Funding overlay 目前只套用 BTC / ETH。")

    headers = {"User-Agent": USER_AGENT}
    try:
        premium_resp = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": symbol.upper()},
            headers=headers,
            timeout=timeout,
        )
        premium_resp.raise_for_status()
        premium_data = premium_resp.json()
        current_rate = float(premium_data.get("lastFundingRate", 0.0))

        history_limit = max(3, min(int(settings.get("history_limit", 12)), 50))
        history_resp = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": symbol.upper(), "limit": history_limit},
            headers=headers,
            timeout=timeout,
        )
        history_resp.raise_for_status()
        history_data = history_resp.json()
        rates = [float(item.get("fundingRate", 0.0)) for item in history_data if "fundingRate" in item]
        avg_recent_rate = statistics.fmean(rates) if rates else current_rate
        return summarize_funding_summary(symbol.upper(), current_rate, avg_recent_rate, settings)
    except Exception as exc:  # noqa: BLE001
        return default_funding_summary(
            symbol,
            enabled=True,
            available=False,
            reason=f"Funding overlay 取得失敗，回退中性：{exc}",
        )


def build_swing_mtf_score(tf15m: dict[str, Any], tf1h: dict[str, Any], tf4h: dict[str, Any]) -> dict[str, Any]:
    weights = {"15m": 0.2, "1h": 0.3, "4h": 0.5}

    def tf_score(tf: dict[str, Any]) -> float:
        score = 50.0
        trend = str(tf.get("trend", "mixed"))
        if trend == "bullish":
            score += 18
        elif trend == "bearish":
            score -= 18

        rsi = float(tf.get("rsi14", 50.0))
        if rsi >= 50:
            score += min(10.0, (rsi - 50.0) / 2.0)
        else:
            score -= min(10.0, (50.0 - rsi) / 2.0)

        ret = float(tf.get("return_6bar_pct", 0.0))
        if ret >= 0:
            score += min(12.0, ret * 4.0)
        else:
            score -= min(12.0, abs(ret) * 4.0)

        score += 5.0 if bool(tf.get("above_vwap")) else -5.0
        return clamp(score, 0.0, 100.0)

    per_tf = {
        "15m": round(tf_score(tf15m), 2),
        "1h": round(tf_score(tf1h), 2),
        "4h": round(tf_score(tf4h), 2),
    }
    score = sum(per_tf[tf] * weight for tf, weight in weights.items())
    bias = "neutral"
    if score >= 62:
        bias = "long"
    elif score <= 38:
        bias = "short"
    return {
        "score": round(score, 2),
        "bias": bias,
        "components": per_tf,
        "weights": weights,
    }
    short_plan = {
        "trigger_price": round(short_trigger, 4),
        "entry_zone": [round(short_trigger - 0.55 * atr_abs, 4), round(short_trigger, 4)],
        "stop_loss": round(short_trigger + 1.25 * atr_abs, 4),
        "take_profit": [round(short_trigger - 2.4 * atr_abs, 4), round(short_trigger - 3.8 * atr_abs, 4)],
        "condition": "以 1h 收線跌破轉弱價，4h 結構不轉強，量能至少高於近 20 根均量。",
        "setup_type": "波段轉弱",
        "confirmation": {
            "first_breakout_watch": round(short_trigger, 4),
            "retest_zone": [round(short_retest_zone[0], 4), round(short_retest_zone[1], 4)],
            "second_breakout_trigger": round(short_second_breakdown, 4),
            "retest_failure_level": round(short_retest_failure, 4),
            "notes": "先看 1h 收線跌破，再等反抽不回去或第二次轉弱。",
        },
        "management": {
            "breakeven_trigger": round(short_breakeven_trigger, 4),
            "breakeven_stop": round(short_trigger, 4),
            "scale_out_zone": [round(short_scale_out_zone[0], 4), round(short_scale_out_zone[1], 4)],
            "runner_zone": [round(short_runner_zone[0], 4), round(short_runner_zone[1], 4)],
            "notes": "中線單以分批減碼為主，不追求最短線保本。",
        },
    }

    preferred_side = "neutral"
    preferred_distance = long_trigger_distance if long_trigger_distance["distance_abs"] <= short_trigger_distance["distance_abs"] else short_trigger_distance
    if analysis_direction == "long_bias":
        preferred_side = "long"
        preferred_distance = long_trigger_distance
    elif analysis_direction == "short_bias":
        preferred_side = "short"
        preferred_distance = short_trigger_distance

    execution_readiness = preferred_distance["readiness"] if analysis_direction != "neutral" else (
        "balanced" if preferred_distance["readiness"] == "far" else preferred_distance["readiness"]
    )
    readiness_label = {
        "ready": "可規劃",
        "caution": "接近但需耐心",
        "far": "距離仍遠",
        "balanced": "雙邊觀察",
    }[execution_readiness]
    signal_strength = swing_signal["strength"]

    recommendation = "先看 1h/4h 的波段轉強或轉弱條件，不再以短線追價為主。"
    if analysis_direction == "long_bias":
        if long_trigger_distance["readiness"] == "far":
            direction = "neutral"
            signal_strength = "low" if signal_strength != "low" else signal_strength
            recommendation = "上方劇本存在，但轉強價距離現價偏遠，先觀察，不追價。"
        elif long_trigger_distance["readiness"] == "caution":
            recommendation = "上方劇本較優先，等 1h 靠近轉強區後再看收線。"
        else:
            recommendation = "上方劇本優先，等 1h 收線站穩後再規劃波段單。"
    elif analysis_direction == "short_bias":
        if short_trigger_distance["readiness"] == "far":
            direction = "neutral"
            signal_strength = "low" if signal_strength != "low" else signal_strength
            recommendation = "下方劇本存在，但轉弱價距離現價偏遠，先觀察，不提前空。"
        elif short_trigger_distance["readiness"] == "caution":
            recommendation = "下方劇本較優先，等 1h 靠近轉弱區後再看收線。"
        else:
            recommendation = "下方劇本優先，等 1h 收線跌破後再規劃波段單。"

    def build_executor_plan(side: str, setup: dict[str, Any]) -> dict[str, Any]:
        entry_trigger = float(setup["confirmation"]["second_breakout_trigger"])
        stop_loss = float(setup["stop_loss"])
        tp1 = float(setup["take_profit"][0])
        tp2 = float(setup["take_profit"][1])
        if side == "long":
            risk_pct = abs((entry_trigger - stop_loss) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp1_pct = abs((tp1 - entry_trigger) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp2_pct = abs((tp2 - entry_trigger) / entry_trigger * 100.0) if entry_trigger else 0.0
        else:
            risk_pct = abs((stop_loss - entry_trigger) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp1_pct = abs((entry_trigger - tp1) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp2_pct = abs((entry_trigger - tp2) / entry_trigger * 100.0) if entry_trigger else 0.0
        rr_to_tp1 = (reward_tp1_pct / risk_pct) if risk_pct > 0 else 0.0
        rr_to_tp2 = (reward_tp2_pct / risk_pct) if risk_pct > 0 else 0.0
        cancel_after_minutes = {"ready": 360, "caution": 480, "far": 720, "balanced": 480}.get(execution_readiness, 480)
        quality = "tradable"
        notes: list[str] = []
        if rr_to_tp1 < 1.0 or risk_pct > 3.5:
            quality = "observe_only"
        elif rr_to_tp1 < 1.2 or risk_pct > 2.8:
            quality = "caution"
        if onchain_summary.get("available"):
            notes.append(f"鏈上偏向 {onchain_summary.get('bias', 'neutral')}")
        return {
            "order_type": "stop_market",
            "entry_trigger": round(entry_trigger, 4),
            "cancel_after_minutes": cancel_after_minutes,
            "risk_pct": round(risk_pct, 3),
            "reward_tp1_pct": round(reward_tp1_pct, 3),
            "reward_tp2_pct": round(reward_tp2_pct, 3),
            "rr_to_tp1": round(rr_to_tp1, 3),
            "rr_to_tp2": round(rr_to_tp2, 3),
            "quality": quality,
            "notes": notes,
        }

    long_plan["executor_plan"] = build_executor_plan("long", long_plan)
    short_plan["executor_plan"] = build_executor_plan("short", short_plan)

    return {
        "analysis_bias": analysis_direction,
        "direction_bias": direction,
        "recommendation": recommendation,
        "timeframe_weight": "4h/24h:70%, 1h:30%",
        "tf_1h_trend": tf1h["trend"],
        "tf_4h_trend": swing_signal.get("checks", {}).get("long_core", {}).get("4h_trend", False),
        "signal_strength": signal_strength,
        "analysis_signal_strength": swing_signal["strength"],
        "execution_readiness": execution_readiness,
        "execution_readiness_label": readiness_label,
        "preferred_setup": preferred_side,
        "trigger_distance": {
            "long": long_trigger_distance,
            "short": short_trigger_distance,
            "preferred": preferred_distance,
        },
        "confirmation_priority": "1h_close_then_retest",
        "long_setup": long_plan,
        "short_setup": short_plan,
        "strategy_style": "swing",
    }


def signed_spot_move_pct(entry_price: float, target_price: float) -> float:
    if entry_price == 0:
        return 0.0
    return (target_price / entry_price - 1.0) * 100.0


def position_roi_pct(spot_move_pct: float, side: str, leverage: float) -> float:
    if side == "short":
        return -spot_move_pct * leverage
    return spot_move_pct * leverage


def account_pnl_pct(position_roi: float, account_allocation_pct: float) -> float:
    return position_roi * (account_allocation_pct / 100.0)


def build_position_examples(
    long_short_plan: dict[str, Any],
    leverage: float = DEFAULT_LEVERAGE,
    account_allocation_pct: float = DEFAULT_ACCOUNT_ALLOCATION_PCT,
) -> dict[str, Any]:
    direction_bias = str(long_short_plan.get("direction_bias", "neutral"))
    side = "long" if direction_bias == "long_bias" else "short" if direction_bias == "short_bias" else "neutral"
    if side == "neutral":
        return {
            "assumptions": {
                "leverage": leverage,
                "account_allocation_pct": account_allocation_pct,
            },
            "recommended_side": "neutral",
        }

    setup = long_short_plan["long_setup"] if side == "long" else long_short_plan["short_setup"]
    entry = float(setup["trigger_price"])
    stop_loss = float(setup["stop_loss"])
    tp1 = float(setup["take_profit"][0])
    tp2 = float(setup["take_profit"][1])
    management = setup.get("management", {})

    def metric(target: float) -> dict[str, float]:
        spot_move = signed_spot_move_pct(entry, target)
        position_roi = position_roi_pct(spot_move, side, leverage)
        account_pnl = account_pnl_pct(position_roi, account_allocation_pct)
        return {
            "spot_move_pct": round(spot_move, 3),
            "position_roi_pct": round(position_roi, 3),
            "account_pnl_pct": round(account_pnl, 3),
        }

    return {
        "assumptions": {
            "leverage": leverage,
            "account_allocation_pct": account_allocation_pct,
        },
        "recommended_side": side,
        "reference_entry": round(entry, 4),
        "recommended_stop_loss": round(stop_loss, 4),
        "recommended_take_profit_1": round(tp1, 4),
        "recommended_take_profit_2": round(tp2, 4),
        "move_stop_to_breakeven_at": round(float(management.get("breakeven_trigger", entry)), 4),
        "breakeven_stop_price": round(float(management.get("breakeven_stop", entry)), 4),
        "scale_out_zone": [round(float(x), 4) for x in management.get("scale_out_zone", [tp1, tp1])],
        "runner_zone": [round(float(x), 4) for x in management.get("runner_zone", [tp2, tp2])],
        "to_stop_loss": metric(stop_loss),
        "to_take_profit_1": metric(tp1),
        "to_take_profit_2": metric(tp2),
    }


def build_price_map(
    price: float,
    market_state: dict[str, Any],
    actionable_levels: dict[str, Any],
    long_short_plan: dict[str, Any],
) -> dict[str, Any]:
    range_low = float(actionable_levels["range_low"])
    range_high = float(actionable_levels["range_high"])
    breakout_up = float(actionable_levels["breakout_up"])
    breakout_down = float(actionable_levels["breakout_down"])
    noise_low = float(actionable_levels.get("noise_zone", [range_low, range_high])[0])
    noise_high = float(actionable_levels.get("noise_zone", [range_low, range_high])[1])
    long_ready_zone = [float(x) for x in actionable_levels.get("long_ready_zone", [breakout_up, breakout_up])]
    short_ready_zone = [float(x) for x in actionable_levels.get("short_ready_zone", [breakout_down, breakout_down])]
    long_setup = long_short_plan["long_setup"]
    short_setup = long_short_plan["short_setup"]
    long_retest_zone = [float(x) for x in long_setup["confirmation"]["retest_zone"]]
    short_retest_zone = [float(x) for x in short_setup["confirmation"]["retest_zone"]]
    long_tp1 = float(long_setup["take_profit"][0])
    long_tp2 = float(long_setup["take_profit"][1])
    short_tp1 = float(short_setup["take_profit"][0])
    short_tp2 = float(short_setup["take_profit"][1])

    def zone(a: float, b: float) -> list[float]:
        return [round(min(a, b), 4), round(max(a, b), 4)]

    phase = "橫盤中段"
    phase_reason = "價格仍在箱體中央，先看上下破再決定方向。"
    primary_support = zone(breakout_down, short_ready_zone[1])
    secondary_support = zone(range_low, noise_low)
    primary_resistance = zone(long_ready_zone[0], breakout_up)
    secondary_resistance = zone(noise_high, range_high)
    if_break_down = f"若跌破 `{breakout_down:.4f}`，下一支撐先看 `{short_tp1:.4f}`，再看 `{short_tp2:.4f}`。"
    if_break_up = f"若站上 `{breakout_up:.4f}`，下一壓力先看 `{long_tp1:.4f}`，再看 `{long_tp2:.4f}`。"

    if price >= breakout_up:
        phase = "上破延伸"
        phase_reason = "價格已在上破價上方，先看回踩支撐是否守住。"
        primary_support = zone(long_retest_zone[0], long_retest_zone[1])
        secondary_support = zone(noise_high, range_high)
        primary_resistance = zone(long_tp1, long_tp1)
        secondary_resistance = zone(long_tp2, long_tp2)
        if_break_down = f"若跌回 `{long_retest_zone[0]:.4f}` 下方，下一支撐看 `{secondary_support[0]:.4f} ~ {secondary_support[1]:.4f}`。"
        if_break_up = f"若續強站上 `{long_tp1:.4f}`，下一壓力看 `{long_tp2:.4f}`。"
    elif price <= breakout_down:
        phase = "下破延伸"
        phase_reason = "價格已在下破價下方，先看反抽壓力是否站不回。"
        primary_support = zone(short_tp1, short_tp1)
        secondary_support = zone(short_tp2, short_tp2)
        primary_resistance = zone(short_retest_zone[0], short_retest_zone[1])
        secondary_resistance = zone(range_low, noise_low)
        if_break_down = f"若再跌破 `{short_tp1:.4f}`，下一支撐看 `{short_tp2:.4f}`。"
        if_break_up = f"若站回 `{short_retest_zone[1]:.4f}` 上方，下一壓力看 `{secondary_resistance[0]:.4f} ~ {secondary_resistance[1]:.4f}`。"
    elif price > noise_high:
        phase = "箱體上緣"
        phase_reason = "價格靠近箱體上緣，容易在上破前先震盪。"
        primary_support = zone(noise_high, range_high)
        secondary_support = zone(range_low, noise_low)
        primary_resistance = zone(long_ready_zone[0], breakout_up)
        secondary_resistance = zone(long_tp1, long_tp1)
        if_break_down = f"若跌回 `{primary_support[0]:.4f}` 下方，下一支撐看 `{secondary_support[0]:.4f} ~ {secondary_support[1]:.4f}`。"
        if_break_up = f"若站上 `{breakout_up:.4f}`，下一壓力看 `{long_tp1:.4f}`。"
    elif price < noise_low:
        phase = "箱體下緣"
        phase_reason = "價格靠近箱體下緣，容易在下破前先反覆。"
        primary_support = zone(breakout_down, short_ready_zone[1])
        secondary_support = zone(short_tp1, short_tp1)
        primary_resistance = zone(range_low, noise_low)
        secondary_resistance = zone(noise_high, range_high)
        if_break_down = f"若跌破 `{breakout_down:.4f}`，下一支撐看 `{short_tp1:.4f}`。"
        if_break_up = f"若站回 `{primary_resistance[1]:.4f}` 上方，下一壓力看 `{secondary_resistance[0]:.4f} ~ {secondary_resistance[1]:.4f}`。"

    timing_note = (
        f"若仍在橫盤，粗略時間窗先看 {actionable_levels.get('timing_window')}（{actionable_levels.get('timing_confidence')}），"
        "但時間只供參考，價位優先。"
    )
    return {
        "phase": phase,
        "phase_reason": phase_reason,
        "primary_support": primary_support,
        "secondary_support": secondary_support,
        "primary_resistance": primary_resistance,
        "secondary_resistance": secondary_resistance,
        "if_break_down": if_break_down,
        "if_break_up": if_break_up,
        "timing_note": timing_note,
    }


def build_beginner_summary(
    symbol: str,
    decision: str,
    risk_level: str,
    risk_score: int,
    trend_view: str,
    news_summary: dict[str, Any],
    trade_plan: dict[str, Any],
    market_state: dict[str, Any],
    actionable_levels: dict[str, Any],
    long_short_plan: dict[str, Any],
    short_term_signal: dict[str, Any],
    position_examples: dict[str, Any],
    protections: dict[str, Any],
) -> dict[str, Any]:
    price_map = actionable_levels.get("price_map", {})
    trend_zh = {"bullish": "偏多", "bearish": "偏空", "mixed": "震盪偏盤整"}.get(trend_view, trend_view)
    headline = f"{symbol}：{decision_zh(decision)}（風險{risk_zh(risk_level)} {risk_score}/100）"
    sideway_text = "目前屬於橫盤" if market_state.get("is_sideways") else "目前不在明顯橫盤"
    core_reason = f"目前趨勢{trend_zh}，{sideway_text}。"
    if decision == "avoid":
        now_action = "先不要進場，等風險分數下降或方向更清楚再看。"
    elif decision == "watch":
        direction_bias = long_short_plan.get("direction_bias", "neutral")
        analysis_bias = long_short_plan.get("analysis_bias", direction_bias)
        execution_readiness = long_short_plan.get("execution_readiness", "ready")
        if analysis_bias == "long_bias" and execution_readiness == "far":
            now_action = (
                f"上方劇本存在，但上破價 `{long_short_plan['long_setup']['trigger_price']}` 離現價仍遠，"
                "先當觀察輪，不追價，等到價提醒或下一輪重算。"
            )
        elif analysis_bias == "short_bias" and execution_readiness == "far":
            now_action = (
                f"下方劇本存在，但下破價 `{long_short_plan['short_setup']['trigger_price']}` 離現價仍遠，"
                "先當觀察輪，不提前空，等到價提醒或下一輪重算。"
            )
        elif analysis_bias == "long_bias" and execution_readiness == "caution":
            now_action = (
                f"上方劇本較優先，但先等價格靠近短線執行區（做多） `{actionable_levels.get('long_ready_zone', ['-', '-'])[0]} ~ "
                f"{actionable_levels.get('long_ready_zone', ['-', '-'])[1]}`，"
                "不要在中間觀望區追單。"
            )
        elif analysis_bias == "short_bias" and execution_readiness == "caution":
            now_action = (
                f"下方劇本較優先，但先等價格靠近短線執行區（做空） `{actionable_levels.get('short_ready_zone', ['-', '-'])[0]} ~ "
                f"{actionable_levels.get('short_ready_zone', ['-', '-'])[1]}`，"
                "不要在中間觀望區提早空。"
            )
        elif direction_bias == "long_bias":
            now_action = (
                f"如果要走上方劇本，先等價格靠近短線執行區（做多） `{actionable_levels.get('long_ready_zone', ['-', '-'])[0]} ~ "
                f"{actionable_levels.get('long_ready_zone', ['-', '-'])[1]}`，"
                "再看 5m 是否帶量突破，不要在中間觀望區追單。"
            )
        elif direction_bias == "short_bias":
            now_action = (
                f"如果要走下方劇本，先等價格靠近短線執行區（做空） `{actionable_levels.get('short_ready_zone', ['-', '-'])[0]} ~ "
                f"{actionable_levels.get('short_ready_zone', ['-', '-'])[1]}`，"
                "再看 5m 是否帶量跌破，不要在中間觀望區追單。"
            )
        else:
            now_action = "先觀望，等價格靠近兩側短線執行區後，再選擇上方劇本或下方劇本。"
    else:
        now_action = (
            f"若要執行，請用小額分批：參考進場區 {trade_plan.get('entry_zone')}，"
            f"並先寫好停損 {trade_plan.get('stop_loss')}。"
        )
    reminder = (
        f"提醒價位：上破 `{actionable_levels.get('breakout_up')}` 代表上方劇本開始成立，"
        f"下破 `{actionable_levels.get('breakout_down')}` 代表下方劇本開始成立。"
        f" 短線執行區（做多） `{actionable_levels.get('long_ready_zone', ['-', '-'])[0]} ~ {actionable_levels.get('long_ready_zone', ['-', '-'])[1]}`；"
        f"短線執行區（做空） `{actionable_levels.get('short_ready_zone', ['-', '-'])[0]} ~ {actionable_levels.get('short_ready_zone', ['-', '-'])[1]}`。"
    )
    range_hint = (
        f"大區間：`{actionable_levels.get('range_low')} ~ {actionable_levels.get('range_high')}`；"
        f"中間觀望區：`{actionable_levels.get('noise_zone', ['-', '-'])[0]} ~ {actionable_levels.get('noise_zone', ['-', '-'])[1]}`；"
        f"預估出方向時間：{actionable_levels.get('timing_window')}（{actionable_levels.get('timing_confidence')}）。"
    )
    phase_hint = (
        f"目前階段：{price_map.get('phase', '橫盤中段')}。"
        f"{price_map.get('phase_reason', '先看上下破，再看是否延續。')}"
    )
    path_map_hint = (
        f"第一支撐 `{price_map.get('primary_support', ['-', '-'])[0]} ~ {price_map.get('primary_support', ['-', '-'])[1]}`；"
        f"第二支撐 `{price_map.get('secondary_support', ['-', '-'])[0]} ~ {price_map.get('secondary_support', ['-', '-'])[1]}`；"
        f"第一壓力 `{price_map.get('primary_resistance', ['-', '-'])[0]} ~ {price_map.get('primary_resistance', ['-', '-'])[1]}`；"
        f"第二壓力 `{price_map.get('secondary_resistance', ['-', '-'])[0]} ~ {price_map.get('secondary_resistance', ['-', '-'])[1]}`。"
    )
    break_path_hint = (
        f"{price_map.get('if_break_down', '')}"
        f" {price_map.get('if_break_up', '')}"
        f" {price_map.get('timing_note', '')}"
    ).strip()
    protection_hint = protection_summary_text(protections)
    long_executor = long_short_plan["long_setup"].get("executor_plan", {})
    short_executor = long_short_plan["short_setup"].get("executor_plan", {})
    long_short_hint = (
        f"做多觸發 `{long_short_plan['long_setup']['trigger_price']}`；"
        f"做空觸發 `{long_short_plan['short_setup']['trigger_price']}`；"
        f"目前劇本：{long_short_plan['recommendation']}"
    )
    executor_hint = (
        f"做多執行：`{long_executor.get('order_type', '-')}`，"
        f"RR(TP1) `{long_executor.get('rr_to_tp1', '-')}`，"
        f"品質 `{long_executor.get('quality', '-')}`，"
        f"未成交約 `{long_executor.get('cancel_after_minutes', '-')}` 分鐘後取消。"
        f" 做空執行：`{short_executor.get('order_type', '-')}`，"
        f"RR(TP1) `{short_executor.get('rr_to_tp1', '-')}`，"
        f"品質 `{short_executor.get('quality', '-')}`，"
        f"未成交約 `{short_executor.get('cancel_after_minutes', '-')}` 分鐘後取消。"
    )
    execution_hint = (
        f"做多：第一次突破看 `{long_short_plan['long_setup']['confirmation']['first_breakout_watch']}`，"
        f"回踩確認區 `{long_short_plan['long_setup']['confirmation']['retest_zone'][0]} ~ "
        f"{long_short_plan['long_setup']['confirmation']['retest_zone'][1]}`，"
        f"第二次突破價 `{long_short_plan['long_setup']['confirmation']['second_breakout_trigger']}`。"
        f" 做空：第一次跌破看 `{long_short_plan['short_setup']['confirmation']['first_breakout_watch']}`，"
        f"反抽確認區 `{long_short_plan['short_setup']['confirmation']['retest_zone'][0]} ~ "
        f"{long_short_plan['short_setup']['confirmation']['retest_zone'][1]}`，"
        f"第二次跌破價 `{long_short_plan['short_setup']['confirmation']['second_breakout_trigger']}`。"
    )
    stop_hint = "建議止損：方向未明，先等突破再決定。"
    exit_hint = "短線出場：先看保本上移條件，再看分批止盈區。"
    leverage_hint = ""
    if position_examples.get("recommended_side") == "long":
        to_stop = position_examples["to_stop_loss"]
        stop_hint = (
            f"建議止損：做多若失守 `{position_examples['recommended_stop_loss']}` 就撤。"
            f" 從上破價算，現貨止損約 `{abs(to_stop['spot_move_pct'])}%`。"
        )
        exit_hint = (
            f"短線出場：價格到 `{position_examples['move_stop_to_breakeven_at']}` 後，"
            f"止損可上移到保本 `{position_examples['breakeven_stop_price']}`；"
            f"分批止盈區 `{position_examples['scale_out_zone'][0]} ~ {position_examples['scale_out_zone'][1]}`；"
            f"續抱區 `{position_examples['runner_zone'][0]} ~ {position_examples['runner_zone'][1]}`。"
        )
        leverage_hint = (
            f"現貨角度：上破價 `{position_examples['reference_entry']}`，"
            f"止損 `{position_examples['recommended_stop_loss']}`，"
            f"止損幅度約 `{abs(to_stop['spot_move_pct'])}%`。"
        )
    elif position_examples.get("recommended_side") == "short":
        to_stop = position_examples["to_stop_loss"]
        stop_hint = (
            f"建議止損：做空若站回 `{position_examples['recommended_stop_loss']}` 就撤。"
            f" 從下破價算，現貨止損約 `{abs(to_stop['spot_move_pct'])}%`。"
        )
        exit_hint = (
            f"短線出場：價格到 `{position_examples['move_stop_to_breakeven_at']}` 後，"
            f"止損可下移到保本 `{position_examples['breakeven_stop_price']}`；"
            f"分批止盈區 `{position_examples['scale_out_zone'][0]} ~ {position_examples['scale_out_zone'][1]}`；"
            f"續抱區 `{position_examples['runner_zone'][0]} ~ {position_examples['runner_zone'][1]}`。"
        )
        leverage_hint = (
            f"現貨角度：下破價 `{position_examples['reference_entry']}`，"
            f"止損 `{position_examples['recommended_stop_loss']}`，"
            f"止損幅度約 `{abs(to_stop['spot_move_pct'])}%`。"
        )
    short_bias_hint = (
        f"短線方向：`{short_term_signal['bias']}`，強度：`{short_term_signal['strength']}`，"
        f"市場狀態：`{short_term_signal.get('market_regime', 'range_or_mixed')}`。"
    )
    avoid_list = [
        "不要滿倉或重倉一次梭哈",
        "不要不設停損",
        "不要因為一根紅K就追單",
    ]
    return {
        "headline": headline,
        "core_reason": core_reason,
        "now_action": now_action,
        "reminder": reminder,
        "range_hint": range_hint,
        "phase_hint": phase_hint,
        "path_map_hint": path_map_hint,
        "break_path_hint": break_path_hint,
        "protection_hint": protection_hint,
        "long_short_hint": long_short_hint,
        "executor_hint": executor_hint,
        "execution_hint": execution_hint,
        "stop_hint": stop_hint,
        "exit_hint": exit_hint,
        "leverage_hint": leverage_hint,
        "short_bias_hint": short_bias_hint,
        "avoid": avoid_list,
    }


def propose_trade_plan(price: float, atr_pct: float, trend_view: str, max_risk_pct: float) -> dict[str, Any]:
    if atr_pct <= 0:
        atr_pct = 1.2
    unit = atr_pct / 100
    if trend_view == "bearish":
        entry_low = price * (1 + 0.1 * unit)
        entry_high = price * (1 + 0.4 * unit)
        stop = price * (1 + 1.1 * unit)
        tp1 = price * (1 - 1.6 * unit)
        tp2 = price * (1 - 2.8 * unit)
    else:
        entry_low = price * (1 - 0.4 * unit)
        entry_high = price * (1 - 0.1 * unit)
        stop = price * (1 - 1.1 * unit)
        tp1 = price * (1 + 1.6 * unit)
        tp2 = price * (1 + 2.8 * unit)
    return {
        "entry_zone": [round(entry_low, 4), round(entry_high, 4)],
        "stop_loss": round(stop, 4),
        "take_profit": [round(tp1, 4), round(tp2, 4)],
        "max_risk_per_trade_pct": max_risk_pct,
    }


def build_rule_based_decision(
    symbol: str,
    price: float,
    returns: dict[str, float],
    volatility: dict[str, float],
    trend: dict[str, Any],
    volume_ratio: float,
    news_summary: dict[str, Any],
    profile: str,
) -> dict[str, Any]:
    risk_score, risk_parts = score_risk(returns, volatility, trend, volume_ratio, news_summary)
    watch_th, avoid_th, max_risk = thresholds(profile)
    risk_level = classify_risk(risk_score)
    decision = "scale_in_test"
    warnings: list[str] = []
    if risk_score >= avoid_th:
        decision = "avoid"
    elif risk_score >= watch_th:
        decision = "watch"

    if trend["trend_view"] == "bearish" and decision == "scale_in_test":
        decision = "watch"
        warnings.append("趨勢偏空，現貨不建議追多。")
    if abs(returns["5m"]) > 2.5:
        warnings.append("15分鐘內波動可能放大，避免市價追單。")
        if decision == "scale_in_test":
            decision = "watch"

    confidence = clamp(0.45 + (100 - risk_score) / 220, 0.35, 0.85)
    plan = propose_trade_plan(price, volatility["atr_pct"], trend["trend_view"], max_risk)
    thesis = [
        f"短線趨勢：{trend['trend_view']}，RSI={trend['rsi14']:.1f}",
        f"24h波動={volatility['realized_24h']:.2f}% / ATR={volatility['atr_pct']:.2f}%",
        f"風險分解={risk_parts}",
    ]
    return {
        "decision": decision,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "confidence": round(confidence, 3),
        "trend_view": trend["trend_view"],
        "thesis": thesis,
        "trade_plan": plan,
        "warnings": warnings,
        "model_source": "rule_engine",
    }


def extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = text[start : i + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    return None
    return None


def try_llama_analysis(
    model: str,
    timeout: int,
    features: dict[str, Any],
    rule_based: dict[str, Any],
) -> dict[str, Any]:
    system_prompt = (
        "你是頂尖交易顧問與風控長。優先保護本金，資料不足時只能輸出 watch 或 avoid。"
        "請只輸出JSON，不要多餘文字。"
    )
    schema_hint = {
        "decision": "scale_in_test|watch|avoid",
        "confidence": 0.0,
        "risk_score": 0,
        "risk_level": "low|medium|high|extreme",
        "trend_view": "bullish|mixed|bearish",
        "thesis": ["理由1", "理由2"],
        "trade_plan": {
            "entry_zone": [0.0, 0.0],
            "stop_loss": 0.0,
            "take_profit": [0.0, 0.0],
            "max_risk_per_trade_pct": 1.0,
        },
        "warnings": ["警示1"],
    }
    user_prompt = {
        "features": features,
        "baseline_rule_engine": rule_based,
        "task": "輸出最終決策JSON。不得移除停損欄位。",
        "schema": schema_hint,
    }
    payload_chat = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "stream": False,
        "format": "json",
    }
    payload_generate = {
        "model": model,
        "prompt": (
            f"{system_prompt}\n\n"
            f"以下是輸入資料(JSON):\n{json.dumps(user_prompt, ensure_ascii=False)}\n\n"
            "只輸出JSON，不要多餘文字。"
        ),
        "stream": False,
        "format": "json",
    }
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    content = ""
    errors: list[str] = []

    try:
        r = requests.post(
            "http://localhost:11434/api/chat",
            data=json.dumps(payload_chat),
            headers=headers,
            timeout=timeout,
        )
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"/api/chat: {exc}")

    if not content:
        try:
            r2 = requests.post(
                "http://localhost:11434/api/generate",
                data=json.dumps(payload_generate),
                headers=headers,
                timeout=timeout,
            )
            r2.raise_for_status()
            content = r2.json().get("response", "")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"/api/generate: {exc}")

    if not content:
        payload_openai = {
            "model": model,
            "messages": payload_chat["messages"],
            "response_format": {"type": "json_object"},
        }
        try:
            r3 = requests.post(
                "http://localhost:11434/v1/chat/completions",
                data=json.dumps(payload_openai),
                headers=headers,
                timeout=timeout,
            )
            r3.raise_for_status()
            content = (
                r3.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"/v1/chat/completions: {exc}")

    if not content:
        raise RuntimeError(" ; ".join(errors))

    parsed = extract_json_object(content) or json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("llama 輸出不是 JSON object")
    parsed["model_source"] = f"llama:{model}"
    return parsed


def normalize_llm_output(llm_res: dict[str, Any]) -> dict[str, Any] | None:
    allowed_decisions = {"scale_in_test", "watch", "avoid"}
    allowed_trends = {"bullish", "mixed", "bearish"}
    try:
        decision = str(llm_res.get("decision", "")).strip().lower()
        if decision not in allowed_decisions:
            return None
        risk_score = int(round(float(llm_res.get("risk_score", 0))))
        risk_score = int(clamp(risk_score, 0, 100))
        confidence = float(llm_res.get("confidence", 0.0))
        if not (0.0 <= confidence <= 1.0):
            return None
        # 過低信心通常代表模型輸出壞掉，直接視為不可信。
        if confidence < 0.1:
            return None
        trend_view = str(llm_res.get("trend_view", "mixed")).strip().lower()
        if trend_view not in allowed_trends:
            trend_view = "mixed"
        thesis = [str(x) for x in llm_res.get("thesis", []) if str(x).strip()]
        if not thesis:
            return None
        warnings = [str(x) for x in llm_res.get("warnings", []) if str(x).strip()]
        plan = llm_res.get("trade_plan")
        if not isinstance(plan, dict):
            return None
        required_plan_keys = {"entry_zone", "stop_loss", "take_profit", "max_risk_per_trade_pct"}
        if not required_plan_keys.issubset(plan.keys()):
            return None
        return {
            "decision": decision,
            "confidence": round(confidence, 3),
            "risk_score": risk_score,
            "risk_level": classify_risk(risk_score),
            "trend_view": trend_view,
            "thesis": thesis,
            "trade_plan": plan,
            "warnings": warnings,
            "model_source": llm_res.get("model_source", "llm"),
        }
    except Exception:  # noqa: BLE001
        return None


def discover_ollama_model(timeout: int) -> str | None:
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get("http://localhost:11434/v1/models", headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data and isinstance(data, list):
            mid = data[0].get("id")
            if mid:
                return str(mid)
    except Exception:  # noqa: BLE001
        pass
    try:
        r = requests.get("http://localhost:11434/api/tags", headers=headers, timeout=timeout)
        r.raise_for_status()
        models = r.json().get("models", [])
        if models and isinstance(models, list):
            name = models[0].get("name")
            if name:
                return str(name)
    except Exception:  # noqa: BLE001
        pass
    return None


def ensure_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market_source TEXT NOT NULL,
            current_price REAL NOT NULL,
            risk_score INTEGER NOT NULL,
            risk_level TEXT NOT NULL,
            decision TEXT NOT NULL,
            confidence REAL NOT NULL,
            trend_view TEXT NOT NULL,
            returns_json TEXT NOT NULL,
            news_json TEXT NOT NULL,
            thesis_json TEXT NOT NULL,
            trade_plan_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            model_source TEXT NOT NULL,
            short_term_signal_json TEXT,
            direction_bias TEXT,
            timeframe_view_json TEXT,
            long_short_plan_json TEXT
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
    migrations = {
        "short_term_signal_json": "ALTER TABLE signals ADD COLUMN short_term_signal_json TEXT",
        "direction_bias": "ALTER TABLE signals ADD COLUMN direction_bias TEXT",
        "timeframe_view_json": "ALTER TABLE signals ADD COLUMN timeframe_view_json TEXT",
        "long_short_plan_json": "ALTER TABLE signals ADD COLUMN long_short_plan_json TEXT",
    }
    for col, sql in migrations.items():
        if col not in existing:
            conn.execute(sql)
    conn.commit()
    return conn


def log_signal(conn: sqlite3.Connection, run_id: str, a: SymbolAnalysis) -> None:
    conn.execute(
        """
        INSERT INTO signals (
            run_id, timestamp, symbol, market_source, current_price,
            risk_score, risk_level, decision, confidence, trend_view,
            returns_json, news_json, thesis_json, trade_plan_json,
            warnings_json, model_source, short_term_signal_json,
            direction_bias, timeframe_view_json, long_short_plan_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            a.timestamp,
            a.symbol,
            a.market_source,
            a.price,
            a.risk_score,
            a.risk_level,
            a.decision,
            a.confidence,
            a.trend_view,
            json.dumps(a.returns, ensure_ascii=False),
            json.dumps({"items": a.news, "summary": a.news_summary}, ensure_ascii=False),
            json.dumps(a.thesis, ensure_ascii=False),
            json.dumps(a.trade_plan, ensure_ascii=False),
            json.dumps(a.warnings, ensure_ascii=False),
            a.model_source,
            json.dumps(a.short_term_signal, ensure_ascii=False),
            a.short_term_signal["bias"],
            json.dumps(a.timeframe_view, ensure_ascii=False),
            json.dumps(a.long_short_plan, ensure_ascii=False),
        ),
    )
    conn.commit()


def build_swing_long_short_plan(
    price: float,
    atr_pct: float,
    levels: dict[str, Any],
    tf1h: dict[str, Any],
    tf4h: dict[str, Any],
    swing_signal: dict[str, Any],
    onchain_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    onchain_summary = onchain_summary or {}

    def summarize_trigger_distance(trigger_price: float) -> dict[str, Any]:
        distance_abs = abs(trigger_price - price)
        distance_pct = (distance_abs / price * 100.0) if price > 0 else 0.0
        distance_atr = (distance_abs / atr_abs) if atr_abs > 0 else 0.0
        readiness = "ready"
        if distance_atr >= 1.8 or distance_pct >= 3.2:
            readiness = "far"
        elif distance_atr >= 1.2 or distance_pct >= 2.0:
            readiness = "caution"
        return {
            "distance_abs": round(distance_abs, 4),
            "distance_pct": round(distance_pct, 3),
            "distance_atr": round(distance_atr, 3),
            "readiness": readiness,
        }

    atr_abs = max(price * max(atr_pct, 1.0) / 100, price * 0.008)
    direction_map = {"long": "long_bias", "short": "short_bias", "neutral": "neutral"}
    analysis_direction = direction_map.get(str(swing_signal.get("bias", "neutral")), "neutral")
    direction = analysis_direction

    long_trigger = float(levels["breakout_up"])
    short_trigger = float(levels["breakout_down"])
    long_retest_zone = [float(x) for x in levels.get("long_retest_zone", [long_trigger, long_trigger])]
    short_retest_zone = [float(x) for x in levels.get("short_retest_zone", [short_trigger, short_trigger])]
    long_trigger_distance = summarize_trigger_distance(long_trigger)
    short_trigger_distance = summarize_trigger_distance(short_trigger)

    long_second_breakout = long_trigger + 0.45 * atr_abs
    short_second_breakdown = short_trigger - 0.45 * atr_abs
    long_retest_failure = long_trigger - 0.85 * atr_abs
    short_retest_failure = short_trigger + 0.85 * atr_abs
    long_breakeven_trigger = long_trigger + 1.6 * atr_abs
    short_breakeven_trigger = short_trigger - 1.6 * atr_abs
    long_scale_out_zone = [long_trigger + 2.0 * atr_abs, long_trigger + 2.8 * atr_abs]
    short_scale_out_zone = [short_trigger - 2.8 * atr_abs, short_trigger - 2.0 * atr_abs]
    long_runner_zone = [long_trigger + 3.0 * atr_abs, long_trigger + 4.5 * atr_abs]
    short_runner_zone = [short_trigger - 4.5 * atr_abs, short_trigger - 3.0 * atr_abs]

    long_plan = {
        "trigger_price": round(long_trigger, 4),
        "entry_zone": [round(long_trigger, 4), round(long_trigger + 0.55 * atr_abs, 4)],
        "stop_loss": round(long_trigger - 1.25 * atr_abs, 4),
        "take_profit": [round(long_trigger + 2.4 * atr_abs, 4), round(long_trigger + 3.8 * atr_abs, 4)],
        "condition": "1h 結構轉強，4h 不逆勢，等波段突破成立。",
        "setup_type": "波段轉強",
        "confirmation": {
            "first_breakout_watch": round(long_trigger, 4),
            "retest_zone": [round(long_retest_zone[0], 4), round(long_retest_zone[1], 4)],
            "second_breakout_trigger": round(long_second_breakout, 4),
            "retest_failure_level": round(long_retest_failure, 4),
            "notes": "先看 1h 站穩，再等回踩確認或第二次突破。",
        },
        "management": {
            "breakeven_trigger": round(long_breakeven_trigger, 4),
            "breakeven_stop": round(long_trigger, 4),
            "scale_out_zone": [round(long_scale_out_zone[0], 4), round(long_scale_out_zone[1], 4)],
            "runner_zone": [round(long_runner_zone[0], 4), round(long_runner_zone[1], 4)],
            "notes": "先收部分獲利，剩餘部位看趨勢續抱。",
        },
    }
    short_plan = {
        "trigger_price": round(short_trigger, 4),
        "entry_zone": [round(short_trigger - 0.55 * atr_abs, 4), round(short_trigger, 4)],
        "stop_loss": round(short_trigger + 1.25 * atr_abs, 4),
        "take_profit": [round(short_trigger - 2.4 * atr_abs, 4), round(short_trigger - 3.8 * atr_abs, 4)],
        "condition": "1h 結構轉弱，4h 不逆勢，等波段跌破成立。",
        "setup_type": "波段轉弱",
        "confirmation": {
            "first_breakout_watch": round(short_trigger, 4),
            "retest_zone": [round(short_retest_zone[0], 4), round(short_retest_zone[1], 4)],
            "second_breakout_trigger": round(short_second_breakdown, 4),
            "retest_failure_level": round(short_retest_failure, 4),
            "notes": "先看 1h 跌破，再等反抽失敗或第二次跌破。",
        },
        "management": {
            "breakeven_trigger": round(short_breakeven_trigger, 4),
            "breakeven_stop": round(short_trigger, 4),
            "scale_out_zone": [round(short_scale_out_zone[0], 4), round(short_scale_out_zone[1], 4)],
            "runner_zone": [round(short_runner_zone[0], 4), round(short_runner_zone[1], 4)],
            "notes": "先收部分獲利，剩餘部位看趨勢續抱。",
        },
    }

    preferred_side = "neutral"
    preferred_distance = long_trigger_distance if long_trigger_distance["distance_abs"] <= short_trigger_distance["distance_abs"] else short_trigger_distance
    if analysis_direction == "long_bias":
        preferred_side = "long"
        preferred_distance = long_trigger_distance
    elif analysis_direction == "short_bias":
        preferred_side = "short"
        preferred_distance = short_trigger_distance

    execution_readiness = preferred_distance["readiness"] if analysis_direction != "neutral" else (
        "balanced" if preferred_distance["readiness"] == "far" else preferred_distance["readiness"]
    )
    readiness_label = {
        "ready": "可開始規劃",
        "caution": "先等靠近",
        "far": "距離仍遠",
        "balanced": "雙向觀察",
    }[execution_readiness]
    signal_strength = str(swing_signal.get("strength", "medium"))
    swing_mtf_bias = str(swing_signal.get("swing_mtf_bias", "neutral"))
    funding_bias = str(swing_signal.get("funding_bias", "neutral"))
    onchain_bias = str(onchain_summary.get("bias", "neutral"))

    recommendation = "先看上下兩邊劇本，等待 1h/4h 結構與價格同步。"
    if analysis_direction == "long_bias":
        if long_trigger_distance["readiness"] == "far":
            direction = "neutral"
            signal_strength = "low" if signal_strength != "low" else signal_strength
            recommendation = "偏多，但轉強價離現價仍遠，先不要追，等靠近或下一輪。"
        elif long_trigger_distance["readiness"] == "caution":
            recommendation = "偏多，但先等價格靠近轉強價，再看 1h 是否站穩。"
        else:
            recommendation = "偏多，若上破並站穩 1h 結構，可規劃波段多單。"
    elif analysis_direction == "short_bias":
        if short_trigger_distance["readiness"] == "far":
            direction = "neutral"
            signal_strength = "low" if signal_strength != "low" else signal_strength
            recommendation = "偏空，但轉弱價離現價仍遠，先不要追，等靠近或下一輪。"
        elif short_trigger_distance["readiness"] == "caution":
            recommendation = "偏空，但先等價格靠近轉弱價，再看 1h 是否跌破。"
        else:
            recommendation = "偏空，若下破並失守 1h 結構，可規劃波段空單。"

    if swing_mtf_bias == "long" and analysis_direction == "neutral":
        recommendation = "多週期偏多，但價格還沒給出波段進場條件，先等上方劇本。"
    elif swing_mtf_bias == "short" and analysis_direction == "neutral":
        recommendation = "多週期偏空，但價格還沒給出波段進場條件，先等下方劇本。"

    if funding_bias == "bearish" and direction == "long_bias":
        direction = "neutral"
        recommendation = "技術面偏多，但 funding 過熱，先不要追多。"
    elif funding_bias == "bullish" and direction == "short_bias":
        direction = "neutral"
        recommendation = "技術面偏空，但 funding 過度偏空，先不要追空。"

    if onchain_bias == "bullish" and direction == "short_bias":
        recommendation += " 鏈上仍偏多，空單只適合保守看待。"
    elif onchain_bias == "bearish" and direction == "long_bias":
        recommendation += " 鏈上仍偏空，多單只適合保守看待。"

    def build_executor_plan(side: str, setup: dict[str, Any]) -> dict[str, Any]:
        entry_trigger = float(setup["confirmation"]["second_breakout_trigger"])
        stop_loss = float(setup["stop_loss"])
        tp1 = float(setup["take_profit"][0])
        tp2 = float(setup["take_profit"][1])
        if side == "long":
            risk_pct = abs((entry_trigger - stop_loss) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp1_pct = abs((tp1 - entry_trigger) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp2_pct = abs((tp2 - entry_trigger) / entry_trigger * 100.0) if entry_trigger else 0.0
        else:
            risk_pct = abs((stop_loss - entry_trigger) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp1_pct = abs((entry_trigger - tp1) / entry_trigger * 100.0) if entry_trigger else 0.0
            reward_tp2_pct = abs((entry_trigger - tp2) / entry_trigger * 100.0) if entry_trigger else 0.0
        rr_to_tp1 = (reward_tp1_pct / risk_pct) if risk_pct > 0 else 0.0
        rr_to_tp2 = (reward_tp2_pct / risk_pct) if risk_pct > 0 else 0.0
        cancel_after_minutes = {"ready": 240, "caution": 360, "far": 480, "balanced": 360}.get(execution_readiness, 360)
        quality = "tradable"
        notes: list[str] = []
        if rr_to_tp1 < 0.8 or risk_pct > 4.0:
            quality = "observe_only"
        elif rr_to_tp1 < 1.0 or risk_pct > 3.0:
            quality = "caution"
        if rr_to_tp1 < 1.0:
            notes.append(f"TP1 風報偏低 ({rr_to_tp1:.2f})")
        if risk_pct > 3.0:
            notes.append(f"止損空間偏大 ({risk_pct:.2f}%)")
        return {
            "order_type": "stop_market",
            "entry_trigger": round(entry_trigger, 4),
            "cancel_after_minutes": cancel_after_minutes,
            "risk_pct": round(risk_pct, 3),
            "reward_tp1_pct": round(reward_tp1_pct, 3),
            "reward_tp2_pct": round(reward_tp2_pct, 3),
            "rr_to_tp1": round(rr_to_tp1, 3),
            "rr_to_tp2": round(rr_to_tp2, 3),
            "quality": quality,
            "notes": notes,
        }

    long_plan["executor_plan"] = build_executor_plan("long", long_plan)
    short_plan["executor_plan"] = build_executor_plan("short", short_plan)

    return {
        "analysis_bias": analysis_direction,
        "direction_bias": direction,
        "recommendation": recommendation,
        "timeframe_weight": "4h:45%, 1h:35%, 15m:20%",
        "tf_1h_trend": tf1h["trend"],
        "tf_4h_trend": tf4h["trend"],
        "signal_strength": signal_strength,
        "execution_readiness": execution_readiness,
        "execution_readiness_label": readiness_label,
        "preferred_setup": preferred_side,
        "trigger_distance": {
            "long": long_trigger_distance,
            "short": short_trigger_distance,
            "preferred": preferred_distance,
        },
        "confirmation_priority": "swing_breakout_retest",
        "swing_mtf_bias": swing_mtf_bias,
        "funding_bias": funding_bias,
        "onchain_bias": onchain_bias,
        "long_setup": long_plan,
        "short_setup": short_plan,
    }


def build_symbol_analysis(
    symbol: str,
    profile: str,
    timeout: int,
    llama_mode: str,
    llama_model: str,
    rss_items: list[dict[str, Any]],
    protection_settings: dict[str, Any] | None = None,
    funding_settings: dict[str, Any] | None = None,
) -> SymbolAnalysis:
    market_source_1m, candles_1m = fetch_klines_with_fallback(symbol, "1m", 1500, timeout)
    market_source_1h, candles_1h = fetch_klines_with_fallback(symbol, "1h", 300, timeout)
    _, candles_5m = fetch_klines_with_fallback(symbol, "5m", 300, timeout)
    _, candles_15m = fetch_klines_with_fallback(symbol, "15m", 300, timeout)
    _, candles_4h = fetch_klines_with_fallback(symbol, "4h", 300, timeout)
    market_source = market_source_1m if market_source_1m == market_source_1h else f"{market_source_1m}/{market_source_1h}"
    closes_1m = [c.close for c in candles_1m]
    closes_1h = [c.close for c in candles_1h]
    price = closes_1m[-1]
    r_5m = pct_change_from_closes(closes_1m, 5)
    r_15m = pct_change_from_closes(closes_1m, 15) if len(closes_1m) > 15 else pct_change_from_closes([c.close for c in candles_15m], 1)
    r_1h = pct_change_from_closes(closes_1m, 60) if len(closes_1m) > 60 else pct_change_from_closes(closes_1h, 1)
    r_4h = pct_change_from_closes(closes_1m, 240) if len(closes_1m) > 240 else pct_change_from_closes(closes_1h, 4)
    r_24h = pct_change_from_closes(closes_1m, 1440) if len(closes_1m) > 1440 else pct_change_from_closes(closes_1h, 24)
    returns = {
        "5m": round(r_5m, 3),
        "15m": round(r_15m, 3),
        "1h": round(r_1h, 3),
        "4h": round(r_4h, 3),
        "24h": round(r_24h, 3),
    }
    vol_window = min(1440, len(closes_1m) - 1)
    rv_24h = realized_vol_pct(closes_1m, vol_window) if vol_window >= 30 else 0.0
    if rv_24h == 0.0 and len(closes_1h) > 24:
        rv_24h = realized_vol_pct(closes_1h, 24)
    volatility = {
        "atr_pct": round(calc_atr_pct(candles_1h, 14), 3),
        "realized_24h": round(rv_24h, 3),
    }
    ema20 = ema(closes_1h, 20)
    ema50 = ema(closes_1h, 50)
    rsi14 = calc_rsi(closes_1h, 14)
    trend_view = "mixed"
    if ema20 > ema50 and rsi14 >= 52:
        trend_view = "bullish"
    elif ema20 < ema50 and rsi14 <= 48:
        trend_view = "bearish"
    trend = {
        "ema20": round(ema20, 4),
        "ema50": round(ema50, 4),
        "ema20_gt_ema50": ema20 > ema50,
        "rsi14": round(rsi14, 3),
        "trend_view": trend_view,
    }
    last_vol = candles_1h[-1].volume
    prev24 = [c.volume for c in candles_1h[-25:-1]]
    avg_vol = statistics.fmean(prev24) if prev24 else last_vol
    volume_ratio = round(last_vol / avg_vol, 3) if avg_vol else 1.0
    volume = {"vol_ratio": volume_ratio}
    market_state, actionable_levels = derive_actionable_levels(
        candles_1m=candles_1m,
        price=price,
        volatility=volatility,
        returns=returns,
        trend_view=trend_view,
    )
    tf5 = summarize_timeframe(candles_5m, "5m")
    tf15m = summarize_timeframe(candles_15m, "15m")
    tf1h = summarize_timeframe(candles_1h, "1h")
    tf4h = summarize_timeframe(candles_4h, "4h")
    timeframe_view = {"5m": tf5, "15m": tf15m, "1h": tf1h, "4h": tf4h}
    false_breakout = detect_false_breakout(candles_5m, actionable_levels["breakout_up"], actionable_levels["breakout_down"])
    onchain_summary = fetch_onchain_summary(symbol, timeout)
    funding_summary = fetch_funding_summary(symbol, timeout, funding_settings)
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
    actionable_levels["price_map"] = build_price_map(price, market_state, actionable_levels, long_short_plan)
    news_items, news_summary = summarize_news(symbol, rss_items)
    baseline = build_rule_based_decision(
        symbol=symbol,
        price=price,
        returns=returns,
        volatility=volatility,
        trend=trend,
        volume_ratio=volume_ratio,
        news_summary=news_summary,
        profile=profile,
    )
    market_protections = evaluate_market_protections(
        symbol=symbol,
        returns=returns,
        volatility=volatility,
        short_term_signal=short_term_signal,
        risk_level=str(baseline["risk_level"]),
        config=protection_settings,
    )
    baseline = apply_protections_to_decision(baseline, market_protections)
    model_source = baseline["model_source"]
    decision_data = baseline
    if llama_mode != "off":
        features = {
            "symbol": symbol,
            "timestamp": utc_now().isoformat(),
            "price": price,
            "returns": returns,
            "volatility": volatility,
            "trend": trend,
            "volume": volume,
            "news_summary": news_summary,
            "funding_summary": funding_summary,
            "market_state": market_state,
            "actionable_levels": actionable_levels,
            "timeframe_view": timeframe_view,
            "long_short_plan": long_short_plan,
            "short_term_signal": short_term_signal,
        }
        try:
            model_to_use = llama_model
            if llama_mode == "auto":
                detected = discover_ollama_model(timeout)
                if detected:
                    model_to_use = detected
            llm_res = try_llama_analysis(model_to_use, timeout, features, baseline)
            normalized = normalize_llm_output(llm_res)
            if normalized is not None:
                decision_data = normalized
                model_source = llm_res.get("model_source", model_source)
            else:
                baseline["warnings"].append("llama輸出格式/數值不可靠，已回退規則引擎。")
        except Exception as exc:  # noqa: BLE001
            if llama_mode == "on":
                raise
            baseline["warnings"].append(f"llama不可用，已回退規則引擎: {exc}")
    if tf1h["trend"] != "mixed" and tf4h["trend"] != "mixed" and tf1h["trend"] != tf4h["trend"]:
        decision_data["warnings"] = list(decision_data.get("warnings", [])) + ["1h 與 4h 結構不同步，先降槓桿或縮小部位。"]
    protections = decision_data.get("protections", market_protections)
    ts = utc_now().isoformat()
    position_examples = build_position_examples(long_short_plan)
    return SymbolAnalysis(
        symbol=symbol,
        market_source=market_source,
        timestamp=ts,
        price=round(price, 6),
        returns=returns,
        volatility=volatility,
        trend=trend,
        volume=volume,
        news=news_items,
        news_summary=news_summary,
        onchain_summary=onchain_summary,
        funding_summary=funding_summary,
        risk_score=int(decision_data["risk_score"]),
        risk_level=str(decision_data["risk_level"]),
        decision=str(decision_data["decision"]),
        confidence=float(decision_data["confidence"]),
        trend_view=str(decision_data["trend_view"]),
        thesis=[str(x) for x in decision_data["thesis"]],
        trade_plan=decision_data["trade_plan"],
        warnings=[str(x) for x in decision_data["warnings"]],
        model_source=model_source,
        position_examples=position_examples,
        beginner_summary=build_beginner_summary(
            symbol=symbol,
            decision=str(decision_data["decision"]),
            risk_level=str(decision_data["risk_level"]),
            risk_score=int(decision_data["risk_score"]),
            trend_view=str(decision_data["trend_view"]),
            news_summary=news_summary,
            trade_plan=decision_data["trade_plan"],
            market_state=market_state,
            actionable_levels=actionable_levels,
            long_short_plan=long_short_plan,
            short_term_signal=short_term_signal,
            position_examples=position_examples,
            protections=protections,
        ),
        market_state=market_state,
        actionable_levels=actionable_levels,
        timeframe_view=timeframe_view,
        long_short_plan=long_short_plan,
        short_term_signal=short_term_signal,
        protections=protections,
    )


def analysis_to_dict(a: SymbolAnalysis) -> dict[str, Any]:
    return {
        "symbol": a.symbol,
        "market_source": a.market_source,
        "timestamp": a.timestamp,
        "price": a.price,
        "returns": a.returns,
        "volatility": a.volatility,
        "trend": a.trend,
        "volume": a.volume,
        "news_summary": a.news_summary,
        "news": a.news,
        "onchain_summary": a.onchain_summary,
        "funding_summary": a.funding_summary,
        "decision": a.decision,
        "confidence": a.confidence,
        "risk_score": a.risk_score,
        "risk_level": a.risk_level,
        "trend_view": a.trend_view,
        "thesis": a.thesis,
        "trade_plan": a.trade_plan,
        "warnings": a.warnings,
        "model_source": a.model_source,
        "beginner_summary": a.beginner_summary,
        "position_examples": a.position_examples,
        "market_state": a.market_state,
        "actionable_levels": a.actionable_levels,
        "timeframe_view": a.timeframe_view,
        "long_short_plan": a.long_short_plan,
        "short_term_signal": a.short_term_signal,
        "protections": a.protections,
    }


def write_outputs(output_dir: Path, run_id: str, analyses: list[SymbolAnalysis], profile: str, llama_mode: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "generated_at": utc_now().isoformat(),
        "mode": "shadow",
        "risk_profile": profile,
        "llama_mode": llama_mode,
        "results": [analysis_to_dict(a) for a in analyses],
    }
    json_path = output_dir / f"shadow_{run_id}.json"
    md_path = output_dir / f"shadow_{run_id}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    sections: list[str] = []
    sections.append(f"# Shadow Mode 報告\n")
    sections.append(f"- Run ID: `{run_id}`")
    sections.append(f"- 產生時間(UTC): `{payload['generated_at']}`")
    sections.append(f"- 風險偏好: `{profile}`")
    sections.append(f"- Llama 模式: `{llama_mode}`")
    sections.append("")
    for a in analyses:
        summary = textwrap.dedent(
            f"""
            ## {a.symbol}
            - 價格: `{a.price}`
            - 來源: `{a.market_source}`
            - 結論: `{a.decision}`
            - 風險: `{a.risk_level}` ({a.risk_score}/100)
            - 信心: `{a.confidence:.2f}`
            - 趨勢: `{a.trend_view}`
            - 報酬率: 5m `{a.returns['5m']}%` / 1h `{a.returns['1h']}%` / 4h `{a.returns['4h']}%` / 24h `{a.returns['24h']}%`
            - 波動: ATR `{a.volatility['atr_pct']}%`, realized24h `{a.volatility['realized_24h']}%`
            - 橫盤判定: `{a.market_state['state']}`，4h區間 `{a.market_state['range_4h']}` ({a.market_state['range_pct_4h']}%)
            - 多時間框架：5m `{a.timeframe_view['5m']['trend']}` / 4h `{a.timeframe_view['4h']['trend']}`
            - 短線方向訊號：`{a.short_term_signal['bias']}`，強度 `{a.short_term_signal['strength']}`，市場狀態 `{a.short_term_signal.get('market_regime', 'range_or_mixed')}`，4h核心 long `{a.short_term_signal.get('long_core_score', '-')}` / short `{a.short_term_signal.get('short_core_score', '-')}`，5m微結構 long `{a.short_term_signal.get('long_micro_score', '-')}` / short `{a.short_term_signal.get('short_micro_score', '-')}`
            - 假突破過濾：up `{a.short_term_signal['false_breakout']['false_breakout_up']}` / down `{a.short_term_signal['false_breakout']['false_breakout_down']}`
            - 進場區: `{a.trade_plan['entry_zone']}`
            - 停損: `{a.trade_plan['stop_loss']}`
            - 止盈: `{a.trade_plan['take_profit']}`
            - 單筆最大風險: `{a.trade_plan['max_risk_per_trade_pct']}%`
            - 模型來源: `{a.model_source}`
            """
        ).strip()
        sections.append(summary)
        sections.append("")
        sections.append("### 白話總結（新手版）")
        sections.append(f"- {a.beginner_summary['headline']}")
        sections.append(f"- 為什麼：{a.beginner_summary['core_reason']}")
        sections.append(f"- 現在怎麼做：{a.beginner_summary['now_action']}")
        sections.append(f"- {a.beginner_summary['reminder']}")
        sections.append(f"- {a.beginner_summary['range_hint']}")
        sections.append(f"- {a.beginner_summary['phase_hint']}")
        sections.append(f"- {a.beginner_summary['path_map_hint']}")
        sections.append(f"- {a.beginner_summary['break_path_hint']}")
        sections.append(f"- {a.beginner_summary['protection_hint']}")
        sections.append(f"- {a.beginner_summary['executor_hint']}")
        sections.append(f"- {a.beginner_summary['execution_hint']}")
        sections.append(f"- {a.beginner_summary['stop_hint']}")
        sections.append(f"- {a.beginner_summary['exit_hint']}")
        sections.append(f"- {a.beginner_summary['leverage_hint']}")
        sections.append(f"- {a.beginner_summary['short_bias_hint']}")
        sections.append(f"- {a.beginner_summary['long_short_hint']}")
        sections.append("- 先避免：")
        for x in a.beginner_summary["avoid"]:
            sections.append(f"- {x}")
        sections.append("")
        if a.position_examples.get("recommended_side") != "neutral":
            sections.append("### 槓桿換算（20x / 50% 倉位）")
            sections.append(f"- 建議方向：`{a.position_examples['recommended_side']}`")
            sections.append(f"- 參考觸發價：`{a.position_examples['reference_entry']}`")
            sections.append(f"- 建議止損：`{a.position_examples['recommended_stop_loss']}`")
            sections.append(
                f"- 打到止損：現貨 `{a.position_examples['to_stop_loss']['spot_move_pct']}%` / "
                f"20x 倉位 `{a.position_examples['to_stop_loss']['position_roi_pct']}%` / "
                f"帳戶 `{a.position_examples['to_stop_loss']['account_pnl_pct']}%`"
            )
            sections.append(f"- TP1：`{a.position_examples['recommended_take_profit_1']}`")
            sections.append(
                f"- 到 TP1：現貨 `{a.position_examples['to_take_profit_1']['spot_move_pct']}%` / "
                f"20x 倉位 `{a.position_examples['to_take_profit_1']['position_roi_pct']}%` / "
                f"帳戶 `{a.position_examples['to_take_profit_1']['account_pnl_pct']}%`"
            )
            sections.append(f"- TP2：`{a.position_examples['recommended_take_profit_2']}`")
            sections.append(
                f"- 到 TP2：現貨 `{a.position_examples['to_take_profit_2']['spot_move_pct']}%` / "
                f"20x 倉位 `{a.position_examples['to_take_profit_2']['position_roi_pct']}%` / "
                f"帳戶 `{a.position_examples['to_take_profit_2']['account_pnl_pct']}%`"
            )
            sections.append("")
        sections.append("### 做多/做空建議（4h/24h 主導 + 5m 進場節奏）")
        sections.append(f"- 綜合建議：{a.long_short_plan['recommendation']}")
        sections.append(f"- 權重：{a.long_short_plan['timeframe_weight']}")
        sections.append(f"- 訊號強度：`{a.long_short_plan['signal_strength']}`")
        sections.append(
            f"- 可交易性：`{a.long_short_plan.get('execution_readiness_label', '可執行')}`，"
            f"分析偏向 `{a.long_short_plan.get('analysis_bias', a.long_short_plan['direction_bias'])}`，"
            f"首選距離 `{a.long_short_plan.get('trigger_distance', {}).get('preferred', {}).get('distance_pct', '-')}`% / "
            f"`{a.long_short_plan.get('trigger_distance', {}).get('preferred', {}).get('distance_atr', '-')}` ATR"
        )
        sections.append(f"- 做多觸發：`{a.long_short_plan['long_setup']['trigger_price']}`")
        sections.append(
            f"- 做多確認：回踩區 `{a.long_short_plan['long_setup']['confirmation']['retest_zone']}`，"
            f"第二次突破 `{a.long_short_plan['long_setup']['confirmation']['second_breakout_trigger']}`，"
            f"失敗位 `{a.long_short_plan['long_setup']['confirmation']['retest_failure_level']}`"
        )
        sections.append(
            f"- 做多進場區：`{a.long_short_plan['long_setup']['entry_zone']}`，停損：`{a.long_short_plan['long_setup']['stop_loss']}`，止盈：`{a.long_short_plan['long_setup']['take_profit']}`"
        )
        sections.append(
            f"- 做多出場：保本上移 `{a.long_short_plan['long_setup']['management']['breakeven_trigger']}` -> `{a.long_short_plan['long_setup']['management']['breakeven_stop']}`，"
            f"分批區 `{a.long_short_plan['long_setup']['management']['scale_out_zone']}`，續抱區 `{a.long_short_plan['long_setup']['management']['runner_zone']}`"
        )
        sections.append(f"- 做空觸發：`{a.long_short_plan['short_setup']['trigger_price']}`")
        sections.append(
            f"- 做空確認：反抽區 `{a.long_short_plan['short_setup']['confirmation']['retest_zone']}`，"
            f"第二次跌破 `{a.long_short_plan['short_setup']['confirmation']['second_breakout_trigger']}`，"
            f"失敗位 `{a.long_short_plan['short_setup']['confirmation']['retest_failure_level']}`"
        )
        sections.append(
            f"- 做空進場區：`{a.long_short_plan['short_setup']['entry_zone']}`，停損：`{a.long_short_plan['short_setup']['stop_loss']}`，止盈：`{a.long_short_plan['short_setup']['take_profit']}`"
        )
        sections.append(
            f"- 做空出場：保本下移 `{a.long_short_plan['short_setup']['management']['breakeven_trigger']}` -> `{a.long_short_plan['short_setup']['management']['breakeven_stop']}`，"
            f"分批區 `{a.long_short_plan['short_setup']['management']['scale_out_zone']}`，續抱區 `{a.long_short_plan['short_setup']['management']['runner_zone']}`"
        )
        sections.append("")
        sections.append("### Onchain")
        if a.onchain_summary.get("available"):
            sections.append(
                f"- Provider: `{a.onchain_summary.get('provider', '-')}` | Bias: `{a.onchain_summary.get('bias', 'neutral')}` | "
                f"Confidence: `{a.onchain_summary.get('confidence', 'low')}`"
            )
            for item in a.onchain_summary.get("summary", []):
                sections.append(f"- {item}")
        else:
            sections.append(f"- {a.onchain_summary.get('summary', ['未啟用鏈上分析。'])[0]}")
        sections.append("")
        sections.append("### Protections")
        if a.protections.get("summaries"):
            for item in a.protections["summaries"]:
                sections.append(f"- {item}")
        else:
            sections.append("- 保護層目前未啟動。")
        sections.append("")
        sections.append("### Thesis")
        for t in a.thesis:
            sections.append(f"- {t}")
        sections.append("")
        sections.append("### Warnings")
        if a.warnings:
            for w in a.warnings:
                sections.append(f"- {w}")
        else:
            sections.append("- 無")
        sections.append("")
    md_path.write_text("\n".join(sections), encoding="utf-8")
    return json_path, md_path


def normalize_symbol(symbol: str, quote: str) -> str:
    s = symbol.upper().strip()
    if s.endswith(quote.upper()):
        return s
    return f"{s}{quote.upper()}"


def main() -> None:
    args = parse_args()
    run_id = time.strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir)
    db_path = Path(args.db_path)
    symbols = [normalize_symbol(s, args.quote) for s in args.symbols]
    rss_items = fetch_rss_items(args.timeout)
    analyses: list[SymbolAnalysis] = []
    conn = ensure_db(db_path)
    try:
        for s in symbols:
            analysis = build_symbol_analysis(
                symbol=s,
                profile=args.risk_profile,
                timeout=args.timeout,
                llama_mode=args.llama,
                llama_model=args.llama_model,
                rss_items=rss_items,
            )
            analyses.append(analysis)
            log_signal(conn, run_id, analysis)
    finally:
        conn.close()
    json_path, md_path = write_outputs(output_dir, run_id, analyses, args.risk_profile, args.llama)
    print(f"Run ID: {run_id}")
    print(f"Saved JSON: {json_path}")
    print(f"Saved Markdown: {md_path}")
    for a in analyses:
        print(
            f"[{a.symbol}] decision={a.decision}, risk={a.risk_level}({a.risk_score}), "
            f"confidence={a.confidence:.2f}, source={a.model_source}"
        )


if __name__ == "__main__":
    main()
