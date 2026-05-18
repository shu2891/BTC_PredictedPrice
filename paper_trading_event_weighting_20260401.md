# Paper Trading Event Weighting 2026-04-01

## Scope

This note summarizes the first event-tier risk sizing pass for the paper trading engine.

The goal was simple:
- keep the same signal set
- keep the same cost model
- keep moderate portfolio caps
- reduce capital allocated to weaker trial-entry events
- preserve full size for the strongest confirmation events

## Applied Event Risk Multipliers

Implemented in [paper_order_engine.py](C:\Users\User\Desktop\Codex\check_price\paper_order_engine.py):

- `breakout_touch_up = 0.35x`
- `breakout_touch_down = 0.50x`
- `effective_long_breakout = 0.80x`
- `effective_short_breakdown = 1.00x`
- `second_breakout_long = 0.70x`
- `second_breakdown_short = 1.00x`

These multipliers are applied on top of the base `risk_pct`.

## Compared Configurations

### A. Moderate caps, flat risk per event

Source:
- [paper_trading_backtest_2025-12-31_2026-04-01_caps_symbol2.json](C:\Users\User\Desktop\Codex\check_price\reports\paper_trading_backtest_2025-12-31_2026-04-01_caps_symbol2.json)

Settings:
- `max_open_positions = 5`
- `max_same_side_positions = 3`
- `max_symbol_positions = 2`
- `daily_loss_limit_pct = 0`
- `drawdown_halt_pct = 0`
- all events used the same `risk_pct`

Result:
- Trades: `169`
- Win rate: `30.2%`
- Avg net realized R: `+0.051`
- Latest equity: `10620.65`

### B. Moderate caps, event-tier risk sizing

Source:
- [paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.json](C:\Users\User\Desktop\Codex\check_price\reports\paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.json)
- [paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.md](C:\Users\User\Desktop\Codex\check_price\paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.md)

Settings:
- same caps as configuration A
- same cost model
- event-tier risk sizing enabled

Result:
- Trades: `144`
- Win rate: `31.2%`
- Avg net realized R: `+0.106`
- Latest equity: `11749.09`

## What Improved

- Net expectancy improved from `+0.051R` to `+0.106R`.
- Final equity improved from `10620.65` to `11749.09`.
- The portfolio stayed more selective without needing harsh stop-trading halts.
- The paper report now exposes the main issue directly:
  - `breakout_touch_up` is still net negative
  - confirmed short events remain strongest
  - `effective_long_breakout` is positive enough to keep

## Event-Level Reading

From [paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.md](C:\Users\User\Desktop\Codex\check_price\paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.md):

- `second_breakdown_short`: best net expectancy
- `effective_short_breakdown`: strong
- `effective_long_breakout`: positive and worth keeping
- `breakout_touch_down`: mildly positive as a small-size trial layer
- `breakout_touch_up`: still net negative and should remain a reduced-risk trial layer

## Decision

Keep:
- moderate portfolio caps
- cost model
- event-tier risk sizing

Do not change yet:
- main signal generation
- daily loss halt
- drawdown halt

## Next Recommended Step

Use the new symbol/event expectancy table to demote specific bad combinations, for example:
- `BTCUSDT / breakout_touch_up`
- `SOLUSDT / breakout_touch_down`

That should be more effective than adding broader stop-trading rules right now.
