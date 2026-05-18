#!/usr/bin/env python3
import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from shadow_mode import analysis_to_dict, build_symbol_analysis, fetch_rss_items, normalize_symbol


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a market outlook report for Telegram/manual use.")
    parser.add_argument("--config", default="watchlist.json")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--llama", choices=["off", "auto", "on"], default="off")
    parser.add_argument("--llama-model", default="llama3.2:3b")
    parser.add_argument("--quote", default="USDT")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--save", action="store_true")
    return parser.parse_args()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fmt_price(value: Any) -> str:
    if value in (None, "", "-"):
        return "-"
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def market_bias_zh(value: str) -> str:
    return {
        "long": "偏多",
        "short": "偏空",
        "neutral": "中性",
        "long_bias": "偏多",
        "short_bias": "偏空",
    }.get(value, value or "中性")


def risk_zh(value: str) -> str:
    return {
        "high": "高",
        "medium": "中",
        "low": "低",
    }.get(value, value or "-")


def build_market_outlook(
    config_path: Path,
    timeout: int,
    llama_mode: str,
    llama_model: str,
    quote: str,
    explicit_symbols: list[str] | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    profile = str(config.get("risk_profile", "conservative"))
    protection_settings = config.get("protections", {})
    funding_settings = config.get("funding", {})
    symbols = explicit_symbols or [normalize_symbol(s, quote) for s in config.get("symbols", [])]
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

    return {
        "generated_at": utc_now().isoformat(),
        "risk_profile": profile,
        "symbols": symbols,
        "results": results,
    }


def build_market_outlook_message(payload: dict[str, Any]) -> str:
    lines = [
        "[check_price]",
        "市場現況報告",
        f"時間 {dt.datetime.fromisoformat(payload['generated_at']).strftime('%Y-%m-%d %H:%M UTC')}",
        "先看當前結構，再看上破/下破後的下一站。",
        "",
    ]

    for item in payload["results"]:
        signal = item.get("short_term_signal", {}) or {}
        plan = item.get("long_short_plan", {}) or {}
        levels = item.get("actionable_levels", {}) or {}
        price_map = levels.get("price_map", {}) or {}
        lines.extend(
            [
                f"{item['symbol']} | 現價 {fmt_price(item['price'])} | {market_bias_zh(plan.get('direction_bias'))} {risk_zh(item.get('risk_level'))}",
                f"結構 {signal.get('market_regime', '-')} | 階段 {price_map.get('phase', '-')}",
                f"上破 {fmt_price(levels.get('breakout_up'))} | 下破 {fmt_price(levels.get('breakout_down'))}",
                f"上去 {price_map.get('if_break_up', '-')}",
                f"下去 {price_map.get('if_break_down', '-')}",
                f"看法 {plan.get('recommendation', '-')}",
                "",
            ]
        )

    return "\n".join(lines).rstrip()


def build_market_outlook_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 市場現況報告",
        "",
        f"- 生成時間(UTC): `{payload['generated_at']}`",
        f"- 風險偏好: `{payload['risk_profile']}`",
        "",
    ]
    for item in payload["results"]:
        signal = item.get("short_term_signal", {}) or {}
        plan = item.get("long_short_plan", {}) or {}
        levels = item.get("actionable_levels", {}) or {}
        price_map = levels.get("price_map", {}) or {}
        lines.extend(
            [
                f"## {item['symbol']}",
                f"- 現價: `{fmt_price(item['price'])}`",
                f"- 方向偏向: `{market_bias_zh(plan.get('direction_bias'))}`",
                f"- 風險: `{item.get('risk_level', '-')}` ({item.get('risk_score', '-')}/100)",
                f"- 市場狀態: `{signal.get('market_regime', '-')}` / `{price_map.get('phase', '-')}`",
                f"- 上破價: `{fmt_price(levels.get('breakout_up'))}`",
                f"- 下破價: `{fmt_price(levels.get('breakout_down'))}`",
                f"- 上破劇本: {price_map.get('if_break_up', '-')}",
                f"- 下破劇本: {price_map.get('if_break_down', '-')}",
                f"- 建議: {plan.get('recommendation', '-')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def save_market_outlook(payload: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.fromisoformat(payload["generated_at"]).strftime("%Y%m%d_%H%M%S")
    json_path = reports_dir / f"market_outlook_{ts}.json"
    md_path = reports_dir / f"market_outlook_{ts}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(build_market_outlook_markdown(payload), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    args = parse_args()
    payload = build_market_outlook(
        config_path=Path(args.config),
        timeout=args.timeout,
        llama_mode=args.llama,
        llama_model=args.llama_model,
        quote=args.quote,
        explicit_symbols=args.symbols,
    )
    print(build_market_outlook_message(payload))
    if args.save:
        json_path, md_path = save_market_outlook(payload, Path(args.reports_dir))
        print(f"\nSaved JSON: {json_path}")
        print(f"Saved Markdown: {md_path}")


if __name__ == "__main__":
    main()
