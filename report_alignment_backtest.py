#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests


USER_AGENT = "report-alignment-backtest/1.0"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
INTERVAL_MS = 5 * 60 * 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest existing shadow reports against actual future price paths.")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--pattern", default="shadow_*.json")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--output-json", default="reports/report_alignment_backtest.json")
    parser.add_argument("--output-md", default="現有報告走勢相符回測.md")
    return parser.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(ts: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def fetch_5m_klines(symbol: str, start_dt: dt.datetime, end_dt: dt.datetime, timeout: int) -> list[dict[str, float | int]]:
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    cursor = start_ms
    rows: list[dict[str, float | int]] = []
    while cursor < end_ms:
        resp = requests.get(
            BINANCE_KLINES_URL,
            params={
                "symbol": symbol,
                "interval": "5m",
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
            rows.append(
                {
                    "open_time": int(row[0]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                }
            )
        cursor = int(data[-1][0]) + INTERVAL_MS
        time.sleep(0.03)
    return rows


def infer_expected_direction(result: dict[str, Any]) -> str:
    long_short_plan = result.get("long_short_plan", {})
    direction_bias = long_short_plan.get("direction_bias")
    if direction_bias == "long_bias":
        return "up"
    if direction_bias == "short_bias":
        return "down"
    trend_view = result.get("trend_view")
    if trend_view == "bullish":
        return "up"
    if trend_view == "bearish":
        return "down"
    return "neutral"


def touch_state(row: dict[str, float | int], target_up: float | None, target_down: float | None) -> str:
    high = float(row["high"])
    low = float(row["low"])
    hit_up = target_up is not None and high >= target_up
    hit_down = target_down is not None and low <= target_down
    if hit_up and hit_down:
        return "both"
    if hit_up:
        return "up"
    if hit_down:
        return "down"
    return "none"


def first_touch(rows: list[dict[str, float | int]], target_up: float | None, target_down: float | None) -> tuple[str, str | None]:
    for row in rows:
        state = touch_state(row, target_up, target_down)
        if state != "none":
            ts = dt.datetime.fromtimestamp(int(row["open_time"]) / 1000, tz=dt.timezone.utc).isoformat()
            return state, ts
    return "none", None


def first_tp_sl(rows: list[dict[str, float | int]], direction: str, tp1: float | None, stop_loss: float | None) -> tuple[str, str | None]:
    for row in rows:
        high = float(row["high"])
        low = float(row["low"])
        ts = dt.datetime.fromtimestamp(int(row["open_time"]) / 1000, tz=dt.timezone.utc).isoformat()
        if direction == "up":
            hit_tp = tp1 is not None and high >= tp1
            hit_sl = stop_loss is not None and low <= stop_loss
        elif direction == "down":
            hit_tp = tp1 is not None and low <= tp1
            hit_sl = stop_loss is not None and high >= stop_loss
        else:
            return "n/a", None
        if hit_tp and hit_sl:
            return "both", ts
        if hit_tp:
            return "tp", ts
        if hit_sl:
            return "sl", ts
    return "none", None


def parse_timing_window(text: str | None) -> tuple[int, int] | None:
    mapping = {
        "約 1-8 小時": (1, 8),
        "約 6-18 小時": (6, 18),
        "約 8-24 小時": (8, 24),
        "約 12-36 小時": (12, 36),
    }
    if not text:
        return None
    return mapping.get(text.strip())


def within_timing(hours: float, window: tuple[int, int] | None) -> bool | None:
    if window is None:
        return None
    lo, hi = window
    return lo <= hours <= hi


def evaluate_result(source_file: str, result: dict[str, Any], timeout: int) -> dict[str, Any]:
    symbol = str(result["symbol"])
    report_ts = parse_iso(str(result["timestamp"]))
    end_ts = report_ts + dt.timedelta(hours=24)
    rows = fetch_5m_klines(symbol, report_ts, end_ts, timeout)
    if not rows:
        raise RuntimeError(f"No 5m data for {symbol} from {report_ts.isoformat()}")

    entry = float(result["price"])
    close_24h = float(rows[-1]["close"])
    ret_24h = (close_24h / entry - 1.0) * 100.0
    max_high = max(float(r["high"]) for r in rows)
    min_low = min(float(r["low"]) for r in rows)
    max_up = (max_high / entry - 1.0) * 100.0
    max_down = (min_low / entry - 1.0) * 100.0

    expected_direction = infer_expected_direction(result)
    direction_match = (
        ret_24h > 0 if expected_direction == "up"
        else ret_24h < 0 if expected_direction == "down"
        else abs(ret_24h) <= 2.0
    )

    trade_plan = result.get("trade_plan", {})
    take_profit = trade_plan.get("take_profit", [])
    tp1 = float(take_profit[0]) if take_profit else None
    stop_loss = float(trade_plan["stop_loss"]) if "stop_loss" in trade_plan else None
    tp_sl_state, tp_sl_ts = first_tp_sl(rows, expected_direction, tp1, stop_loss)
    tp_sl_match = tp_sl_state == "tp"

    actionable_levels = result.get("actionable_levels", {})
    long_short_plan = result.get("long_short_plan", {})
    long_setup = long_short_plan.get("long_setup", {})
    short_setup = long_short_plan.get("short_setup", {})
    breakout_up = (
        float(long_setup.get("trigger_price"))
        if long_setup.get("trigger_price") is not None
        else float(actionable_levels["breakout_up"])
        if "breakout_up" in actionable_levels
        else None
    )
    breakout_down = (
        float(short_setup.get("trigger_price"))
        if short_setup.get("trigger_price") is not None
        else float(actionable_levels["breakout_down"])
        if "breakout_down" in actionable_levels
        else None
    )
    breakout_state = "n/a"
    breakout_ts = None
    breakout_match = None
    if breakout_up is not None or breakout_down is not None:
        breakout_state, breakout_ts = first_touch(rows, breakout_up, breakout_down)
        breakout_match = (
            breakout_state == "up" if expected_direction == "up"
            else breakout_state == "down" if expected_direction == "down"
            else breakout_state == "none"
        )

    timing_hours = None
    timing_match = None
    if breakout_ts is not None:
        breakout_dt = parse_iso(breakout_ts)
        timing_hours = round((breakout_dt - report_ts).total_seconds() / 3600, 3)
        timing_match = within_timing(timing_hours, parse_timing_window(actionable_levels.get("timing_window")))

    return {
        "source_file": source_file,
        "symbol": symbol,
        "timestamp": report_ts.isoformat(),
        "decision": result.get("decision"),
        "trend_view": result.get("trend_view"),
        "expected_direction": expected_direction,
        "entry_price": entry,
        "close_24h": round(close_24h, 6),
        "return_24h_pct": round(ret_24h, 3),
        "max_up_pct": round(max_up, 3),
        "max_down_pct": round(max_down, 3),
        "direction_match": bool(direction_match),
        "tp_sl_state": tp_sl_state,
        "tp_sl_timestamp": tp_sl_ts,
        "tp_sl_match": bool(tp_sl_match),
        "breakout_state": breakout_state,
        "breakout_timestamp": breakout_ts,
        "breakout_match": breakout_match,
        "timing_window": actionable_levels.get("timing_window"),
        "timing_hours": timing_hours,
        "timing_match": timing_match,
        "has_actionable_levels": bool(actionable_levels),
        "has_long_short_plan": bool(long_short_plan),
    }


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by_direction = Counter(s["expected_direction"] for s in samples)
    by_symbol = Counter(s["symbol"] for s in samples)
    overall = {
        "samples": len(samples),
        "direction_match_rate": average([1.0 if s["direction_match"] else 0.0 for s in samples]),
        "tp_sl_match_rate": average([1.0 if s["tp_sl_match"] else 0.0 for s in samples if s["tp_sl_state"] != "n/a"]),
        "breakout_match_rate": average([1.0 if s["breakout_match"] else 0.0 for s in samples if s["breakout_match"] is not None]),
        "timing_match_rate": average([1.0 if s["timing_match"] else 0.0 for s in samples if s["timing_match"] is not None]),
        "avg_return_24h_pct": average([float(s["return_24h_pct"]) for s in samples]),
    }
    by_exp_dir: dict[str, Any] = {}
    for direction in sorted(by_direction):
        items = [s for s in samples if s["expected_direction"] == direction]
        by_exp_dir[direction] = {
            "count": len(items),
            "direction_match_rate": average([1.0 if s["direction_match"] else 0.0 for s in items]),
            "tp_sl_match_rate": average([1.0 if s["tp_sl_match"] else 0.0 for s in items if s["tp_sl_state"] != "n/a"]),
            "breakout_match_rate": average([1.0 if s["breakout_match"] else 0.0 for s in items if s["breakout_match"] is not None]),
            "timing_match_rate": average([1.0 if s["timing_match"] else 0.0 for s in items if s["timing_match"] is not None]),
            "avg_return_24h_pct": average([float(s["return_24h_pct"]) for s in items]),
        }
    return {
        "generated_at": utc_now().isoformat(),
        "overall": overall,
        "expected_direction_distribution": dict(by_direction),
        "symbol_distribution": dict(by_symbol),
        "by_expected_direction": by_exp_dir,
    }


def write_md(path: Path, summary: dict[str, Any], samples: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    lines.append("# 現有報告走勢相符回測")
    lines.append("")
    lines.append(f"- 產生時間(UTC): `{summary['generated_at']}`")
    lines.append(f"- 樣本數: `{summary['overall']['samples']}`")
    lines.append(f"- 方向相符率: `{pct(summary['overall']['direction_match_rate'])}`")
    lines.append(f"- TP/SL 路徑相符率: `{pct(summary['overall']['tp_sl_match_rate'])}`")
    lines.append(f"- 突破方向相符率: `{pct(summary['overall']['breakout_match_rate'])}`")
    lines.append(f"- 時間窗相符率: `{pct(summary['overall']['timing_match_rate'])}`")
    lines.append(f"- 24h 平均報酬: `{summary['overall']['avg_return_24h_pct']:.3f}%`")
    lines.append("")
    lines.append("## 預期方向分布")
    for k, v in sorted(summary["expected_direction_distribution"].items()):
        lines.append(f"- `{k}`: `{v}`")
    lines.append("")
    lines.append("## 各方向相符度")
    lines.append("| 預期方向 | 筆數 | 方向相符率 | TP/SL 路徑相符率 | 突破方向相符率 | 時間窗相符率 | 24h 平均報酬 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for direction, st in sorted(summary["by_expected_direction"].items()):
        avg_ret = st["avg_return_24h_pct"]
        lines.append(
            f"| {direction} | {st['count']} | {pct(st['direction_match_rate'])} | {pct(st['tp_sl_match_rate'])} | "
            f"{pct(st['breakout_match_rate'])} | {pct(st['timing_match_rate'])} | {avg_ret:.3f}% |"
        )
    lines.append("")
    lines.append("## 個別樣本")
    lines.append("| 時間 | 幣種 | 預期方向 | 24h 報酬 | 方向相符 | TP/SL | 突破方向 | 時間窗 |")
    lines.append("|---|---|---|---:|---:|---|---|---|")
    for s in sorted(samples, key=lambda x: x["timestamp"]):
        lines.append(
            f"| {s['timestamp']} | {s['symbol']} | {s['expected_direction']} | {s['return_24h_pct']:.3f}% | "
            f"{'Y' if s['direction_match'] else 'N'} | {s['tp_sl_state']} | {s['breakout_state']} | "
            f"{'Y' if s['timing_match'] is True else 'N' if s['timing_match'] is False else 'n/a'} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    reports_dir = Path(args.reports_dir)
    samples: list[dict[str, Any]] = []
    for path in sorted(reports_dir.glob(args.pattern)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for result in payload.get("results", []):
            samples.append(evaluate_result(path.name, result, args.timeout))
    summary = build_summary(samples)
    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps({"summary": summary, "samples": samples}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_md(output_md, summary, samples)
    print(json.dumps({"samples": len(samples), "output_json": str(output_json), "output_md": str(output_md)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
