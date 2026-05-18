#!/usr/bin/env python3
import json
import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from event_types import event_role, is_watch_only_event


def _json_loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def _first(values: list[Any], index: int, default: Any = None) -> Any:
    if len(values) > index:
        return values[index]
    return default


def _coerce_bool(value: Any) -> bool:
    return bool(value)


def build_market_context_snapshot_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id,
            event_key,
            symbol,
            event_type,
            direction,
            level,
            sent_at,
            last_price,
            stop_loss,
            take_profit_1,
            take_profit_2,
            timeframe_view_json,
            actionable_levels_json,
            short_term_signal_json,
            protections_json,
            long_short_plan_json
        FROM alert_events
        ORDER BY sent_at ASC, id ASC
        """
    ).fetchall()

    snapshots: list[dict[str, Any]] = []
    for row in rows:
        timeframe_view = _json_loads(row["timeframe_view_json"])
        actionable_levels = _json_loads(row["actionable_levels_json"])
        short_signal = _json_loads(row["short_term_signal_json"])
        protections = _json_loads(row["protections_json"])
        long_short_plan = _json_loads(row["long_short_plan_json"])

        tf5 = timeframe_view.get("5m", {})
        tf1h = timeframe_view.get("1h", {})
        tf4h = timeframe_view.get("4h", {})
        market_state = timeframe_view.get("market_state", {})
        if not market_state:
            market_state = {}

        long_setup = long_short_plan.get("long_setup", {})
        short_setup = long_short_plan.get("short_setup", {})
        long_executor = long_setup.get("executor_plan", {})
        short_executor = short_setup.get("executor_plan", {})

        snapshots.append(
            {
                "event_id": row["id"],
                "event_key": row["event_key"],
                "symbol": row["symbol"],
                "event_type": row["event_type"],
                "event_role": event_role(row["event_type"]),
                "is_watch_only": is_watch_only_event(row["event_type"]),
                "direction": row["direction"],
                "sent_at": row["sent_at"],
                "level": row["level"],
                "last_price": row["last_price"],
                "market_state_label": market_state.get("state"),
                "is_sideways": _coerce_bool(market_state.get("is_sideways", False)),
                "stop_loss": row["stop_loss"],
                "take_profit_1": row["take_profit_1"],
                "take_profit_2": row["take_profit_2"],
                "market_regime": short_signal.get("market_regime", "unknown"),
                "signal_bias": short_signal.get("bias", "neutral"),
                "signal_strength": short_signal.get("strength", "unknown"),
                "long_core_score": short_signal.get("long_core_score"),
                "short_core_score": short_signal.get("short_core_score"),
                "long_micro_score": short_signal.get("long_micro_score"),
                "short_micro_score": short_signal.get("short_micro_score"),
                "long_gate_open": _coerce_bool(short_signal.get("gate_open", {}).get("long", False)),
                "short_gate_open": _coerce_bool(short_signal.get("gate_open", {}).get("short", False)),
                "protection_status": protections.get("status", "unknown"),
                "protection_active": _coerce_bool(protections.get("active", False)),
                "pause_long": _coerce_bool(protections.get("pause_long", False)),
                "pause_short": _coerce_bool(protections.get("pause_short", False)),
                "hard_block": _coerce_bool(protections.get("hard_block", False)),
                "analysis_bias": long_short_plan.get("analysis_bias", "neutral"),
                "direction_bias": long_short_plan.get("direction_bias", "neutral"),
                "preferred_setup": long_short_plan.get("preferred_setup", "neutral"),
                "execution_readiness": long_short_plan.get("execution_readiness", "unknown"),
                "tf5_trend": tf5.get("trend"),
                "tf5_volume_ratio": tf5.get("volume_ratio"),
                "tf5_rsi14": tf5.get("rsi14"),
                "tf5_above_vwap": _coerce_bool(tf5.get("above_vwap", False)),
                "tf1h_trend": tf1h.get("trend"),
                "tf1h_volume_ratio": tf1h.get("volume_ratio"),
                "tf1h_rsi14": tf1h.get("rsi14"),
                "tf4h_trend": tf4h.get("trend"),
                "tf4h_volume_ratio": tf4h.get("volume_ratio"),
                "tf4h_rsi14": tf4h.get("rsi14"),
                "breakout_up": actionable_levels.get("breakout_up"),
                "breakout_down": actionable_levels.get("breakout_down"),
                "range_low": actionable_levels.get("range_low"),
                "range_high": actionable_levels.get("range_high"),
                "long_ready_zone_low": _first(actionable_levels.get("long_ready_zone", []), 0),
                "long_ready_zone_high": _first(actionable_levels.get("long_ready_zone", []), 1),
                "short_ready_zone_low": _first(actionable_levels.get("short_ready_zone", []), 0),
                "short_ready_zone_high": _first(actionable_levels.get("short_ready_zone", []), 1),
                "price_map_state": actionable_levels.get("price_map", {}).get("market_phase"),
                "support_1": actionable_levels.get("price_map", {}).get("support_1"),
                "support_2": actionable_levels.get("price_map", {}).get("support_2"),
                "resistance_1": actionable_levels.get("price_map", {}).get("resistance_1"),
                "resistance_2": actionable_levels.get("price_map", {}).get("resistance_2"),
                "long_trigger_price": long_setup.get("trigger_price"),
                "short_trigger_price": short_setup.get("trigger_price"),
                "long_rr_to_tp1": long_executor.get("rr_to_tp1"),
                "short_rr_to_tp1": short_executor.get("rr_to_tp1"),
                "long_executor_quality": long_executor.get("quality"),
                "short_executor_quality": short_executor.get("quality"),
                "active_trigger_price": (
                    long_setup.get("trigger_price")
                    if row["direction"] == "up"
                    else short_setup.get("trigger_price")
                    if row["direction"] == "down"
                    else None
                ),
                "active_trigger_distance_pct": (
                    abs(float((long_setup.get("trigger_price") if row["direction"] == "up" else short_setup.get("trigger_price")) or 0.0) - float(row["last_price"]))
                    / float(row["last_price"])
                    * 100.0
                    if row["direction"] in {"up", "down"} and row["last_price"]
                    else None
                ),
                "active_rr_to_tp1": (
                    long_executor.get("rr_to_tp1")
                    if row["direction"] == "up"
                    else short_executor.get("rr_to_tp1")
                    if row["direction"] == "down"
                    else None
                ),
                "active_executor_quality": (
                    long_executor.get("quality")
                    if row["direction"] == "up"
                    else short_executor.get("quality")
                    if row["direction"] == "down"
                    else None
                ),
            }
        )

    return snapshots


def build_event_outcome_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            e.id AS event_id,
            e.event_key,
            e.symbol,
            e.event_type,
            e.direction,
            e.sent_at,
            e.short_term_signal_json,
            e.protections_json,
            e.long_short_plan_json,
            p.horizon_label,
            p.window_start,
            p.window_end,
            p.bars,
            p.close_price,
            p.close_return_pct,
            p.max_runup_pct,
            p.max_drawdown_pct,
            p.tp1_hit,
            p.tp2_hit,
            p.stop_loss_hit
        FROM alert_event_performance p
        JOIN alert_events e ON e.id = p.event_id
        ORDER BY e.sent_at ASC, p.horizon_label ASC
        """
    ).fetchall()

    outcomes: list[dict[str, Any]] = []
    for row in rows:
        short_signal = _json_loads(row["short_term_signal_json"])
        protections = _json_loads(row["protections_json"])
        long_short_plan = _json_loads(row["long_short_plan_json"])

        outcomes.append(
            {
                "event_id": row["event_id"],
                "event_key": row["event_key"],
                "symbol": row["symbol"],
                "event_type": row["event_type"],
                "event_role": event_role(row["event_type"]),
                "is_watch_only": is_watch_only_event(row["event_type"]),
                "direction": row["direction"],
                "sent_at": row["sent_at"],
                "horizon_label": row["horizon_label"],
                "window_start": row["window_start"],
                "window_end": row["window_end"],
                "bars": row["bars"],
                "close_price": row["close_price"],
                "close_return_pct": row["close_return_pct"],
                "max_runup_pct": row["max_runup_pct"],
                "max_drawdown_pct": row["max_drawdown_pct"],
                "tp1_hit": row["tp1_hit"],
                "tp2_hit": row["tp2_hit"],
                "stop_loss_hit": row["stop_loss_hit"],
                "market_regime": short_signal.get("market_regime", "unknown"),
                "signal_bias": short_signal.get("bias", "neutral"),
                "protection_status": protections.get("status", "unknown"),
                "protection_active": _coerce_bool(protections.get("active", False)),
                "analysis_bias": long_short_plan.get("analysis_bias", "neutral"),
                "direction_bias": long_short_plan.get("direction_bias", "neutral"),
                "preferred_setup": long_short_plan.get("preferred_setup", "neutral"),
            }
        )

    return outcomes


def _write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        pl.from_dicts(rows, infer_schema_length=None).write_parquet(path)
        return
    pl.DataFrame({"empty": []}).drop("empty").write_parquet(path)


def export_market_sample_store(
    conn: sqlite3.Connection,
    analytics_dir: Path,
    duckdb_path: Path,
) -> dict[str, Any]:
    analytics_dir.mkdir(parents=True, exist_ok=True)
    market_context_path = analytics_dir / "market_context_snapshots.parquet"
    event_outcomes_path = analytics_dir / "event_outcomes.parquet"

    snapshot_rows = build_market_context_snapshot_rows(conn)
    outcome_rows = build_event_outcome_rows(conn)
    _write_parquet(snapshot_rows, market_context_path)
    _write_parquet(outcome_rows, event_outcomes_path)

    db = duckdb.connect(str(duckdb_path))
    try:
        db.execute(
            "CREATE OR REPLACE TABLE market_context_snapshots AS SELECT * FROM read_parquet(?)",
            [str(market_context_path)],
        )
        db.execute(
            "CREATE OR REPLACE TABLE event_outcomes AS SELECT * FROM read_parquet(?)",
            [str(event_outcomes_path)],
        )
        db.execute(
            """
            CREATE OR REPLACE VIEW event_probabilities AS
            SELECT
                horizon_label,
                event_type,
                event_role,
                market_regime,
                symbol,
                COUNT(*) AS sample_count,
                AVG(close_return_pct) AS avg_close_return_pct,
                AVG(max_runup_pct) AS avg_max_runup_pct,
                AVG(max_drawdown_pct) AS avg_max_drawdown_pct,
                AVG(CASE WHEN close_return_pct > 0 THEN 1.0 ELSE 0.0 END) * 100.0 AS positive_close_rate,
                AVG(tp1_hit) * 100.0 AS tp1_rate,
                AVG(tp2_hit) * 100.0 AS tp2_rate,
                AVG(stop_loss_hit) * 100.0 AS stop_loss_rate
            FROM event_outcomes
            GROUP BY ALL
            """
        )
    finally:
        db.close()

    return {
        "market_context_rows": len(snapshot_rows),
        "event_outcome_rows": len(outcome_rows),
        "market_context_path": market_context_path,
        "event_outcomes_path": event_outcomes_path,
        "duckdb_path": duckdb_path,
    }


def _pct(value: float) -> str:
    return f"{value:.1f}%"


def build_joined_sample_frame(analytics_dir: Path) -> pl.LazyFrame:
    market_context_path = analytics_dir / "market_context_snapshots.parquet"
    event_outcomes_path = analytics_dir / "event_outcomes.parquet"
    if not market_context_path.exists():
        raise FileNotFoundError(f"Missing analytics parquet: {market_context_path}")
    if not event_outcomes_path.exists():
        raise FileNotFoundError(f"Missing analytics parquet: {event_outcomes_path}")
    snapshots = pl.scan_parquet(market_context_path)
    outcomes = pl.scan_parquet(event_outcomes_path)
    return outcomes.join(
        snapshots.select(
            [
                "event_id",
                "event_key",
                "tf5_volume_ratio",
                "tf5_rsi14",
                "tf5_trend",
                "tf5_above_vwap",
                "tf1h_trend",
                "tf4h_trend",
                "active_trigger_distance_pct",
                "active_rr_to_tp1",
                "active_executor_quality",
                "is_sideways",
                "market_state_label",
                "long_core_score",
                "short_core_score",
                "long_micro_score",
                "short_micro_score",
            ]
        ),
        on=["event_id", "event_key"],
        how="left",
    )


def _append_ranked_bucket(lines: list[str], title: str, frame: pl.DataFrame) -> None:
    lines.append(title)
    if frame.is_empty():
        lines.append("- 無資料")
        lines.append("")
        return
    for row in frame.iter_rows(named=True):
        bucket = row["bucket"]
        lines.append(
            f"- `{bucket}`: 樣本 `{int(row['sample_count'])}`，正報酬率 `{_pct(row['positive_close_rate'])}`，"
            f"平均收盤報酬 `{row['avg_close_return_pct']:.3f}%`，TP1 `{_pct(row['tp1_rate'])}`，"
            f"SL `{_pct(row['stop_loss_rate'])}`"
        )
    lines.append("")


def build_conditional_probability_report(
    analytics_dir: Path,
    duckdb_path: Path,
    report_path: Path,
    *,
    min_samples: int = 3,
) -> None:
    event_outcomes_path = analytics_dir / "event_outcomes.parquet"
    if not event_outcomes_path.exists():
        raise FileNotFoundError(f"Missing analytics parquet: {event_outcomes_path}")

    outcomes = build_joined_sample_frame(analytics_dir)

    def aggregate(bucket_expr: pl.Expr, *, actionable_only: bool = False) -> pl.DataFrame:
        scan = outcomes
        if actionable_only:
            scan = scan.filter(pl.col("is_watch_only") == False)
        return (
            scan.group_by(["horizon_label", bucket_expr.alias("bucket")])
            .agg(
                pl.len().alias("sample_count"),
                pl.mean("close_return_pct").alias("avg_close_return_pct"),
                pl.mean("positive_close").alias("positive_close_rate"),
                pl.mean("tp1_hit_rate").alias("tp1_rate"),
                pl.mean("stop_loss_hit_rate").alias("stop_loss_rate"),
            )
            .with_columns(
                positive_close_rate=pl.col("positive_close_rate") * 100.0,
                tp1_rate=pl.col("tp1_rate") * 100.0,
                stop_loss_rate=pl.col("stop_loss_rate") * 100.0,
            )
            .filter(pl.col("sample_count") >= min_samples)
            .sort(["horizon_label", "avg_close_return_pct", "positive_close_rate"], descending=[False, True, True])
            .collect()
        )

    enriched = outcomes.with_columns(
        positive_close=(pl.col("close_return_pct") > 0).cast(pl.Float64),
        tp1_hit_rate=pl.col("tp1_hit").cast(pl.Float64),
        stop_loss_hit_rate=pl.col("stop_loss_hit").cast(pl.Float64),
    )
    outcomes = enriched

    overall = aggregate(pl.col("event_type"))
    by_regime = aggregate(
        pl.concat_str([pl.col("event_type"), pl.lit(" | "), pl.col("market_regime")], separator="")
    )
    actionable = aggregate(pl.col("event_type"), actionable_only=True)
    by_symbol_regime = aggregate(
        pl.concat_str([pl.col("symbol"), pl.lit(" | "), pl.col("event_type"), pl.lit(" | "), pl.col("market_regime")], separator=""),
        actionable_only=True,
    )
    by_symbol_regime_protection = aggregate(
        pl.concat_str(
            [
                pl.col("symbol"),
                pl.lit(" | "),
                pl.col("event_type"),
                pl.lit(" | "),
                pl.col("market_regime"),
                pl.lit(" | protection="),
                pl.col("protection_status"),
            ],
            separator="",
        ),
        actionable_only=True,
    )

    total_context_rows = duckdb.sql(
        "SELECT COUNT(*) FROM read_parquet(?)",
        params=[str(analytics_dir / "market_context_snapshots.parquet")],
    ).fetchone()[0]
    total_outcome_rows = duckdb.sql(
        "SELECT COUNT(*) FROM read_parquet(?)",
        params=[str(event_outcomes_path)],
    ).fetchone()[0]

    lines = [
        "# 條件機率分析報告",
        "",
        "- 這份報告使用 `DuckDB + Parquet + Polars` 的分析層。",
        f"- 市場樣本快照數: `{total_context_rows}`",
        f"- 結果樣本數: `{total_outcome_rows}`",
        f"- DuckDB: `{duckdb_path}`",
        f"- Parquet 目錄: `{analytics_dir}`",
        f"- 最小樣本門檻: `{min_samples}`",
        "",
    ]

    for horizon in ["15m", "1h", "4h", "24h"]:
        lines.append(f"## {horizon}")
        lines.append("")
        _append_ranked_bucket(
            lines,
            "### 事件類型整體條件機率",
            overall.filter(pl.col("horizon_label") == horizon),
        )
        _append_ranked_bucket(
            lines,
            "### 事件類型 x 市場狀態",
            by_regime.filter(pl.col("horizon_label") == horizon),
        )
        _append_ranked_bucket(
            lines,
            "### 可交易事件整體條件機率",
            actionable.filter(pl.col("horizon_label") == horizon),
        )
        _append_ranked_bucket(
            lines,
            "### 可交易事件 x 幣種 x 市場狀態",
            by_symbol_regime.filter(pl.col("horizon_label") == horizon),
        )
        _append_ranked_bucket(
            lines,
            "### 可交易事件 x 幣種 x 市場狀態 x 保護層",
            by_symbol_regime_protection.filter(pl.col("horizon_label") == horizon),
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_threshold_sweep(
    lines: list[str],
    title: str,
    frame: pl.DataFrame,
    threshold_label: str,
) -> None:
    lines.append(title)
    if frame.is_empty():
        lines.append("- 無足夠樣本")
        lines.append("")
        return
    for row in frame.iter_rows(named=True):
        lines.append(
            f"- `{row[threshold_label]}`: 樣本 `{int(row['sample_count'])}`，正報酬率 `{_pct(row['positive_close_rate'])}`，"
            f"平均收盤報酬 `{row['avg_close_return_pct']:.3f}%`，SL `{_pct(row['stop_loss_rate'])}`"
        )
    lines.append("")


def build_parameter_tuning_report(
    analytics_dir: Path,
    report_path: Path,
    *,
    min_samples: int = 5,
) -> None:
    joined = build_joined_sample_frame(analytics_dir).with_columns(
        positive_close=(pl.col("close_return_pct") > 0).cast(pl.Float64),
        stop_loss_hit_rate=pl.col("stop_loss_hit").cast(pl.Float64),
    )

    def summarize_thresholds(
        base: pl.LazyFrame,
        thresholds: list[float],
        threshold_name: str,
    ) -> pl.DataFrame:
        frames: list[pl.DataFrame] = []
        for threshold in thresholds:
            agg = (
                base.filter(pl.col(threshold_name) >= threshold if "rr" in threshold_name or "volume" in threshold_name else pl.col(threshold_name) <= threshold)
                .group_by(["horizon_label", "event_type"])
                .agg(
                    pl.len().alias("sample_count"),
                    pl.mean("close_return_pct").alias("avg_close_return_pct"),
                    pl.mean("positive_close").alias("positive_close_rate"),
                    pl.mean("stop_loss_hit_rate").alias("stop_loss_rate"),
                )
                .with_columns(
                    positive_close_rate=pl.col("positive_close_rate") * 100.0,
                    stop_loss_rate=pl.col("stop_loss_rate") * 100.0,
                    threshold=pl.lit(threshold),
                )
                .filter(pl.col("sample_count") >= min_samples)
                .collect()
            )
            if not agg.is_empty():
                frames.append(agg)
        if not frames:
            return pl.DataFrame(
                {
                    "horizon_label": [],
                    "event_type": [],
                    "sample_count": [],
                    "avg_close_return_pct": [],
                    "positive_close_rate": [],
                    "stop_loss_rate": [],
                    "threshold": [],
                }
            )
        return pl.concat(frames).sort(
            ["horizon_label", "event_type", "avg_close_return_pct", "positive_close_rate"],
            descending=[False, False, True, True],
        )

    retest_base = joined.filter(pl.col("event_type").is_in(["retest_hold_long", "retest_hold_short"]))
    distance_base = joined.filter(
        pl.col("event_type").is_in(["approach_up", "approach_down", "effective_long_breakout", "effective_short_breakdown"])
        & pl.col("active_trigger_distance_pct").is_not_null()
    )
    rr_base = joined.filter(
        pl.col("event_type").is_in(["second_breakout_long", "second_breakdown_short"])
        & pl.col("active_rr_to_tp1").is_not_null()
    )

    retest_sweep = summarize_thresholds(retest_base, [1.0, 1.05, 1.1, 1.15, 1.2], "tf5_volume_ratio")
    distance_sweep = summarize_thresholds(distance_base, [0.25, 0.35, 0.5, 0.75, 1.0], "active_trigger_distance_pct")
    rr_sweep = summarize_thresholds(rr_base, [1.0, 1.2, 1.4, 1.6], "active_rr_to_tp1")

    def pick_best(frame: pl.DataFrame, threshold_col: str) -> pl.DataFrame:
        if frame.is_empty():
            return frame
        return (
            frame.group_by(["horizon_label", "event_type"])
            .agg(
                pl.all().sort_by(["avg_close_return_pct", "positive_close_rate"], descending=[True, True]).first()
            )
            .select(
                "horizon_label",
                "event_type",
                pl.col(threshold_col).alias("best_threshold"),
                "sample_count",
                "avg_close_return_pct",
                "positive_close_rate",
                "stop_loss_rate",
            )
            .sort(["horizon_label", "avg_close_return_pct"], descending=[False, True])
        )

    retest_best = pick_best(retest_sweep, "threshold")
    distance_best = pick_best(distance_sweep, "threshold")
    rr_best = pick_best(rr_sweep, "threshold")

    lines = [
        "# 參數調校建議報告",
        "",
        "- 這份報告根據目前的市場樣本庫，自動掃描幾組值得先調的門檻。",
        f"- 最小樣本門檻: `{min_samples}`",
        "",
        "## 建議摘要",
        "",
    ]

    if not retest_best.is_empty():
        for row in retest_best.iter_rows(named=True):
            lines.append(
                f"- `{row['event_type']} @ {row['horizon_label']}`：`retest_volume_ratio_min` 可先看 `{row['best_threshold']}`，"
                f"平均收盤報酬 `{row['avg_close_return_pct']:.3f}%`，正報酬率 `{_pct(row['positive_close_rate'])}`。"
            )
    if not distance_best.is_empty():
        for row in distance_best.iter_rows(named=True):
            lines.append(
                f"- `{row['event_type']} @ {row['horizon_label']}`：可先把 `trigger_distance_pct` 控在 `{row['best_threshold']}` 以內，"
                f"平均收盤報酬 `{row['avg_close_return_pct']:.3f}%`。"
            )
    if not rr_best.is_empty():
        for row in rr_best.iter_rows(named=True):
            lines.append(
                f"- `{row['event_type']} @ {row['horizon_label']}`：`RR(TP1)` 至少 `{row['best_threshold']}` 看起來更健康，"
                f"平均收盤報酬 `{row['avg_close_return_pct']:.3f}%`。"
            )
    if retest_best.is_empty() and distance_best.is_empty() and rr_best.is_empty():
        lines.append("- 目前樣本量還不夠，先繼續累積 live 樣本。")
    lines.append("")

    for horizon in ["15m", "1h", "4h", "24h"]:
        lines.append(f"## {horizon}")
        lines.append("")
        _append_threshold_sweep(
            lines,
            "### retest 量能門檻掃描",
            retest_sweep.filter(pl.col("horizon_label") == horizon).with_columns(threshold=pl.col("threshold").cast(pl.String)),
            "threshold",
        )
        _append_threshold_sweep(
            lines,
            "### trigger 距離門檻掃描",
            distance_sweep.filter(pl.col("horizon_label") == horizon).with_columns(threshold=pl.col("threshold").cast(pl.String)),
            "threshold",
        )
        _append_threshold_sweep(
            lines,
            "### second 事件 RR 門檻掃描",
            rr_sweep.filter(pl.col("horizon_label") == horizon).with_columns(threshold=pl.col("threshold").cast(pl.String)),
            "threshold",
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
