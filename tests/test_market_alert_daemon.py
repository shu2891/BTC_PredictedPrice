import unittest
from unittest.mock import Mock, patch

import market_alert_daemon as daemon


class RunCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "symbols": ["BTC"],
            "risk_profile": "conservative",
            "cooldown_minutes": 30,
            "alerts": {},
        }
        self.result = {"symbol": "BTCUSDT", "price": 100.0}
        self.events = [
            {"event_type": "effective_long_breakout", "level": 101.0, "message": "first"},
            {"event_type": "second_breakout_long", "level": 102.0, "message": "second"},
        ]
        self.next_state = {"long_stage": "reconfirmed", "short_stage": "idle"}

    def test_paper_event_message_uses_canonical_event_risk_multiplier(self) -> None:
        message = daemon.build_paper_event_message(
            "breakout_touch_up",
            {
                "symbol": "BTCUSDT",
                "price": 100.0,
                "long_short_plan": {
                    "long_setup": {
                        "entry_zone": [100.0, 101.0],
                        "stop_loss": 99.0,
                        "take_profit": [104.0],
                    }
                },
            },
            101.0,
        )

        self.assertIn("提醒權重：0.35x（觀察用）", message)
        self.assertNotIn("建倉：", message)

    def test_paper_event_message_formats_actionable_trade_ticket(self) -> None:
        with patch.object(
            daemon,
            "utc_now",
            return_value=daemon.dt.datetime(2026, 5, 4, 13, 30, tzinfo=daemon.dt.timezone.utc),
        ):
            message = daemon.build_paper_event_message(
                "effective_long_breakout",
                {
                    "symbol": "BTCUSDT",
                    "price": 78550.0,
                    "long_short_plan": {
                        "long_setup": {
                            "entry_zone": [78500.0, 78000.0],
                            "stop_loss": 77700.0,
                            "take_profit": [79200.0, 80000.0],
                            "management": {"runner_zone": [80700.0, 81500.0]},
                        }
                    },
                },
                78500.0,
            )

        self.assertIn("Btc", message)
        self.assertIn("方向：多", message)
        self.assertIn("建倉：78500-78000", message)
        self.assertIn("止損：77700", message)
        self.assertIn("止盈：79200-80000-80700", message)
        self.assertIn("時間區：台灣時間 21:00-23:59｜美盤開盤", message)
        self.assertIn("備註：進場靈活，不追價；失守就撤", message)

    def test_session_label_handles_cross_midnight_custom_window(self) -> None:
        presentation = {
            "display_timezone": "Asia/Taipei",
            "session_labels": [{"start": "21:00", "end": "01:00", "label": "美盤開盤"}],
        }
        before_midnight = daemon.dt.datetime(2026, 5, 4, 15, 30, tzinfo=daemon.dt.timezone.utc)
        after_midnight = daemon.dt.datetime(2026, 5, 4, 16, 30, tzinfo=daemon.dt.timezone.utc)

        self.assertEqual(daemon.current_session_label(before_midnight, presentation), "台灣時間 21:00-01:00｜美盤開盤")
        self.assertEqual(daemon.current_session_label(after_midnight, presentation), "台灣時間 21:00-01:00｜美盤開盤")

    def test_process_symbol_events_commits_state_when_all_sendable_events_succeed(self) -> None:
        save_state = Mock()
        record_alert = Mock()
        send_telegram = Mock()

        with (
            patch.object(daemon, "should_send", return_value=True),
            patch.object(daemon, "send_telegram", send_telegram),
            patch.object(daemon, "record_alert", record_alert),
            patch.object(daemon, "save_symbol_state", save_state),
        ):
            sent = daemon.process_symbol_events(
                conn=object(),  # type: ignore[arg-type]
                symbol="BTCUSDT",
                result=self.result,
                events=self.events,
                next_state=self.next_state,
                cooldown_minutes=30,
                telegram_script=daemon.DEFAULT_TELEGRAM_SCRIPT,
            )

        self.assertEqual(sent, 2)
        self.assertEqual(send_telegram.call_count, 2)
        self.assertEqual(record_alert.call_count, 2)
        save_state.assert_called_once_with(unittest.mock.ANY, "BTCUSDT", self.next_state)

    def test_process_symbol_events_skips_cooldown_events_and_still_commits_state(self) -> None:
        save_state = Mock()
        record_alert = Mock()
        send_telegram = Mock()

        with (
            patch.object(daemon, "should_send", side_effect=[False, False]),
            patch.object(daemon, "send_telegram", send_telegram),
            patch.object(daemon, "record_alert", record_alert),
            patch.object(daemon, "save_symbol_state", save_state),
        ):
            sent = daemon.process_symbol_events(
                conn=object(),  # type: ignore[arg-type]
                symbol="BTCUSDT",
                result=self.result,
                events=self.events,
                next_state=self.next_state,
                cooldown_minutes=30,
                telegram_script=daemon.DEFAULT_TELEGRAM_SCRIPT,
            )

        self.assertEqual(sent, 0)
        send_telegram.assert_not_called()
        record_alert.assert_not_called()
        save_state.assert_called_once_with(unittest.mock.ANY, "BTCUSDT", self.next_state)

    def test_commits_state_after_all_events_succeed(self) -> None:
        save_state = Mock()
        record_alert = Mock()
        send_telegram = Mock()

        with (
            patch.object(daemon, "fetch_rss_items", return_value=[]),
            patch.object(daemon, "normalize_symbol", side_effect=lambda symbol, quote: f"{symbol}{quote}"),
            patch.object(daemon, "build_symbol_analysis", return_value=object()),
            patch.object(daemon, "analysis_to_dict", return_value=self.result),
            patch.object(daemon, "load_symbol_state", return_value={"long_stage": "idle", "short_stage": "idle"}),
            patch.object(daemon, "build_events", return_value=(self.events, self.next_state)),
            patch.object(daemon, "should_send", return_value=True),
            patch.object(daemon, "send_telegram", send_telegram),
            patch.object(daemon, "record_alert", record_alert),
            patch.object(daemon, "save_symbol_state", save_state),
        ):
            sent = daemon.run_cycle(
                config=self.config,
                state_conn=object(),  # type: ignore[arg-type]
                timeout=1,
                llama_mode="off",
                llama_model="unused",
                quote="USDT",
                telegram_script=daemon.DEFAULT_TELEGRAM_SCRIPT,
            )

        self.assertEqual(sent, 2)
        self.assertEqual(send_telegram.call_count, 2)
        self.assertEqual(record_alert.call_count, 2)
        save_state.assert_called_once()
        self.assertEqual(save_state.call_args.args[2], self.next_state)

    def test_does_not_commit_state_when_later_event_send_fails(self) -> None:
        save_state = Mock()
        record_alert = Mock()
        send_telegram = Mock(side_effect=[None, RuntimeError("boom")])

        with (
            patch.object(daemon, "fetch_rss_items", return_value=[]),
            patch.object(daemon, "normalize_symbol", side_effect=lambda symbol, quote: f"{symbol}{quote}"),
            patch.object(daemon, "build_symbol_analysis", return_value=object()),
            patch.object(daemon, "analysis_to_dict", return_value=self.result),
            patch.object(daemon, "load_symbol_state", return_value={"long_stage": "idle", "short_stage": "idle"}),
            patch.object(daemon, "build_events", return_value=(self.events, self.next_state)),
            patch.object(daemon, "should_send", return_value=True),
            patch.object(daemon, "send_telegram", send_telegram),
            patch.object(daemon, "record_alert", record_alert),
            patch.object(daemon, "save_symbol_state", save_state),
        ):
            sent = daemon.run_cycle(
                config=self.config,
                state_conn=object(),  # type: ignore[arg-type]
                timeout=1,
                llama_mode="off",
                llama_model="unused",
                quote="USDT",
                telegram_script=daemon.DEFAULT_TELEGRAM_SCRIPT,
            )

        self.assertEqual(sent, 1)
        self.assertEqual(send_telegram.call_count, 2)
        self.assertEqual(record_alert.call_count, 1)
        save_state.assert_not_called()

    def test_build_events_suppresses_long_approach_when_short_bias_is_active(self) -> None:
        result = {
            "symbol": "ETHUSDT",
            "price": 100.8,
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.5, 101.0],
                "short_ready_zone": [98.0, 98.5],
            },
            "long_short_plan": {
                "analysis_bias": "short_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.4,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.0,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.8, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "neutral",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": True, "short": True},
                "long_core_score": 1,
                "short_core_score": 2,
            },
            "timeframe_view": {"5m": {"volume_ratio": 1.0, "trend": "bearish", "above_vwap": False, "rsi14": 45.0}},
        }

        events, _ = daemon.build_events(result, {"enable_approach_alerts": True, "notification_language": "zh"}, daemon.default_symbol_state("ETHUSDT"))

        self.assertNotIn("approach_up", [event["event_type"] for event in events])

    def test_approach_alert_includes_stop_loss_and_targets(self) -> None:
        result = {
            "symbol": "ETHUSDT",
            "price": 98.3,
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.5, 101.0],
                "short_ready_zone": [98.0, 98.5],
            },
            "long_short_plan": {
                "analysis_bias": "short_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.4,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.0,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.8, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "neutral",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": True, "short": True},
                "long_core_score": 1,
                "short_core_score": 2,
            },
            "timeframe_view": {"5m": {"volume_ratio": 1.0, "trend": "bearish", "above_vwap": False, "rsi14": 45.0}},
        }

        events, _ = daemon.build_events(
            result,
            {"enable_approach_alerts": True, "notification_language": "zh", "approach_edge_ratio": 1.0},
            daemon.default_symbol_state("ETHUSDT"),
        )

        approach_down = next(event for event in events if event["event_type"] == "approach_down")
        self.assertIn("觀察", approach_down["message"])
        self.assertIn("首破/首跌確認", approach_down["message"])

    def test_mixed_market_can_emit_short_watch_before_short_trade_gate_opens(self) -> None:
        result = {
            "symbol": "ETHUSDT",
            "price": 98.2,
            "risk_level": "high",
            "warnings": ["趨勢偏空，現貨不建議追多。"],
            "market_state": {
                "is_sideways": True,
                "state": "sideways",
            },
            "protections": {
                "pause_short": False,
                "hard_block": False,
            },
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.5, 101.0],
                "short_ready_zone": [98.0, 98.4],
            },
            "long_short_plan": {
                "analysis_bias": "long_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.4,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.0,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.8, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "neutral",
                "market_regime": "range_or_mixed",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": True, "short": False},
                "long_core_score": 2,
                "short_core_score": 0,
            },
            "timeframe_view": {
                "5m": {
                    "volume_ratio": 0.8,
                    "above_vwap": False,
                    "trend": "mixed",
                    "rsi14": 45.0,
                }
            },
        }

        events, _ = daemon.build_events(
            result,
            {"enable_approach_alerts": True, "notification_language": "zh", "approach_edge_ratio": 1.0},
            daemon.default_symbol_state("ETHUSDT"),
        )

        event_types = [event["event_type"] for event in events]
        self.assertIn("approach_down", event_types)
        self.assertNotIn("effective_short_breakdown", event_types)

    def test_approach_up_respects_max_trigger_distance_cap(self) -> None:
        result = {
            "symbol": "BTCUSDT",
            "price": 100.2,
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.0, 101.0],
                "short_ready_zone": [98.0, 98.4],
            },
            "long_short_plan": {
                "analysis_bias": "long_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.4,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.0,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.8, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "neutral",
                "market_regime": "range_or_mixed",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": True, "short": False},
                "long_core_score": 2,
                "short_core_score": 0,
            },
            "timeframe_view": {"5m": {"volume_ratio": 0.9, "above_vwap": True, "trend": "bullish", "rsi14": 58.0}},
        }

        events, _ = daemon.build_events(
            result,
            {
                "enable_approach_alerts": True,
                "notification_language": "zh",
                "approach_up_max_trigger_distance_pct": 0.5,
            },
            daemon.default_symbol_state("BTCUSDT"),
        )

        self.assertNotIn("approach_up", [event["event_type"] for event in events])

    def test_effective_long_breakout_respects_stricter_long_volume_threshold(self) -> None:
        result = {
            "symbol": "ETHUSDT",
            "price": 101.2,
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.7, 101.0],
                "short_ready_zone": [98.0, 98.3],
            },
            "long_short_plan": {
                "analysis_bias": "long_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.5,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.2,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.9, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "long",
                "market_regime": "range_or_mixed",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": True, "short": False},
                "long_core_score": 2,
                "short_core_score": 0,
            },
            "timeframe_view": {"5m": {"volume_ratio": 1.3, "above_vwap": True, "trend": "bullish", "rsi14": 58.0}},
        }

        events, _ = daemon.build_events(
            result,
            {
                "enable_trigger_alerts": True,
                "notification_language": "zh",
                "effective_long_volume_ratio_min": 1.35,
            },
            daemon.default_symbol_state("ETHUSDT"),
        )

        self.assertNotIn("effective_long_breakout", [event["event_type"] for event in events])

    def test_process_telegram_commands_generates_market_report_and_updates_offset(self) -> None:
        conn = unittest.mock.Mock()
        conn.execute.return_value.fetchone.return_value = None

        updates = [
            {
                "update_id": 42,
                "message": {
                    "chat": {"id": "12345"},
                    "text": "/market BTC ETH",
                },
            }
        ]
        payload = {
            "generated_at": "2026-04-19T14:30:00+00:00",
            "risk_profile": "conservative",
            "results": [],
        }

        with (
            patch.object(daemon, "resolve_env", side_effect=lambda key: "12345" if key == "TELEGRAM_CHAT_ID" else "token"),
            patch.object(daemon, "telegram_api_get_updates", return_value=updates),
            patch.object(daemon, "build_market_outlook", return_value=payload) as build_outlook,
            patch.object(daemon, "save_market_outlook", return_value=(daemon.Path("reports/test.json"), daemon.Path("reports/test.md"))),
            patch.object(daemon, "build_market_outlook_message", return_value="[check_price]\n市場現況報告"),
            patch.object(daemon, "send_telegram") as send_telegram,
            patch.object(daemon, "save_runtime_state") as save_runtime_state,
        ):
            processed = daemon.process_telegram_commands(
                conn=conn,
                config_path=daemon.Path("watchlist.json"),
                timeout=15,
                llama_mode="off",
                llama_model="unused",
                quote="USDT",
                reports_dir=daemon.Path("reports"),
                telegram_script=daemon.DEFAULT_TELEGRAM_SCRIPT,
            )

        self.assertEqual(processed, 1)
        build_outlook.assert_called_once()
        self.assertEqual(build_outlook.call_args.kwargs["explicit_symbols"], ["BTCUSDT", "ETHUSDT"])
        send_telegram.assert_called_once()
        self.assertIn("已生成報告 test.md", send_telegram.call_args.args[1])
        save_runtime_state.assert_called_once_with(conn, "telegram_update_offset", "43")

    def test_symbol_specific_long_tuning_blocks_sol_effective_long_breakout(self) -> None:
        result = {
            "symbol": "SOLUSDT",
            "price": 101.2,
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.7, 101.0],
                "short_ready_zone": [98.0, 98.3],
            },
            "long_short_plan": {
                "analysis_bias": "long_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.5,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.2,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.9, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "long",
                "market_regime": "range_or_mixed",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": True, "short": False},
                "long_core_score": 2,
                "short_core_score": 0,
            },
            "timeframe_view": {
                "5m": {"volume_ratio": 1.4, "above_vwap": True, "trend": "bullish", "rsi14": 58.0},
                "1h": {"volume_ratio": 1.4, "above_vwap": True, "trend": "bullish", "rsi14": 58.0},
                "4h": {"trend": "bullish", "above_vwap": True},
            },
        }

        events, _ = daemon.build_events(
            result,
            {
                "enable_trigger_alerts": True,
                "notification_language": "zh",
                "long_symbol_tuning": {
                    "SOLUSDT": {
                        "effective_volume_ratio_min": 1.35,
                        "min_core_score": 3,
                        "require_4h_bullish": True,
                        "require_1h_above_vwap": True,
                    }
                },
            },
            daemon.default_symbol_state("SOLUSDT"),
        )

        self.assertNotIn("effective_long_breakout", [event["event_type"] for event in events])

    def test_retest_hold_short_requires_volume_and_trend_confirmation(self) -> None:
        result = {
            "symbol": "BTCUSDT",
            "price": 98.1,
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.7, 101.0],
                "short_ready_zone": [98.0, 98.3],
            },
            "long_short_plan": {
                "analysis_bias": "short_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.5,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.2,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.9, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "neutral",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": False, "short": True},
                "long_core_score": 0,
                "short_core_score": 2,
            },
            "timeframe_view": {"5m": {"volume_ratio": 1.0, "above_vwap": False, "trend": "bearish"}},
        }
        state = daemon.default_symbol_state("BTCUSDT")
        state["short_stage"] = "confirmed"

        events, _ = daemon.build_events(
            result,
            {"enable_trigger_alerts": True, "notification_language": "zh"},
            state,
        )

        self.assertNotIn("retest_hold_short", [event["event_type"] for event in events])

    def test_retest_hold_short_emits_when_volume_and_structure_confirm(self) -> None:
        result = {
            "symbol": "BTCUSDT",
            "price": 98.1,
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.7, 101.0],
                "short_ready_zone": [98.0, 98.3],
            },
            "long_short_plan": {
                "analysis_bias": "short_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.5,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.2,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.9, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "neutral",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": False, "short": True},
                "long_core_score": 0,
                "short_core_score": 2,
            },
            "timeframe_view": {"5m": {"volume_ratio": 1.15, "above_vwap": False, "trend": "bearish"}},
        }
        state = daemon.default_symbol_state("BTCUSDT")
        state["short_stage"] = "confirmed"

        events, _ = daemon.build_events(
            result,
            {"enable_trigger_alerts": True, "notification_language": "zh"},
            state,
        )

        retest = next(event for event in events if event["event_type"] == "retest_hold_short")
        self.assertIn("量比", retest["message"])

    def test_second_breakdown_short_includes_executor_plan_details(self) -> None:
        result = {
            "symbol": "BTCUSDT",
            "price": 97.4,
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.7, 101.0],
                "short_ready_zone": [98.0, 98.3],
            },
            "long_short_plan": {
                "analysis_bias": "short_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.5,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                    "executor_plan": {
                        "quality": "tradable",
                        "order_type": "stop_market",
                        "rr_to_tp1": 1.26,
                        "cancel_after_minutes": 90,
                        "notes": [],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.2,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.9, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                    "executor_plan": {
                        "quality": "tradable",
                        "order_type": "stop_market",
                        "rr_to_tp1": 1.26,
                        "cancel_after_minutes": 90,
                        "notes": [],
                    },
                },
            },
            "short_term_signal": {
                "bias": "short",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": False, "short": True},
                "long_core_score": 0,
                "short_core_score": 2,
            },
            "timeframe_view": {"5m": {"volume_ratio": 1.3, "above_vwap": False, "trend": "bearish"}},
        }
        state = daemon.default_symbol_state("BTCUSDT")
        state["short_stage"] = "retest"

        events, _ = daemon.build_events(
            result,
            {"enable_trigger_alerts": True, "notification_language": "en"},
            state,
        )

        event = next(event for event in events if event["event_type"] == "second_breakdown_short")
        self.assertIn("RR(TP1): 1.26", event["message"])
        self.assertIn("Cancel after: 90 minutes", event["message"])

    def test_second_breakout_long_is_suppressed_when_executor_quality_is_observe_only(self) -> None:
        result = {
            "symbol": "ETHUSDT",
            "price": 101.5,
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.7, 101.0],
                "short_ready_zone": [98.0, 98.3],
            },
            "long_short_plan": {
                "analysis_bias": "long_bias",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.5,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                    "executor_plan": {
                        "quality": "observe_only",
                        "order_type": "stop_market",
                        "rr_to_tp1": 0.72,
                        "cancel_after_minutes": 120,
                        "notes": ["RR too low"],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.2,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.9, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                    "executor_plan": {
                        "quality": "tradable",
                        "order_type": "stop_market",
                        "rr_to_tp1": 1.26,
                        "cancel_after_minutes": 90,
                        "notes": [],
                    },
                },
            },
            "short_term_signal": {
                "bias": "long",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": True, "short": False},
                "long_core_score": 2,
                "short_core_score": 0,
            },
            "timeframe_view": {"5m": {"volume_ratio": 1.35, "above_vwap": True, "trend": "bullish"}},
        }
        state = daemon.default_symbol_state("ETHUSDT")
        state["long_stage"] = "retest"

        events, _ = daemon.build_events(
            result,
            {"enable_trigger_alerts": True, "notification_language": "en"},
            state,
        )

        self.assertNotIn("second_breakout_long", [event["event_type"] for event in events])

    def test_unlock_stale_contexts_keeps_confirmed_long_setup_when_failure_only_drifts(self) -> None:
        result = {
            "symbol": "BTCUSDT",
            "actionable_levels": {"breakout_up": 101.8, "breakout_down": 97.5},
            "long_short_plan": {
                "long_setup": {
                    "trigger_price": 101.8,
                    "confirmation": {"retest_failure_level": 99.8},
                },
                "short_setup": {
                    "trigger_price": 97.5,
                    "confirmation": {"retest_failure_level": 98.4},
                },
            },
        }
        state = daemon.default_symbol_state("BTCUSDT")
        state["long_stage"] = "confirmed"
        state["long_context"] = {
            "captured_at": "2026-03-18T00:00:00+00:00",
            "trigger_price": 101.0,
            "failure_level": 99.0,
        }

        with patch.object(daemon, "utc_now", return_value=daemon.dt.datetime(2026, 3, 18, 0, 30, tzinfo=daemon.dt.timezone.utc)):
            updated = daemon.unlock_stale_contexts(result, state, lock_timeout_minutes=120, lock_drift_pct=1.0)

        self.assertEqual(updated["long_stage"], "confirmed")
        self.assertIsNotNone(updated["long_context"])

    def test_unlock_stale_contexts_resets_confirmed_long_setup_when_failure_drift_is_large(self) -> None:
        result = {
            "symbol": "BTCUSDT",
            "actionable_levels": {"breakout_up": 101.6, "breakout_down": 97.5},
            "long_short_plan": {
                "long_setup": {
                    "trigger_price": 101.6,
                    "confirmation": {"retest_failure_level": 101.2},
                },
                "short_setup": {
                    "trigger_price": 97.5,
                    "confirmation": {"retest_failure_level": 98.4},
                },
            },
        }
        state = daemon.default_symbol_state("BTCUSDT")
        state["long_stage"] = "confirmed"
        state["long_context"] = {
            "captured_at": "2026-03-18T00:00:00+00:00",
            "trigger_price": 101.0,
            "failure_level": 99.0,
        }

        with patch.object(daemon, "utc_now", return_value=daemon.dt.datetime(2026, 3, 18, 0, 30, tzinfo=daemon.dt.timezone.utc)):
            updated = daemon.unlock_stale_contexts(result, state, lock_timeout_minutes=120, lock_drift_pct=1.0)

        self.assertEqual(updated["long_stage"], "idle")
        self.assertIsNone(updated["long_context"])

    def test_unlock_stale_contexts_resets_touched_setup_when_trigger_drift_is_large(self) -> None:
        result = {
            "symbol": "BTCUSDT",
            "actionable_levels": {"breakout_up": 103.5, "breakout_down": 97.5},
            "long_short_plan": {
                "long_setup": {
                    "trigger_price": 103.5,
                    "confirmation": {"retest_failure_level": 99.8},
                },
                "short_setup": {
                    "trigger_price": 97.5,
                    "confirmation": {"retest_failure_level": 98.4},
                },
            },
        }
        state = daemon.default_symbol_state("BTCUSDT")
        state["long_stage"] = "touched"
        state["long_context"] = {
            "captured_at": "2026-03-18T00:00:00+00:00",
            "trigger_price": 101.0,
            "failure_level": 99.0,
        }

        with patch.object(daemon, "utc_now", return_value=daemon.dt.datetime(2026, 3, 18, 0, 30, tzinfo=daemon.dt.timezone.utc)):
            updated = daemon.unlock_stale_contexts(result, state, lock_timeout_minutes=120, lock_drift_pct=1.0)

        self.assertEqual(updated["long_stage"], "idle")
        self.assertIsNone(updated["long_context"])

    def test_build_events_emits_breakout_touch_only_on_first_idle_transition(self) -> None:
        result = {
            "symbol": "BTCUSDT",
            "price": 101.2,
            "news_summary": {"event_risk_level": "low"},
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.7, 101.0],
                "short_ready_zone": [98.0, 98.3],
            },
            "long_short_plan": {
                "analysis_bias": "neutral",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.5,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.0,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.8, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "neutral",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": True, "short": False},
                "long_core_score": 2,
                "short_core_score": 0,
            },
            "timeframe_view": {"5m": {"volume_ratio": 1.0}},
        }
        state = daemon.default_symbol_state("BTCUSDT")

        events_first, next_state = daemon.build_events(result, {"enable_trigger_alerts": True, "notification_language": "zh"}, state)
        events_second, _ = daemon.build_events(result, {"enable_trigger_alerts": True, "notification_language": "zh"}, next_state)

        self.assertIn("breakout_touch_up", [event["event_type"] for event in events_first])
        self.assertNotIn("breakout_touch_up", [event["event_type"] for event in events_second])

    def test_breakout_touch_up_message_is_probe_style(self) -> None:
        result = {
            "symbol": "ETHUSDT",
            "price": 101.2,
            "news_summary": {"event_risk_level": "low"},
            "actionable_levels": {
                "breakout_up": 101.0,
                "breakout_down": 98.0,
                "long_ready_zone": [100.7, 101.0],
                "short_ready_zone": [98.0, 98.3],
            },
            "long_short_plan": {
                "analysis_bias": "neutral",
                "long_setup": {
                    "trigger_price": 101.0,
                    "entry_zone": [101.0, 101.3],
                    "stop_loss": 99.0,
                    "take_profit": [102.0, 103.0],
                    "confirmation": {
                        "retest_zone": [100.6, 101.1],
                        "second_breakout_trigger": 101.4,
                        "retest_failure_level": 99.5,
                    },
                    "management": {
                        "breakeven_trigger": 102.0,
                        "breakeven_stop": 101.0,
                        "scale_out_zone": [102.5, 103.0],
                        "runner_zone": [103.0, 104.0],
                    },
                },
                "short_setup": {
                    "trigger_price": 98.0,
                    "entry_zone": [97.7, 98.0],
                    "stop_loss": 99.0,
                    "take_profit": [97.0, 96.0],
                    "confirmation": {
                        "retest_zone": [97.8, 98.3],
                        "second_breakout_trigger": 97.5,
                        "retest_failure_level": 98.8,
                    },
                    "management": {
                        "breakeven_trigger": 97.0,
                        "breakeven_stop": 98.0,
                        "scale_out_zone": [96.8, 96.2],
                        "runner_zone": [96.0, 95.0],
                    },
                },
            },
            "short_term_signal": {
                "bias": "neutral",
                "false_breakout": {"false_breakout_up": False, "false_breakout_down": False},
                "gate_open": {"long": True, "short": False},
                "long_core_score": 2,
                "short_core_score": 0,
            },
            "timeframe_view": {"5m": {"volume_ratio": 1.0, "above_vwap": True, "trend": "bullish", "rsi14": 58.0}},
        }
        events, _ = daemon.build_events(result, {"enable_trigger_alerts": True, "notification_language": "zh"}, daemon.default_symbol_state("ETHUSDT"))
        touch = next(event for event in events if event["event_type"] == "breakout_touch_up")
        self.assertIn("首破觀察", touch["message"])
        self.assertIn("關鍵價：101", touch["message"])
        self.assertIn("觀察提醒，不建倉", touch["message"])
        self.assertNotIn("建倉：", touch["message"])

    def test_build_macro_news_alert_emits_high_risk_notification_for_watchlist_symbols(self) -> None:
        rss_items = [
            {
                "title": "Ripple lawsuit escalates as market braces for Fed decision",
                "description": "XRP and broader crypto traders face hawkish inflation and rate hike risk.",
                "link": "https://example.com/xrp-fed",
            },
            {
                "title": "Solana outage adds risk-off pressure before key macro release",
                "description": "SOL traders react as crypto market turns defensive.",
                "link": "https://example.com/sol",
            }
        ]

        alert = daemon.build_macro_news_alert(rss_items, ["BTCUSDT", "XRPUSDT", "SOLUSDT"])

        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert["event_type"], "macro_news_high_risk")
        self.assertIn("宏觀消息提醒", alert["message"])
        self.assertIn("XRPUSDT", alert["message"])
        self.assertIn("SOLUSDT", alert["message"])

    def test_build_volatility_system_alert_emits_when_high_volatility_rule_is_active(self) -> None:
        alert = daemon.build_volatility_system_alert(
            {
                "symbol": "BTCUSDT",
                "price": 74200.0,
                "returns": {"5m": 2.15},
                "volatility": {"realized_24h": 5.1},
                "risk_level": "high",
                "short_term_signal": {"market_regime": "range_or_mixed"},
                "long_short_plan": {
                    "long_setup": {
                        "trigger_price": 74500.0,
                        "stop_loss": 73600.0,
                        "take_profit": [75800.0, 76400.0],
                    },
                    "short_setup": {
                        "trigger_price": 73800.0,
                        "stop_loss": 74600.0,
                        "take_profit": [72800.0, 72100.0],
                    },
                },
            },
            {
                "rules": [
                    {
                        "code": "high_volatility_pause",
                        "summary": "BTCUSDT 出現高波動，先暫停新倉。",
                    }
                ]
            },
            {"enable_volatility_alerts": True},
        )

        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert["event_type"], "high_volatility_alert")
        self.assertIn("大波動提醒", alert["message"])
        self.assertIn("5m 漲跌", alert["message"])
        self.assertIn("先停看", alert["message"])
        self.assertIn("上方劇本 轉強 74500", alert["message"])
        self.assertIn("下方劇本 轉弱 73800", alert["message"])

    def test_build_volatility_system_alert_returns_none_when_disabled(self) -> None:
        alert = daemon.build_volatility_system_alert(
            {
                "symbol": "BTCUSDT",
                "price": 74200.0,
                "returns": {"5m": 2.15},
                "volatility": {"realized_24h": 5.1},
                "risk_level": "high",
                "short_term_signal": {"market_regime": "range_or_mixed"},
            },
            {
                "rules": [
                    {
                        "code": "high_volatility_pause",
                        "summary": "BTCUSDT 出現高波動，先暫停新倉。",
                    }
                ]
            },
            {"enable_volatility_alerts": False},
        )

        self.assertIsNone(alert)

    def test_build_bull_trend_pullback_alert_emits_on_bull_pullback(self) -> None:
        alert = daemon.build_bull_trend_pullback_alert(
            {
                "symbol": "BTCUSDT",
                "price": 79000.0,
                "returns": {"15m": -1.4, "1h": -1.1},
                "short_term_signal": {"market_regime": "bull_trend", "bias": "long"},
                "actionable_levels": {
                    "breakout_down": 78000.0,
                    "price_map": {"primary_support": [78100.0, 78500.0]},
                },
                "long_short_plan": {
                    "long_setup": {"trigger_price": 79500.0, "stop_loss": 78050.0, "take_profit": [81200.0, 82000.0]},
                    "short_setup": {"stop_loss": 79250.0},
                },
            },
            {"summaries": [{"code": "countertrend_short_pause", "summary": "bull trend 保守看回撤"}]},
            {"enable_bull_pullback_alerts": True, "bull_pullback_15m_drop_pct": 1.0, "bull_pullback_1h_drop_pct": 0.8},
        )

        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert["event_type"], "bull_trend_pullback_alert")
        self.assertIn("牛趨勢回撤提醒", alert["message"])
        self.assertIn("15m 漲跌 -1.4%", alert["message"])
        self.assertIn("支撐區 78100 ~ 78500", alert["message"])

    def test_build_shock_15m_alert_emits_on_fast_drop(self) -> None:
        alert = daemon.build_shock_15m_alert(
            {
                "symbol": "ETHUSDT",
                "price": 2400.0,
                "returns": {"15m": -1.6, "1h": -0.9},
                "long_short_plan": {
                    "long_setup": {"trigger_price": 2427.0, "stop_loss": 2387.0},
                    "short_setup": {"trigger_price": 2387.0, "stop_loss": 2427.0},
                },
            },
            {"enable_15m_shock_alerts": True, "shock_15m_pct": 1.3},
        )

        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert["event_type"], "shock_15m_alert")
        self.assertIn("15m 急跌提醒", alert["message"])
        self.assertIn("15m 漲跌 -1.6%", alert["message"])

    def test_protection_layer_keeps_watch_event_but_blocks_actionable_long(self) -> None:
        events = [
            {"event_type": "approach_up", "message": "watch"},
            {"event_type": "effective_long_breakout", "message": "action"},
        ]

        filtered = daemon.apply_protection_layer_to_events(
            events,
            {
                "active": True,
                "pause_long": True,
                "pause_short": False,
                "hard_block": False,
                "summaries": ["BTCUSDT 處於 bear_trend，暫停逆勢做多確認訊號。"],
            },
        )

        self.assertEqual([event["event_type"] for event in filtered], ["approach_up"])
        self.assertIn("保護層", filtered[0]["message"])


if __name__ == "__main__":
    unittest.main()
