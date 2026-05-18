#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any

import duckdb


QUERY_TEMPLATES: dict[str, dict[str, Any]] = {
    "short_effective_4h": {
        "description": "看 effective_short_breakdown 在 4h 的歷史樣本。",
        "event_type": "effective_short_breakdown",
        "horizon": "4h",
        "output_md": "歷史市場樣本查詢_short_effective_4h.md",
    },
    "short_second_4h": {
        "description": "看 second_breakdown_short 在 4h 的歷史樣本。",
        "event_type": "second_breakdown_short",
        "horizon": "4h",
        "output_md": "歷史市場樣本查詢_short_second_4h.md",
    },
    "long_effective_4h": {
        "description": "看 effective_long_breakout 在 4h 的歷史樣本。",
        "event_type": "effective_long_breakout",
        "horizon": "4h",
        "output_md": "歷史市場樣本查詢_long_effective_4h.md",
    },
    "long_retest_4h": {
        "description": "看 retest_hold_long 在 4h 的歷史樣本。",
        "event_type": "retest_hold_long",
        "horizon": "4h",
        "output_md": "歷史市場樣本查詢_long_retest_4h.md",
    },
    "watch_up_1h": {
        "description": "看 approach_up 在 1h 的戰備提醒樣本。",
        "event_type": "approach_up",
        "horizon": "1h",
        "output_md": "歷史市場樣本查詢_watch_up_1h.md",
    },
    "watch_down_1h": {
        "description": "看 approach_down 在 1h 的戰備提醒樣本。",
        "event_type": "approach_down",
        "horizon": "1h",
        "output_md": "歷史市場樣本查詢_watch_down_1h.md",
    },
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Query historical market samples from the analytics store.")
    p.add_argument("--analytics-db", default="analytics/market_samples.duckdb")
    p.add_argument("--analytics-dir", default="analytics")
    p.add_argument("--template", default="", help="Use a built-in template query name.")
    p.add_argument("--list-templates", action="store_true")
    p.add_argument("--symbol", default="")
    p.add_argument("--event-type", default="")
    p.add_argument("--market-regime", default="")
    p.add_argument("--protection-status", default="")
    p.add_argument("--horizon", default="")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--output-md", default="歷史市場樣本查詢.md")
    p.add_argument("--output-json", default="")
    return p.parse_args()


def _pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}%"


def main() -> None:
    args = parse_args()
    if args.list_templates:
        for name, meta in QUERY_TEMPLATES.items():
            print(f"{name}: {meta['description']}")
        return

    if args.template:
        template = QUERY_TEMPLATES.get(args.template)
        if not template:
            raise SystemExit(f"Unknown template: {args.template}")
        for key in ("symbol", "event_type", "market_regime", "protection_status", "horizon", "output_md"):
            current = getattr(args, key.replace("-", "_"), "")
            if not current and template.get(key):
                setattr(args, key.replace("-", "_"), template[key])

    db_path = Path(args.analytics_db)
    analytics_dir = Path(args.analytics_dir)
    market_context_path = analytics_dir / "market_context_snapshots.parquet"
    event_outcomes_path = analytics_dir / "event_outcomes.parquet"

    conn = duckdb.connect()
    filters: list[str] = []
    params: list[Any] = []
    if args.symbol:
        filters.append("eo.symbol = ?")
        params.append(args.symbol.upper())
    if args.event_type:
        filters.append("eo.event_type = ?")
        params.append(args.event_type)
    if args.market_regime:
        filters.append("eo.market_regime = ?")
        params.append(args.market_regime)
    if args.protection_status:
        filters.append("eo.protection_status = ?")
        params.append(args.protection_status)
    if args.horizon:
        filters.append("eo.horizon_label = ?")
        params.append(args.horizon)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    if db_path.exists():
        conn.close()
        conn = duckdb.connect(str(db_path), read_only=True)
        base_sql = f"""
            SELECT
                eo.sent_at,
                eo.symbol,
                eo.event_type,
                eo.horizon_label,
                eo.market_regime,
                eo.protection_status,
                eo.close_return_pct,
                eo.max_runup_pct,
                eo.max_drawdown_pct,
                eo.tp1_hit,
                eo.stop_loss_hit,
                m.tf5_volume_ratio,
                m.tf5_trend,
                m.tf4h_trend,
                m.active_trigger_distance_pct,
                m.active_rr_to_tp1,
                m.active_executor_quality
            FROM event_outcomes eo
            LEFT JOIN market_context_snapshots m USING (event_id, event_key)
            {where_sql}
        """
    elif market_context_path.exists() and event_outcomes_path.exists():
        base_sql = f"""
            SELECT
                eo.sent_at,
                eo.symbol,
                eo.event_type,
                eo.horizon_label,
                eo.market_regime,
                eo.protection_status,
                eo.close_return_pct,
                eo.max_runup_pct,
                eo.max_drawdown_pct,
                eo.tp1_hit,
                eo.stop_loss_hit,
                m.tf5_volume_ratio,
                m.tf5_trend,
                m.tf4h_trend,
                m.active_trigger_distance_pct,
                m.active_rr_to_tp1,
                m.active_executor_quality
            FROM read_parquet('{event_outcomes_path.as_posix()}') eo
            LEFT JOIN read_parquet('{market_context_path.as_posix()}') m USING (event_id, event_key)
            {where_sql}
        """
    else:
        raise FileNotFoundError(f"Missing analytics inputs: {db_path} and {analytics_dir}")

    summary = conn.execute(
        f"""
        SELECT
            COUNT(*) AS sample_count,
            AVG(close_return_pct) AS avg_close_return_pct,
            AVG(CASE WHEN close_return_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0 AS positive_close_rate,
            AVG(tp1_hit) * 100.0 AS tp1_rate,
            AVG(stop_loss_hit) * 100.0 AS stop_loss_rate
        FROM ({base_sql})
        """,
        params,
    ).fetchone()

    rows = conn.execute(
        f"""
        {base_sql}
        ORDER BY eo.sent_at DESC
        LIMIT ?
        """,
        [*params, args.limit],
    ).fetchall()
    conn.close()

    lines = [
        "# 歷史市場樣本查詢",
        "",
        f"- DuckDB: `{db_path}`",
        f"- symbol: `{args.symbol or 'ALL'}`",
        f"- event_type: `{args.event_type or 'ALL'}`",
        f"- market_regime: `{args.market_regime or 'ALL'}`",
        f"- protection_status: `{args.protection_status or 'ALL'}`",
        f"- horizon: `{args.horizon or 'ALL'}`",
        f"- sample_count: `{summary[0] or 0}`",
        f"- avg_close_return_pct: `{(summary[1] or 0.0):.3f}%`",
        f"- positive_close_rate: `{_pct(summary[2])}`",
        f"- tp1_rate: `{_pct(summary[3])}`",
        f"- stop_loss_rate: `{_pct(summary[4])}`",
        "",
        "## 最近樣本",
        "",
    ]
    if not rows:
        lines.append("- 無資料")
    else:
        for row in rows:
            lines.append(
                f"- `{row[0]}` | `{row[1]}` | `{row[2]}` | `{row[3]}` | regime=`{row[4]}` | "
                f"protection=`{row[5]}` | close=`{row[6]:.3f}%` | runup=`{row[7]:.3f}%` | "
                f"drawdown=`{row[8]:.3f}%` | vol5m=`{(row[11] or 0.0):.3f}` | "
                f"distance=`{(row[14] or 0.0):.3f}%` | rr=`{(row[15] or 0.0):.2f}` | quality=`{row[16] or '-'}`"
            )

    output_md = Path(args.output_md)
    output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.output_json:
        payload = {
            "filters": {
                "symbol": args.symbol or None,
                "event_type": args.event_type or None,
                "market_regime": args.market_regime or None,
                "protection_status": args.protection_status or None,
                "horizon": args.horizon or None,
                "limit": args.limit,
            },
            "summary": {
                "sample_count": summary[0] or 0,
                "avg_close_return_pct": summary[1] or 0.0,
                "positive_close_rate": summary[2] or 0.0,
                "tp1_rate": summary[3] or 0.0,
                "stop_loss_rate": summary[4] or 0.0,
            },
            "rows": [list(row) for row in rows],
        }
        Path(args.output_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"historical sample query written to {output_md}")


if __name__ == "__main__":
    main()
