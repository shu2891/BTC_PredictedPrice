#!/usr/bin/env python3
import argparse
import datetime as dt
import time
from pathlib import Path
from typing import Any

from market_alert_daemon import DEFAULT_TELEGRAM_SCRIPT, load_config, send_telegram
from shadow_mode import analysis_to_dict, build_symbol_analysis, fetch_rss_items, normalize_symbol


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Send 4-hour Chinese swing summary reports to Telegram.")
    p.add_argument("--config", default="watchlist.json")
    p.add_argument("--loop-minutes", type=int, default=240)
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--llama", choices=["off", "auto", "on"], default="off")
    p.add_argument("--llama-model", default="llama3.2:3b")
    p.add_argument("--quote", default="USDT")
    p.add_argument("--telegram-script", default=str(DEFAULT_TELEGRAM_SCRIPT))
    p.add_argument("--once", action="store_true")
    return p.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fmt_price(value: Any) -> str:
    if value in (None, "", "-"):
        return "-"
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def bias_zh(value: str) -> str:
    return {
        "long": "偏多",
        "short": "偏空",
        "neutral": "中性",
    }.get(value, value)


def strength_zh(value: str) -> str:
    return {
        "high": "強",
        "medium": "中",
        "low": "弱",
    }.get(value, value)


def summary_line(result: dict[str, Any]) -> str:
    protections = result.get("protections", {}) or {}
    if protections.get("active"):
        return "建議：先觀察，保護層已啟動。"

    bias = str(result.get("short_term_signal", {}).get("bias", "neutral"))
    if bias == "long":
        return "建議：主看上方劇本，等到價提醒再決定是否下單。"
    if bias == "short":
        return "建議：主看下方劇本，等到價提醒再決定是否下單。"
    return "建議：雙向觀察，先不要提前進場。"


def format_symbol_section_zh(result: dict[str, Any]) -> list[str]:
    levels = result["actionable_levels"]
    short_signal = result["short_term_signal"]
    long_setup = (result.get("long_short_plan", {}) or {}).get("long_setup", {}) or {}
    short_setup = (result.get("long_short_plan", {}) or {}).get("short_setup", {}) or {}
    long_tp = (long_setup.get("take_profit") or ["-"])[0]
    short_tp = (short_setup.get("take_profit") or ["-"])[0]
    long_stop = long_setup.get("stop_loss", "-")
    short_stop = short_setup.get("stop_loss", "-")
    protection_tag = " | 保護中" if result.get("protections", {}).get("active") else ""
    return [
        f"{result['symbol']} | 現價 {fmt_price(result['price'])} | {bias_zh(short_signal['bias'])} {strength_zh(short_signal['strength'])}{protection_tag}",
        f"區間 {fmt_price(levels['range_low'])}-{fmt_price(levels['range_high'])}",
        f"上方 {fmt_price(levels['breakout_up'])} | 失效 {fmt_price(long_stop)} | 目標1 {fmt_price(long_tp)}",
        f"下方 {fmt_price(levels['breakout_down'])} | 失效 {fmt_price(short_stop)} | 目標1 {fmt_price(short_tp)}",
        summary_line(result),
    ]


def build_report_message(results: list[dict[str, Any]]) -> str:
    timestamp = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "[check_price]",
        "四小時波段摘要",
        f"時間 {timestamp}",
        "先看上方/下方劇本，再等到價提醒決定是否出手。",
        "",
    ]
    for idx, result in enumerate(results):
        lines.extend(format_symbol_section_zh(result))
        if idx != len(results) - 1:
            lines.append("")
    return "\n".join(lines)


def run_cycle(
    config_path: Path,
    timeout: int,
    llama_mode: str,
    llama_model: str,
    quote: str,
    telegram_script: Path,
) -> None:
    config = load_config(config_path)
    profile = str(config.get("risk_profile", "conservative"))
    protection_settings = config.get("protections", {})
    funding_settings = config.get("funding", {})
    symbols = [normalize_symbol(s, quote) for s in config.get("symbols", [])]
    if not symbols:
        raise RuntimeError("watchlist has no symbols")

    rss_items = fetch_rss_items(timeout)
    results: list[dict[str, Any]] = []
    for symbol in symbols:
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
        results.append(analysis_to_dict(analysis))

    send_telegram(telegram_script, build_report_message(results))


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    telegram_script = Path(args.telegram_script)
    while True:
        try:
            run_cycle(
                config_path=config_path,
                timeout=args.timeout,
                llama_mode=args.llama,
                llama_model=args.llama_model,
                quote=args.quote,
                telegram_script=telegram_script,
            )
            if args.once:
                break
        except Exception as exc:  # noqa: BLE001
            print(f"[{utc_now().isoformat()}] reporter_cycle_error error={exc}")
            if args.once:
                raise
        time.sleep(args.loop_minutes * 60)


if __name__ == "__main__":
    main()
