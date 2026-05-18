import tempfile
import unittest
from pathlib import Path

import paper_order_engine as engine


def sample_event(
    event_type: str = "breakout_touch_up",
    direction: str = "up",
    protections_json: str = '{"status":"normal","active":false}',
) -> dict:
    return {
        "id": 1,
        "event_key": "e1",
        "symbol": "BTCUSDT",
        "event_type": event_type,
        "direction": direction,
        "sent_at": "2026-03-31T00:00:00+00:00",
        "last_price": 100.0,
        "level": 100.0,
        "stop_loss": None,
        "protections_json": protections_json,
        "long_short_plan_json": (
            '{"analysis_bias":"long_bias","preferred_setup":"long","execution_readiness":"ready",'
            '"long_setup":{"trigger_price":101.0,"stop_loss":100.0,"take_profit":[104.0,107.0],'
            '"executor_plan":{"quality":"tradable","entry_trigger":101.0,"cancel_after_minutes":90,"order_type":"stop_market"}},'
            '"short_setup":{"trigger_price":99.0,"stop_loss":100.0,"take_profit":[96.0,93.0],'
            '"executor_plan":{"quality":"tradable","entry_trigger":99.0,"cancel_after_minutes":90,"order_type":"stop_market"}}}'
        ),
    }


class OrderBuildTests(unittest.TestCase):
    def test_builds_fixed_rr_order_from_breakout_touch_up(self) -> None:
        order = engine.build_sim_order_from_event(
            sample_event(),
            rr_ratio=3.0,
            risk_pct=1.0,
            default_cancel_after_minutes=360,
            mode="fixed_rr",
        )

        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order["side"], "long")
        self.assertEqual(order["order_type"], "stop_entry")
        self.assertEqual(order["entry_price"], 101.0)
        self.assertEqual(order["stop_loss"], 100.0)
        self.assertEqual(order["take_profit"], 104.0)

    def test_skips_order_when_protections_block_long(self) -> None:
        order = engine.build_sim_order_from_event(
            sample_event(protections_json='{"status":"guarded","active":true,"pause_long":true}'),
            rr_ratio=3.0,
            risk_pct=1.0,
            default_cancel_after_minutes=360,
            mode="fixed_rr",
        )
        self.assertIsNone(order)

    def test_applies_lower_risk_to_trial_entry_events(self) -> None:
        order = engine.build_sim_order_from_event(
            sample_event(event_type="breakout_touch_up"),
            rr_ratio=3.0,
            risk_pct=1.0,
            default_cancel_after_minutes=360,
            mode="fixed_rr",
            risk_pct_multipliers=engine.DEFAULT_EVENT_RISK_MULTIPLIERS,
        )
        assert order is not None
        self.assertAlmostEqual(float(order["risk_pct"]), 0.35, places=4)

    def test_keeps_full_risk_for_best_short_confirmation(self) -> None:
        order = engine.build_sim_order_from_event(
            sample_event(event_type="second_breakdown_short", direction="down"),
            rr_ratio=3.0,
            risk_pct=1.0,
            default_cancel_after_minutes=360,
            mode="fixed_rr",
            risk_pct_multipliers=engine.DEFAULT_EVENT_RISK_MULTIPLIERS,
        )
        assert order is not None
        self.assertAlmostEqual(float(order["risk_pct"]), 1.0, places=4)


class OrderLifecycleTests(unittest.TestCase):
    def test_fills_and_closes_long_position_and_updates_equity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = engine.ensure_paper_db(Path(tmpdir) / "paper.db")
            try:
                engine.ensure_initial_equity_snapshot(conn, 10000.0)
                order_id = engine.insert_sim_order(
                    conn,
                    {
                        "source_event_id": 1,
                        "source_event_key": "e1",
                        "symbol": "BTCUSDT",
                        "event_type": "breakout_touch_up",
                        "side": "long",
                        "order_type": "stop_entry",
                        "status": "pending",
                        "entry_price": 101.0,
                        "stop_loss": 100.0,
                        "take_profit": 104.0,
                        "risk_reward_ratio": 3.0,
                        "risk_pct": 1.0,
                        "created_at": "2026-03-31T00:00:00+00:00",
                        "cancel_after_ts": "2026-03-31T06:00:00+00:00",
                        "filled_at": None,
                        "closed_at": None,
                        "exit_price": None,
                        "exit_reason": None,
                        "last_price": 100.0,
                        "notes_json": "{}",
                    },
                )

                fill_now = engine.dt.datetime.fromisoformat("2026-03-31T00:30:00+00:00")
                close_now = engine.dt.datetime.fromisoformat("2026-03-31T01:30:00+00:00")
                stats = engine.process_pending_orders(conn, {"BTCUSDT": 101.2}, fill_now)
                self.assertEqual(stats["fills"], 1)
                position = conn.execute("SELECT * FROM sim_positions WHERE order_id = ?", (order_id,)).fetchone()
                self.assertIsNotNone(position)

                stats = engine.process_open_positions(conn, {"BTCUSDT": 104.5}, close_now, 10000.0)
                self.assertEqual(stats["closed"], 1)
                order = conn.execute("SELECT * FROM sim_orders WHERE id = ?", (order_id,)).fetchone()
                self.assertEqual(order["status"], "closed")
                self.assertEqual(order["exit_reason"], "take_profit")
                equity = conn.execute("SELECT equity FROM sim_equity_curve ORDER BY id DESC LIMIT 1").fetchone()
                self.assertAlmostEqual(float(equity["equity"]), 10300.0, places=3)
            finally:
                conn.close()

    def test_fees_reduce_realized_r(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = engine.ensure_paper_db(Path(tmpdir) / "paper.db")
            try:
                engine.ensure_initial_equity_snapshot(conn, 10000.0)
                order_id = engine.insert_sim_order(
                    conn,
                    {
                        "source_event_id": 3,
                        "source_event_key": "e3",
                        "symbol": "BTCUSDT",
                        "event_type": "breakout_touch_up",
                        "side": "long",
                        "order_type": "stop_entry",
                        "status": "pending",
                        "entry_price": 101.0,
                        "stop_loss": 100.0,
                        "take_profit": 104.0,
                        "risk_reward_ratio": 3.0,
                        "risk_pct": 1.0,
                        "created_at": "2026-03-31T00:00:00+00:00",
                        "cancel_after_ts": "2026-03-31T06:00:00+00:00",
                        "filled_at": None,
                        "closed_at": None,
                        "exit_price": None,
                        "exit_reason": None,
                        "last_price": 100.0,
                        "notes_json": "{}",
                    },
                )
                fill_now = engine.dt.datetime.fromisoformat("2026-03-31T00:30:00+00:00")
                close_now = engine.dt.datetime.fromisoformat("2026-03-31T01:30:00+00:00")
                engine.process_pending_orders(conn, {"BTCUSDT": 101.2}, fill_now, entry_slippage_bps=5.0)
                engine.process_open_positions(
                    conn,
                    {"BTCUSDT": 104.5},
                    close_now,
                    10000.0,
                    fee_bps=5.0,
                    stop_slippage_bps=10.0,
                    take_profit_slippage_bps=5.0,
                )
                position = conn.execute("SELECT * FROM sim_positions WHERE order_id = ?", (order_id,)).fetchone()
                self.assertLess(float(position["realized_r"]), 3.0)
                self.assertLess(float(position["pnl_pct"]), float(position["gross_pnl_pct"]))
            finally:
                conn.close()

    def test_cancels_pending_order_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = engine.ensure_paper_db(Path(tmpdir) / "paper.db")
            try:
                engine.ensure_initial_equity_snapshot(conn, 10000.0)
                engine.insert_sim_order(
                    conn,
                    {
                        "source_event_id": 2,
                        "source_event_key": "e2",
                        "symbol": "ETHUSDT",
                        "event_type": "breakout_touch_up",
                        "side": "long",
                        "order_type": "stop_entry",
                        "status": "pending",
                        "entry_price": 200.0,
                        "stop_loss": 195.0,
                        "take_profit": 215.0,
                        "risk_reward_ratio": 3.0,
                        "risk_pct": 1.0,
                        "created_at": "2026-03-31T00:00:00+00:00",
                        "cancel_after_ts": "2026-03-31T00:01:00+00:00",
                        "filled_at": None,
                        "closed_at": None,
                        "exit_price": None,
                        "exit_reason": None,
                        "last_price": 199.0,
                        "notes_json": "{}",
                    },
                )

                cancel_now = engine.dt.datetime.fromisoformat("2026-03-31T01:00:00+00:00")
                stats = engine.process_pending_orders(
                    conn,
                    {"ETHUSDT": 198.0},
                    cancel_now,
                )
                self.assertEqual(stats["cancels"], 1)
                order = conn.execute("SELECT * FROM sim_orders WHERE source_event_id = 2").fetchone()
                self.assertEqual(order["status"], "canceled")
            finally:
                conn.close()


class GuardTests(unittest.TestCase):
    def test_blocks_new_order_when_max_open_positions_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = engine.ensure_paper_db(Path(tmpdir) / "paper.db")
            try:
                engine.ensure_initial_equity_snapshot(conn, 10000.0)
                now = engine.dt.datetime.fromisoformat("2026-03-31T00:30:00+00:00")
                conn.execute(
                    """
                    INSERT INTO sim_positions (
                        order_id, symbol, side, status, opened_at, entry_price, stop_loss, take_profit,
                        last_price, risk_pct, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (99, "BTCUSDT", "long", "open", now.isoformat(), 100.0, 99.0, 103.0, 100.0, 1.0, now.isoformat()),
                )
                conn.commit()
                allowed, reason = engine.can_create_order(
                    conn,
                    side="long",
                    symbol="ETHUSDT",
                    now=now,
                    starting_equity=10000.0,
                    max_open_positions=1,
                    max_same_side_positions=2,
                    max_symbol_positions=1,
                    daily_loss_limit_pct=3.0,
                    drawdown_halt_pct=8.0,
                )
                self.assertFalse(allowed)
                self.assertEqual(reason, "max_open_positions")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
