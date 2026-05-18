from __future__ import annotations

EVENT_DIRECTION_MAP: dict[str, str] = {
    "approach_up": "up",
    "breakout_touch_up": "up",
    "effective_long_breakout": "up",
    "retest_hold_long": "up",
    "second_breakout_long": "up",
    "approach_down": "down",
    "breakout_touch_down": "down",
    "effective_short_breakdown": "down",
    "retest_hold_short": "down",
    "second_breakdown_short": "down",
}

EVENT_ROLE_MAP: dict[str, str] = {
    "approach_up": "watch",
    "approach_down": "watch",
    "breakout_touch_up": "watch",
    "breakout_touch_down": "watch",
    "effective_long_breakout": "actionable",
    "effective_short_breakdown": "actionable",
    "retest_hold_long": "watch",
    "retest_hold_short": "watch",
    "second_breakout_long": "actionable",
    "second_breakdown_short": "actionable",
}

DEFAULT_ELIGIBLE_EVENTS: tuple[str, ...] = (
    "effective_long_breakout",
    "effective_short_breakdown",
    "second_breakout_long",
    "second_breakdown_short",
)

DEFAULT_EVENT_RISK_MULTIPLIERS: dict[str, float] = {
    "breakout_touch_up": 0.35,
    "breakout_touch_down": 0.50,
    "effective_long_breakout": 0.80,
    "effective_short_breakdown": 1.00,
    "second_breakout_long": 0.70,
    "second_breakdown_short": 1.00,
}


def event_direction(event_type: str) -> str:
    return EVENT_DIRECTION_MAP.get(event_type, "neutral")


def event_role(event_type: str) -> str:
    return EVENT_ROLE_MAP.get(event_type, "other")


def is_watch_only_event(event_type: str) -> bool:
    return event_role(event_type) == "watch"


def is_actionable_event(event_type: str) -> bool:
    return event_role(event_type) == "actionable"


def event_risk_multiplier(event_type: str) -> float:
    return float(DEFAULT_EVENT_RISK_MULTIPLIERS.get(event_type, 1.0))
