import unittest

import market_trend_report as report


class MarketTrendReportTests(unittest.TestCase):
    def test_build_market_outlook_message_contains_key_levels(self) -> None:
        payload = {
            "generated_at": "2026-04-19T14:30:00+00:00",
            "risk_profile": "conservative",
            "results": [
                {
                    "symbol": "BTCUSDT",
                    "price": 75781.58,
                    "risk_level": "medium",
                    "short_term_signal": {"market_regime": "range_or_mixed"},
                    "long_short_plan": {
                        "direction_bias": "long_bias",
                        "recommendation": "偏多，若上破並站穩 1h 結構，可規劃波段多單。",
                    },
                    "actionable_levels": {
                        "breakout_up": 76355.021,
                        "breakout_down": 74883.506,
                        "price_map": {
                            "phase": "橫盤中段",
                            "if_break_up": "若站上 `76355.0210`，下一壓力先看 `78173.7789`。",
                            "if_break_down": "若跌破 `74883.5060`，下一支撐先看 `73064.7481`。",
                        },
                    },
                }
            ],
        }

        message = report.build_market_outlook_message(payload)

        self.assertIn("市場現況報告", message)
        self.assertIn("BTCUSDT", message)
        self.assertIn("上破 76355.021", message)
        self.assertIn("下破 74883.506", message)


if __name__ == "__main__":
    unittest.main()
