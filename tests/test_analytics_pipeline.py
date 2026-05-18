import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analytics_pipeline import (
    build_conditional_probability_report,
    build_parameter_tuning_report,
    export_market_sample_store,
)


class AnalyticsPipelineTests(unittest.TestCase):
    def test_exports_parquet_duckdb_and_probability_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "alert_state.db"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE alert_events (
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
                CREATE TABLE alert_event_performance (
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
                    created_at TEXT NOT NULL
                )
                """
            )
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
                    "e-1",
                    "BTCUSDT",
                    "effective_short_breakdown",
                    "down",
                    100.0,
                    "2026-03-22T00:00:00+00:00",
                    100.0,
                    101.0,
                    99.0,
                    98.0,
                    '{"5m":{"trend":"bearish","volume_ratio":1.4,"above_vwap":false,"rsi14":42},"1h":{"trend":"bearish"},"4h":{"trend":"bearish"}}',
                    '{"breakout_up":101.0,"breakout_down":99.5,"price_map":{"market_phase":"trend"}}',
                    '{"market_regime":"bear_trend","bias":"short","strength":"high","long_core_score":0,"short_core_score":3,"long_micro_score":1,"short_micro_score":4,"gate_open":{"long":false,"short":true}}',
                    '{"status":"normal","active":false}',
                    '{"analysis_bias":"short_bias","direction_bias":"short_bias","preferred_setup":"short","execution_readiness":"ready","long_setup":{"executor_plan":{"quality":"observe_only"}},"short_setup":{"trigger_price":99.5,"executor_plan":{"quality":"tradable","rr_to_tp1":1.4}}}',
                    "message",
                ),
            )
            conn.execute(
                """
                INSERT INTO alert_event_performance (
                    event_id, horizon_label, window_start, window_end, bars, close_price,
                    close_return_pct, max_runup_pct, max_drawdown_pct, tp1_hit, tp2_hit,
                    stop_loss_hit, evaluation_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "1h",
                    "2026-03-22T00:00:00+00:00",
                    "2026-03-22T01:00:00+00:00",
                    12,
                    98.7,
                    1.3,
                    1.9,
                    -0.4,
                    1,
                    0,
                    0,
                    "{}",
                    "2026-03-22T02:00:00+00:00",
                ),
            )
            conn.commit()

            analytics_dir = root / "analytics"
            duckdb_path = analytics_dir / "market_samples.duckdb"
            report_path = root / "條件機率分析報告.md"
            tuning_report_path = root / "參數調校建議報告.md"
            query_report_path = root / "歷史市場樣本查詢.md"

            summary = export_market_sample_store(conn, analytics_dir, duckdb_path)
            build_conditional_probability_report(analytics_dir, duckdb_path, report_path, min_samples=1)
            build_parameter_tuning_report(analytics_dir, tuning_report_path, min_samples=1)
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "historical_market_sample_query.py"),
                    "--analytics-db",
                    str(duckdb_path),
                    "--symbol",
                    "BTCUSDT",
                    "--event-type",
                    "effective_short_breakdown",
                    "--horizon",
                    "1h",
                    "--output-md",
                    str(query_report_path),
                ],
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "historical_market_sample_query.py"),
                    "--analytics-db",
                    str(duckdb_path),
                    "--template",
                    "short_effective_4h",
                    "--output-md",
                    str(root / "template_query.md"),
                ],
                check=True,
            )

            self.assertEqual(summary["market_context_rows"], 1)
            self.assertEqual(summary["event_outcome_rows"], 1)
            self.assertTrue((analytics_dir / "market_context_snapshots.parquet").exists())
            self.assertTrue((analytics_dir / "event_outcomes.parquet").exists())
            self.assertTrue(duckdb_path.exists())
            self.assertTrue(report_path.exists())
            self.assertTrue(tuning_report_path.exists())
            self.assertTrue(query_report_path.exists())
            self.assertTrue((root / "template_query.md").exists())
            content = report_path.read_text(encoding="utf-8")
            self.assertIn("effective_short_breakdown", content)
            self.assertIn("bear_trend", content)
            self.assertIn("參數調校建議報告", tuning_report_path.read_text(encoding="utf-8"))
            self.assertIn("BTCUSDT", query_report_path.read_text(encoding="utf-8"))
            conn.close()
