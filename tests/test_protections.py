import datetime as dt
import sqlite3
import unittest

import protections


class MarketProtectionTests(unittest.TestCase):
    def test_high_volatility_pause_hard_blocks_new_entries(self) -> None:
        result = protections.evaluate_market_protections(
            symbol="BTCUSDT",
            returns={"5m": 2.1},
            volatility={"realized_24h": 2.0},
            short_term_signal={"market_regime": "range_or_mixed"},
            risk_level="medium",
        )

        self.assertTrue(result["hard_block"])
        self.assertTrue(result["pause_long"])
        self.assertTrue(result["pause_short"])
        self.assertTrue(result["blocks_new_entries"])

    def test_countertrend_pause_blocks_only_opposite_side(self) -> None:
        result = protections.evaluate_market_protections(
            symbol="ETHUSDT",
            returns={"5m": 0.3},
            volatility={"realized_24h": 1.8},
            short_term_signal={"market_regime": "bear_trend"},
            risk_level="low",
        )

        self.assertTrue(result["pause_long"])
        self.assertFalse(result["pause_short"])
        self.assertFalse(result["hard_block"])


class PerformanceProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                event_type TEXT NOT NULL,
                direction TEXT NOT NULL,
                sent_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE alert_event_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                horizon_label TEXT NOT NULL,
                close_return_pct REAL NOT NULL,
                window_end TEXT NOT NULL
            )
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_row(self, event_type: str, direction: str, close_return_pct: float, hours_ago: int) -> None:
        sent_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago + 1)).isoformat()
        window_end = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).isoformat()
        cur = self.conn.execute(
            "INSERT INTO alert_events (symbol, event_type, direction, sent_at) VALUES (?, ?, ?, ?)",
            ("BTCUSDT", event_type, direction, sent_at),
        )
        event_id = cur.lastrowid
        self.conn.execute(
            """
            INSERT INTO alert_event_performance (event_id, horizon_label, close_return_pct, window_end)
            VALUES (?, ?, ?, ?)
            """,
            (event_id, "1h", close_return_pct, window_end),
        )
        self.conn.commit()

    def test_loss_streak_cooldown_pauses_direction(self) -> None:
        self._insert_row("effective_long_breakout", "up", -0.5, 1)
        self._insert_row("effective_long_breakout", "up", -0.8, 2)
        self._insert_row("second_breakout_long", "up", -0.3, 3)

        result = protections.evaluate_performance_protections(
            conn=self.conn,
            symbol="BTCUSDT",
            config={
                "loss_streak_cooldown": {
                    "enabled": True,
                    "horizon_label": "1h",
                    "loss_streak_count": 3,
                    "cooldown_hours": 6,
                    "lookback_rows": 12,
                }
            },
        )

        self.assertTrue(result["pause_long"])
        self.assertFalse(result["pause_short"])
        self.assertTrue(result["active"])


if __name__ == "__main__":
    unittest.main()
