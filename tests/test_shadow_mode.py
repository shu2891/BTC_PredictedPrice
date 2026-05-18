import unittest

import shadow_mode


class ShortTermSignalTests(unittest.TestCase):
    def test_5m_strength_alone_does_not_override_bearish_4h_context(self) -> None:
        tf5 = {
            "trend": "bullish",
            "rsi14": 60.0,
            "above_vwap": True,
            "volume_ratio": 1.4,
        }
        tf4h = {
            "trend": "bearish",
        }
        returns = {
            "4h": -1.2,
            "24h": -4.5,
        }
        false_breakout = {
            "false_breakout_up": False,
            "false_breakout_down": False,
        }

        signal = shadow_mode.build_swing_signal(tf5, tf4h, 100.0, false_breakout, returns)

        self.assertEqual(signal["market_regime"], "bear_trend")
        self.assertNotEqual(signal["bias"], "long")
        self.assertFalse(signal["gate_open"]["long"])

    def test_4h_and_24h_support_can_open_long_gate(self) -> None:
        tf5 = {
            "trend": "bullish",
            "rsi14": 61.0,
            "above_vwap": True,
            "volume_ratio": 1.35,
        }
        tf4h = {
            "trend": "bullish",
        }
        returns = {
            "4h": 0.8,
            "24h": 3.8,
        }
        false_breakout = {
            "false_breakout_up": False,
            "false_breakout_down": False,
        }

        signal = shadow_mode.build_swing_signal(tf5, tf4h, 100.0, false_breakout, returns)

        self.assertEqual(signal["market_regime"], "bull_trend")
        self.assertEqual(signal["bias"], "long")
        self.assertTrue(signal["gate_open"]["long"])
        self.assertGreaterEqual(signal["long_core_score"], 2)

    def test_onchain_bearish_overlay_blocks_long_bias(self) -> None:
        tf1h = {
            "trend": "bullish",
            "rsi14": 61.0,
            "above_vwap": True,
            "volume_ratio": 1.35,
        }
        tf4h = {
            "trend": "bullish",
        }
        returns = {
            "4h": 0.8,
            "24h": 3.8,
        }
        false_breakout = {
            "false_breakout_up": False,
            "false_breakout_down": False,
        }

        signal = shadow_mode.build_swing_signal(
            tf1h,
            tf4h,
            100.0,
            false_breakout,
            returns,
            {
                "available": True,
                "bias": "bearish",
                "confidence": "high",
                "summary": ["活躍地址與交易數同步走弱"],
            },
        )

        self.assertNotEqual(signal["bias"], "long")
        self.assertEqual(signal["onchain_bias"], "bearish")

    def test_funding_bearish_overlay_blocks_long_bias(self) -> None:
        tf1h = {
            "trend": "bullish",
            "rsi14": 61.0,
            "above_vwap": True,
            "volume_ratio": 1.35,
        }
        tf4h = {
            "trend": "bullish",
        }
        returns = {
            "4h": 0.8,
            "24h": 3.8,
        }
        false_breakout = {
            "false_breakout_up": False,
            "false_breakout_down": False,
        }

        signal = shadow_mode.build_swing_signal(
            tf1h,
            tf4h,
            100.0,
            false_breakout,
            returns,
            {},
            funding_summary={
                "available": True,
                "bias": "bearish",
                "signal": "BLOCK_LONG",
                "current_rate_pct": 0.06,
                "avg_recent_rate_pct": 0.04,
            },
            swing_mtf_score={"score": 68.0, "bias": "long", "components": {}, "weights": {}},
        )

        self.assertNotEqual(signal["bias"], "long")
        self.assertEqual(signal["funding_bias"], "bearish")

    def test_build_swing_mtf_score_favors_aligned_bullish_structure(self) -> None:
        tf15m = {
            "trend": "bullish",
            "rsi14": 61.0,
            "return_6bar_pct": 1.2,
            "above_vwap": True,
        }
        tf1h = {
            "trend": "bullish",
            "rsi14": 59.0,
            "return_6bar_pct": 1.1,
            "above_vwap": True,
        }
        tf4h = {
            "trend": "bullish",
            "rsi14": 57.0,
            "return_6bar_pct": 0.9,
            "above_vwap": True,
        }

        score = shadow_mode.build_swing_mtf_score(tf15m, tf1h, tf4h)

        self.assertGreaterEqual(score["score"], 62.0)
        self.assertEqual(score["bias"], "long")


class ActionableLevelTests(unittest.TestCase):
    def test_derive_actionable_levels_excludes_current_bar_from_breakout_reference(self) -> None:
        candles = []
        for idx in range(241):
            high = 100.0
            low = 90.0
            close = 95.0
            if idx == 240:
                high = 130.0
                low = 94.0
                close = 128.0
            candles.append(
                shadow_mode.Candle(
                    ts_ms=idx * 60_000,
                    open=95.0,
                    high=high,
                    low=low,
                    close=close,
                    volume=1.0,
                )
            )

        market_state, actionable_levels = shadow_mode.derive_actionable_levels(
            candles_1m=candles,
            price=128.0,
            volatility={"atr_pct": 1.0, "realized_24h": 2.0},
            returns={"1h": 0.2, "4h": 0.8, "24h": 3.0},
            trend_view="bullish",
        )

        self.assertEqual(market_state["range_4h"], [90.0, 100.0])
        self.assertAlmostEqual(actionable_levels["breakout_up"], 100.15, places=4)
        self.assertLess(actionable_levels["breakout_up"], 130.0)

    def test_build_price_map_marks_sideways_mid_and_next_levels(self) -> None:
        actionable_levels = {
            "range_low": 90.0,
            "range_high": 100.0,
            "breakout_up": 100.15,
            "breakout_down": 89.865,
            "long_ready_zone": [99.6, 100.15],
            "short_ready_zone": [89.865, 90.4],
            "noise_zone": [93.0, 97.0],
            "timing_window": "6-18 小時",
            "timing_confidence": "中",
        }
        long_short_plan = {
            "long_setup": {
                "take_profit": [102.0, 104.0],
                "confirmation": {"retest_zone": [99.5, 100.2]},
            },
            "short_setup": {
                "take_profit": [88.0, 86.0],
                "confirmation": {"retest_zone": [89.8, 90.5]},
            },
        }

        price_map = shadow_mode.build_price_map(
            price=95.0,
            market_state={"is_sideways": True},
            actionable_levels=actionable_levels,
            long_short_plan=long_short_plan,
        )

        self.assertEqual(price_map["phase"], "橫盤中段")
        self.assertEqual(price_map["primary_support"], [89.865, 90.4])
        self.assertEqual(price_map["primary_resistance"], [99.6, 100.15])
        self.assertIn("若跌破", price_map["if_break_down"])

    def test_build_price_map_marks_breakout_extension_with_retest_support(self) -> None:
        actionable_levels = {
            "range_low": 90.0,
            "range_high": 100.0,
            "breakout_up": 100.15,
            "breakout_down": 89.865,
            "long_ready_zone": [99.6, 100.15],
            "short_ready_zone": [89.865, 90.4],
            "noise_zone": [93.0, 97.0],
            "timing_window": "1-8 小時",
            "timing_confidence": "高",
        }
        long_short_plan = {
            "long_setup": {
                "take_profit": [102.0, 104.0],
                "confirmation": {"retest_zone": [99.5, 100.2]},
            },
            "short_setup": {
                "take_profit": [88.0, 86.0],
                "confirmation": {"retest_zone": [89.8, 90.5]},
            },
        }

        price_map = shadow_mode.build_price_map(
            price=101.2,
            market_state={"is_sideways": False},
            actionable_levels=actionable_levels,
            long_short_plan=long_short_plan,
        )

        self.assertEqual(price_map["phase"], "上破延伸")
        self.assertEqual(price_map["primary_support"], [99.5, 100.2])
        self.assertEqual(price_map["primary_resistance"], [102.0, 102.0])


class NewsOverlayTests(unittest.TestCase):
    def test_summarize_news_detects_macro_risk_off_event(self) -> None:
        rss_items = [
            {
                "title": "Fed hawkish stance pressures crypto market ahead of FOMC",
                "description": "Bitcoin and Ethereum traders brace for inflation and rate hike risks.",
                "link": "https://example.com/fed",
            }
        ]

        items, summary = shadow_mode.summarize_news("BTCUSDT", rss_items)

        self.assertEqual(len(items), 1)
        self.assertEqual(summary["macro_bias"], "risk_off")
        self.assertEqual(summary["event_risk_level"], "high")
        self.assertGreaterEqual(summary["expires_hours"], 12)

    def test_news_overlay_no_longer_changes_risk_or_decision(self) -> None:
        base_kwargs = {
            "symbol": "BTCUSDT",
            "price": 100.0,
            "returns": {"5m": 0.2, "1h": 0.4, "4h": 0.8, "24h": 1.5},
            "volatility": {"atr_pct": 0.9, "realized_24h": 1.2},
            "trend": {"trend_view": "bullish", "rsi14": 60.0},
            "volume_ratio": 1.2,
            "profile": "conservative",
        }

        neutral = shadow_mode.build_rule_based_decision(
            news_summary={
                "sentiment": "neutral",
                "score": 0.0,
                "confidence": 0.4,
                "conflict": False,
                "macro_bias": "neutral",
                "macro_confidence": 0.35,
                "macro_conflict": False,
                "event_risk_level": "low",
                "event_risk_score": 1,
                "expires_hours": 0,
                "key_event_headlines": [],
            },
            **base_kwargs,
        )
        high_risk = shadow_mode.build_rule_based_decision(
            news_summary={
                "sentiment": "neutral",
                "score": 0.0,
                "confidence": 0.8,
                "conflict": True,
                "macro_bias": "risk_off",
                "macro_confidence": 0.82,
                "macro_conflict": True,
                "event_risk_level": "high",
                "event_risk_score": 10,
                "expires_hours": 12,
                "key_event_headlines": ["Fed hawkish stance pressures crypto market"],
            },
            **base_kwargs,
        )

        self.assertEqual(neutral["risk_score"], high_risk["risk_score"])
        self.assertEqual(neutral["decision"], high_risk["decision"])


class FundingSummaryTests(unittest.TestCase):
    def test_summarize_funding_summary_detects_block_long(self) -> None:
        summary = shadow_mode.summarize_funding_summary(
            "BTCUSDT",
            current_rate=0.0006,
            avg_recent_rate=0.0004,
            settings={
                "extreme_positive": 0.0003,
                "extreme_negative": -0.0001,
                "block_long_above": 0.0005,
                "block_short_below": -0.0003,
            },
        )

        self.assertEqual(summary["signal"], "BLOCK_LONG")
        self.assertEqual(summary["bias"], "bearish")


class ProtectionIntegrationTests(unittest.TestCase):
    def test_apply_protections_downgrades_scale_in_test_to_watch(self) -> None:
        decision = {
            "decision": "scale_in_test",
            "warnings": [],
        }
        protections = {
            "active": True,
            "hard_block": False,
            "summaries": ["BTCUSDT 處於 bear_trend，暫停逆勢做多確認訊號。"],
        }

        updated = shadow_mode.apply_protections_to_decision(decision, protections)

        self.assertEqual(updated["decision"], "watch")
        self.assertIn("暫停逆勢做多確認訊號", updated["warnings"][0])


if __name__ == "__main__":
    unittest.main()
