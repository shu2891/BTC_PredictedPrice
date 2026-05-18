import unittest

from event_types import (
    DEFAULT_ELIGIBLE_EVENTS,
    DEFAULT_EVENT_RISK_MULTIPLIERS,
    EVENT_DIRECTION_MAP,
    event_direction,
    event_risk_multiplier,
    event_role,
    is_actionable_event,
    is_watch_only_event,
)


class EventTypeTests(unittest.TestCase):
    def test_breakout_touch_events_have_consistent_mapping(self) -> None:
        self.assertEqual(EVENT_DIRECTION_MAP["breakout_touch_up"], "up")
        self.assertEqual(EVENT_DIRECTION_MAP["breakout_touch_down"], "down")
        self.assertEqual(event_direction("breakout_touch_up"), "up")
        self.assertEqual(event_direction("breakout_touch_down"), "down")

    def test_unknown_event_defaults_to_neutral(self) -> None:
        self.assertEqual(event_direction("unknown_event"), "neutral")

    def test_approach_events_are_watch_only(self) -> None:
        self.assertEqual(event_role("approach_up"), "watch")
        self.assertTrue(is_watch_only_event("approach_down"))
        self.assertFalse(is_actionable_event("approach_up"))

    def test_second_breakout_events_are_actionable(self) -> None:
        self.assertEqual(event_role("second_breakout_long"), "actionable")
        self.assertTrue(is_actionable_event("second_breakout_long"))
        self.assertFalse(is_watch_only_event("second_breakout_long"))

    def test_retest_hold_long_is_watch_only_not_actionable(self) -> None:
        self.assertEqual(event_role("retest_hold_long"), "watch")
        self.assertFalse(is_actionable_event("retest_hold_long"))
        self.assertTrue(is_watch_only_event("retest_hold_long"))

    def test_probe_and_retest_events_are_watch_only(self) -> None:
        self.assertEqual(event_role("breakout_touch_up"), "watch")
        self.assertEqual(event_role("breakout_touch_down"), "watch")
        self.assertEqual(event_role("retest_hold_short"), "watch")
        self.assertTrue(is_watch_only_event("breakout_touch_up"))
        self.assertFalse(is_actionable_event("retest_hold_short"))

    def test_eligible_events_have_explicit_risk_multipliers(self) -> None:
        self.assertNotIn("breakout_touch_up", DEFAULT_ELIGIBLE_EVENTS)
        self.assertNotIn("breakout_touch_down", DEFAULT_ELIGIBLE_EVENTS)
        self.assertEqual(
            set(DEFAULT_ELIGIBLE_EVENTS),
            {
                "effective_long_breakout",
                "effective_short_breakdown",
                "second_breakout_long",
                "second_breakdown_short",
            },
        )
        self.assertTrue(set(DEFAULT_ELIGIBLE_EVENTS).issubset(DEFAULT_EVENT_RISK_MULTIPLIERS))
        self.assertAlmostEqual(event_risk_multiplier("breakout_touch_up"), 0.35)
        self.assertAlmostEqual(event_risk_multiplier("unknown_event"), 1.0)


if __name__ == "__main__":
    unittest.main()
