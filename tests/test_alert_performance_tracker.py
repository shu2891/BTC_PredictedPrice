import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import alert_performance_tracker as tracker


class EventDirectionTests(unittest.TestCase):
    def test_breakout_touch_up_maps_to_up(self) -> None:
        self.assertEqual(tracker.event_direction("breakout_touch_up"), "up")

    def test_breakout_touch_down_maps_to_down(self) -> None:
        self.assertEqual(tracker.event_direction("breakout_touch_down"), "down")


class ReportTests(unittest.TestCase):
    def test_build_report_separates_watch_only_and_actionable_breakdowns(self) -> None:
        conn = tracker.sqlite3.connect(":memory:")
        conn.row_factory = tracker.sqlite3.Row
        tracker.ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO alert_events (
                event_key, symbol, event_type, direction, level, sent_at, last_price,
                stop_loss, take_profit_1, take_profit_2, timeframe_view_json,
                actionable_levels_json, short_term_signal_json, protections_json, long_short_plan_json, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "k1",
                "ETHUSDT",
                "approach_up",
                "up",
                100.0,
                "2026-03-18T00:00:00+00:00",
                100.0,
                None,
                None,
                None,
                "{}",
                "{}",
                '{"market_regime":"range_or_mixed"}',
                '{"status":"normal","active":false}',
                "{}",
                "watch",
            ),
        )
        conn.execute(
            """
            INSERT INTO alert_events (
                event_key, symbol, event_type, direction, level, sent_at, last_price,
                stop_loss, take_profit_1, take_profit_2, timeframe_view_json,
                actionable_levels_json, short_term_signal_json, protections_json, long_short_plan_json, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "k2",
                "ETHUSDT",
                "second_breakout_long",
                "up",
                101.0,
                "2026-03-18T00:05:00+00:00",
                101.0,
                99.0,
                103.0,
                105.0,
                "{}",
                "{}",
                '{"market_regime":"bull_trend"}',
                '{"status":"guarded","active":true}',
                "{}",
                "actionable",
            ),
        )
        conn.execute(
            """
            INSERT INTO alert_event_performance (
                event_id, horizon_label, window_start, window_end, bars, close_price,
                close_return_pct, max_runup_pct, max_drawdown_pct, tp1_hit, tp2_hit,
                stop_loss_hit, evaluation_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "1h", "a", "b", 5, 100.5, 0.5, 1.0, -0.2, 0, 0, 0, "{}", "c"),
        )
        conn.execute(
            """
            INSERT INTO alert_event_performance (
                event_id, horizon_label, window_start, window_end, bars, close_price,
                close_return_pct, max_runup_pct, max_drawdown_pct, tp1_hit, tp2_hit,
                stop_loss_hit, evaluation_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (2, "1h", "a", "b", 5, 103.0, 1.98, 2.5, -0.4, 1, 0, 0, "{}", "c"),
        )
        conn.commit()

        with TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "report.md"
            tracker.build_report(conn, report_path, ["1h"])
            content = report_path.read_text(encoding="utf-8")

        self.assertIn("哪類可交易事件最值得看", content)
        self.assertIn("可交易事件拆分（方向 x 市場狀態）", content)
        self.assertIn("可交易事件拆分（幣種 x 市場狀態 x 方向）", content)
        self.assertIn("觀察事件（watch-only，和可掛單事件分開看）", content)
        self.assertIn("ETHUSDT | bull_trend | up", content)
        self.assertIn("up | bull_trend", content)
        self.assertIn("protection=guarded", content)
        self.assertIn("approach_up", content)
        self.assertIn("second_breakout_long", content)


if __name__ == "__main__":
    unittest.main()
