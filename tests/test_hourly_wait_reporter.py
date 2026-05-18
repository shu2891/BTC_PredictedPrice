import argparse
import unittest
from unittest.mock import patch

import hourly_wait_reporter as reporter


class HourlyWaitReporterTests(unittest.TestCase):
    def test_build_report_message_contains_core_sections(self) -> None:
        message = reporter.build_report_message(
            [
                {
                    "symbol": "BTCUSDT",
                    "price": 74123.0,
                    "actionable_levels": {
                        "range_low": 73500.0,
                        "range_high": 74500.0,
                        "breakout_up": 74600.0,
                        "breakout_down": 73400.0,
                    },
                    "short_term_signal": {"bias": "long", "strength": "medium"},
                }
            ]
        )

        self.assertIn("[check_price]", message)
        self.assertIn("BTCUSDT", message)
        self.assertIn("74600", message)
        self.assertIn("73400", message)

    def test_run_cycle_builds_results_and_sends_message(self) -> None:
        fake_result = {
            "symbol": "BTCUSDT",
            "price": 74123.0,
            "actionable_levels": {
                "range_low": 73500.0,
                "range_high": 74500.0,
                "breakout_up": 74600.0,
                "breakout_down": 73400.0,
            },
            "short_term_signal": {"bias": "long", "strength": "medium"},
        }

        with (
            patch.object(reporter, "load_config", return_value={"symbols": ["BTC"], "risk_profile": "conservative"}),
            patch.object(reporter, "normalize_symbol", side_effect=lambda symbol, quote: f"{symbol}{quote}"),
            patch.object(reporter, "fetch_rss_items", return_value=[]),
            patch.object(reporter, "build_symbol_analysis", return_value=object()),
            patch.object(reporter, "analysis_to_dict", return_value=fake_result),
            patch.object(reporter, "send_telegram") as send_telegram,
        ):
            reporter.run_cycle(
                config_path=reporter.Path("watchlist.json"),
                timeout=1,
                llama_mode="off",
                llama_model="unused",
                quote="USDT",
                telegram_script=reporter.DEFAULT_TELEGRAM_SCRIPT,
            )

        send_telegram.assert_called_once()
        sent_message = send_telegram.call_args.args[1]
        self.assertIn("BTCUSDT", sent_message)
        self.assertIn("74600", sent_message)

    def test_main_reraises_cycle_error_when_once(self) -> None:
        args = argparse.Namespace(
            config="watchlist.json",
            loop_minutes=240,
            timeout=15,
            llama="off",
            llama_model="llama3.2:3b",
            quote="USDT",
            telegram_script=str(reporter.DEFAULT_TELEGRAM_SCRIPT),
            once=True,
        )

        with (
            patch.object(reporter, "parse_args", return_value=args),
            patch.object(reporter, "run_cycle", side_effect=RuntimeError("boom")),
            patch.object(reporter.time, "sleep"),
        ):
            with self.assertRaises(RuntimeError):
                reporter.main()


if __name__ == "__main__":
    unittest.main()
