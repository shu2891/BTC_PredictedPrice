import datetime as dt
import unittest

import historical_replay_backtest as replay


class HistoricalReplayMacroOverlayTests(unittest.TestCase):
    def test_synthesize_macro_news_summary_marks_event_window_high_risk(self) -> None:
        event_ts = dt.datetime(2026, 3, 18, 18, 0, tzinfo=dt.timezone.utc)
        calendar = [
            {
                "name": "FOMC Statement",
                "label": "FOMC 2026-03-18",
                "severity": "high",
                "pre_minutes": 720,
                "post_minutes": 480,
                "timestamp_utc": event_ts,
                "timestamp_ms": int(event_ts.timestamp() * 1000),
            }
        ]

        current_ts_ms = int(dt.datetime(2026, 3, 18, 12, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)
        summary = replay.synthesize_macro_news_summary(current_ts_ms, calendar)

        self.assertEqual(summary["event_risk_level"], "high")
        self.assertEqual(summary["event_risk_score"], 10)
        self.assertIn("FOMC 2026-03-18", summary["key_event_headlines"])

    def test_synthesize_macro_news_summary_is_low_risk_outside_window(self) -> None:
        event_ts = dt.datetime(2026, 3, 18, 18, 0, tzinfo=dt.timezone.utc)
        calendar = [
            {
                "name": "FOMC Statement",
                "label": "FOMC 2026-03-18",
                "severity": "high",
                "pre_minutes": 720,
                "post_minutes": 480,
                "timestamp_utc": event_ts,
                "timestamp_ms": int(event_ts.timestamp() * 1000),
            }
        ]

        current_ts_ms = int(dt.datetime(2026, 3, 16, 12, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)
        summary = replay.synthesize_macro_news_summary(current_ts_ms, calendar)

        self.assertEqual(summary["event_risk_level"], "low")
        self.assertEqual(summary["event_risk_score"], 1)

    def test_build_report_includes_protection_distribution(self) -> None:
        report = replay.build_report(
            events=[
                {
                    "symbol": "BTCUSDT",
                    "event_type": "effective_short_breakdown",
                    "protection_status": "guarded",
                    "evaluations": {
                        "1h": {
                            "direction_hit": 1,
                            "close_return_pct": 1.0,
                            "max_runup_pct": 1.5,
                            "max_drawdown_pct": -0.2,
                            "tp1_hit": 1,
                            "tp2_hit": 0,
                            "stop_loss_hit": 0,
                        }
                    },
                }
            ],
            horizons=["1h"],
            start_dt=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            end_dt=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(report["protection_distribution"]["guarded"], 1)


if __name__ == "__main__":
    unittest.main()
