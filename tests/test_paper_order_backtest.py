import tempfile
import unittest
from pathlib import Path

import paper_order_backtest as backtest
import paper_order_engine as engine


class EndOfBacktestCostTests(unittest.TestCase):
    def test_close_remaining_positions_applies_costs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = engine.ensure_paper_db(Path(tmpdir) / "paper.db")
            try:
                engine.ensure_initial_equity_snapshot(conn, 10_000.0)
                order_id = engine.insert_sim_order(
                    conn,
                    {
                        "source_event_id": 10,
                        "source_event_key": "end-1",
                        "symbol": "BTCUSDT",
                        "event_type": "effective_long_breakout",
                        "side": "long",
                        "order_type": "stop_entry",
                        "status": "filled",
                        "entry_price": 100.0,
                        "stop_loss": 95.0,
                        "take_profit": 115.0,
                        "risk_reward_ratio": 3.0,
                        "risk_pct": 1.0,
                        "created_at": "2026-03-31T00:00:00+00:00",
                        "cancel_after_ts": "2026-03-31T06:00:00+00:00",
                        "filled_at": "2026-03-31T00:05:00+00:00",
                        "closed_at": None,
                        "exit_price": None,
                        "exit_reason": None,
                        "last_price": 100.0,
                        "notes_json": "{}",
                    },
                )
                opened_at = engine.dt.datetime.fromisoformat("2026-03-31T00:05:00+00:00")
                order = conn.execute("SELECT * FROM sim_orders WHERE id = ?", (order_id,)).fetchone()
                engine._open_position(conn, order, opened_at, 100.0)
                conn.execute(
                    "UPDATE sim_positions SET last_price = ?, updated_at = ? WHERE order_id = ?",
                    (103.0, "2026-03-31T01:00:00+00:00", order_id),
                )
                conn.commit()

                closed = backtest.close_remaining_positions(
                    conn,
                    int(engine.dt.datetime.fromisoformat("2026-03-31T01:00:00+00:00").timestamp() * 1000),
                    10_000.0,
                    fee_bps=5.0,
                    take_profit_slippage_bps=5.0,
                )
                self.assertEqual(closed, 1)
                position = conn.execute("SELECT * FROM sim_positions WHERE order_id = ?", (order_id,)).fetchone()
                self.assertAlmostEqual(float(position["gross_realized_r"]), 0.59, places=2)
                self.assertLess(float(position["realized_r"]), float(position["gross_realized_r"]))
                self.assertLess(float(position["pnl_pct"]), float(position["gross_pnl_pct"]))
                equity = conn.execute("SELECT equity FROM sim_equity_curve ORDER BY id DESC LIMIT 1").fetchone()
                self.assertLess(float(equity["equity"]), 10_059.0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
