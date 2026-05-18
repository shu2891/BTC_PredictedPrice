# Telegram Notification Audit

- Data source: `alert_state_pi_snapshot_20260319.db`
- Notification window: `2026-03-15T20:37:56.080419+00:00` ~ `2026-03-18T14:28:29.736275+00:00`
- Total recorded Telegram alerts: `180`
- Watch-only alerts: `161`
- Setup alerts: `4`
- Actionable alerts: `15`

## What You Actually Received Most
- `approach_up`: `93`
- `approach_down`: `68`
- `retest_hold_short`: `7`
- `breakout_touch_down`: `4`
- `effective_short_breakdown`: `4`
- `second_breakdown_short`: `4`

## Were Up/Down Observation Alerts Included?
- Yes. `approach_up` and `approach_down` were both written into `alert_events` and backfilled into `alert_event_performance`.
- `approach_up`: `93` alerts
- `approach_down`: `68` alerts

## Accuracy Summary By Event Type
| Event | Role | Count | 15m | 1h | 4h | 24h | Reading |
|---|---|---:|---|---|---|---|---|
| approach_up | watch | 93 | 20% / -0.23% | 16% / -0.54% | 24% / -0.65% | 3% / -4.56% | Watch-only; poor as entry signal |
| approach_down | watch | 68 | 37% / -0.07% | 37% / -0.07% | 20% / -0.08% | 100% / +5.08% | Watch-only; poor as entry signal |
| retest_hold_short | actionable | 7 | 57% / +0.03% | 57% / +0.83% | 83% / +2.59% | n/a | Best current trading candidate |
| breakout_touch_down | setup | 4 | 25% / -0.23% | 25% / +0.19% | 75% / +1.53% | n/a | Early setup; still too early to trust blindly |
| effective_short_breakdown | actionable | 4 | 50% / +0.04% | 100% / +1.31% | 100% / +3.90% | n/a | Best current trading candidate |
| second_breakdown_short | actionable | 4 | 75% / +0.54% | 100% / +1.28% | 100% / +4.61% | n/a | Best current trading candidate |

## Practical Takeaways
- Most Telegram volume came from `approach_up/down`; these were included in the score report, but they behave more like watchlist pings than entry signals.
- In this live sample, the useful edge is concentrated on the short-side actionable chain: `effective_short_breakdown`, `retest_hold_short`, `second_breakdown_short`.
- `approach_up` was the weakest recurring alert in this batch. It fired often and performed poorly across 1h / 4h / 24h.
- `breakout_touch_down` improved later on 4h, but is still early-stage. It should stay as a setup alert, not an execution alert.
- 24h actionable data is still too immature in live tracking, so the most reliable read right now is 1h / 4h.

## What I Would Trust Right Now
- Prefer `effective_short_breakdown`
- Prefer `retest_hold_short`
- Prefer `second_breakdown_short`

## What I Would Not Trade Directly
- Do not enter directly from `approach_down`
- Do not enter directly from `approach_up`
