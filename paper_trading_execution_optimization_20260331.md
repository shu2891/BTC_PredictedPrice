# Paper Trading Execution Optimization 2026-03-31

## Scope

This note compares the current simulated order engine after adding:

- entry / exit fees
- entry / exit slippage
- max position guards
- same-side / symbol-level caps
- daily loss halt
- drawdown halt

The goal is to decide whether the execution layer is healthier than the original optimistic paper backtest and which defaults should remain active.

## Compared Runs

### 1. Baseline replay without costs or portfolio guards

Source:
- [paper_trading_backtest_2025-12-31_2026-04-01.json](C:\Users\User\Desktop\Codex\check_price\reports\paper_trading_backtest_2025-12-31_2026-04-01.json)

Headline:
- Orders: `465`
- Closed trades: `350`
- Win rate: `31.7%`
- Avg realized R: `+0.235`
- Latest equity: `21493.14`

Interpretation:
- Positive and attractive, but unrealistically optimistic because fills assume no costs.

### 2. Cost-aware replay with current defaults

Source:
- [paper_trading_backtest_2025-12-31_2026-04-01_cost_default.md](C:\Users\User\Desktop\Codex\check_price\paper_trading_backtest_2025-12-31_2026-04-01_cost_default.md)
- [paper_trading_backtest_2025-12-31_2026-04-01_cost_default.json](C:\Users\User\Desktop\Codex\check_price\reports\paper_trading_backtest_2025-12-31_2026-04-01_cost_default.json)

Original assumptions tested first:
- Fee: `5 bps` entry + `5 bps` exit
- Entry slippage: `5 bps`
- Stop slippage: `10 bps`
- Take-profit slippage: `5 bps`
- Portfolio guards available but disabled by default:
  - `max_open_positions = 99`
  - `max_same_side_positions = 99`
  - `max_symbol_positions = 99`
  - `daily_loss_limit_pct = 0`
  - `drawdown_halt_pct = 0`

Headline:
- Orders: `465`
- Closed trades: `350`
- Win rate: `30.9%`
- Avg gross realized R: `+0.152`
- Avg net realized R: `+0.098`
- Avg gross PnL %: `+0.266%`
- Avg net PnL %: `+0.166%`
- Total fee charged: `35.0%`
- Latest equity: `13338.46`

Interpretation:
- The edge survives realistic simple costs.
- The paper engine is now much less optimistic.
- The strategy still has positive expectancy, but the margin of safety is smaller than the original no-cost replay suggested.

### 3. Cost-aware replay with strict portfolio guards

Source:
- [paper_trading_backtest_2025-12-31_2026-04-01_cost_strict.md](C:\Users\User\Desktop\Codex\check_price\paper_trading_backtest_2025-12-31_2026-04-01_cost_strict.md)
- [paper_trading_backtest_2025-12-31_2026-04-01_cost_strict.json](C:\Users\User\Desktop\Codex\check_price\reports\paper_trading_backtest_2025-12-31_2026-04-01_cost_strict.json)

Assumptions:
- Same cost model as the default cost-aware replay
- Strict guards:
  - `max_open_positions = 3`
  - `max_same_side_positions = 2`
  - `max_symbol_positions = 1`
  - `daily_loss_limit_pct = 3`
  - `drawdown_halt_pct = 8`

Headline:
- Orders: `14`
- Closed trades: `12`
- Win rate: `16.7%`
- Avg gross realized R: `-0.402`
- Avg net realized R: `-0.456`
- Latest equity: `9454.02`
- Blocked reasons:
  - `drawdown_halt = 262`
  - `daily_loss_limit = 159`
  - `max_symbol_positions = 34`

Interpretation:
- The guard framework works technically.
- The selected thresholds are too strict for the current signal stream.
- These guard values suppress too much of the sample and make the replay negative.

### 4. Cost-aware replay with moderate research guards

Source:
- [paper_trading_backtest_2025-12-31_2026-04-01_caps_symbol2.md](C:\Users\User\Desktop\Codex\check_price\paper_trading_backtest_2025-12-31_2026-04-01_caps_symbol2.md)
- [paper_trading_backtest_2025-12-31_2026-04-01_caps_symbol2.json](C:\Users\User\Desktop\Codex\check_price\reports\paper_trading_backtest_2025-12-31_2026-04-01_caps_symbol2.json)

Assumptions:
- Same cost model as the default cost-aware replay
- Moderate caps:
  - `max_open_positions = 5`
  - `max_same_side_positions = 3`
  - `max_symbol_positions = 2`
  - `daily_loss_limit_pct = 0`
  - `drawdown_halt_pct = 0`

Headline:
- Orders: `231`
- Closed trades: `169`
- Win rate: `30.2%`
- Avg gross realized R: `+0.105`
- Avg net realized R: `+0.051`
- Latest equity: `10620.65`
- Blocked reasons:
  - `max_symbol_positions = 106`
  - `max_same_side_positions = 125`
  - `max_open_positions = 11`

Interpretation:
- This is currently the most balanced configuration tested.
- It keeps the replay positive after costs.
- It limits clustering without letting stop-trading rules suppress most of the sample.

## What Changed In Code

Main runtime / backtest changes:
- [paper_order_engine.py](C:\Users\User\Desktop\Codex\check_price\paper_order_engine.py)
- [paper_order_backtest.py](C:\Users\User\Desktop\Codex\check_price\paper_order_backtest.py)

Implemented:
- configurable fee model
- configurable adverse slippage model
- portfolio guard checks before order creation
- cost-aware reporting fields:
  - `gross_pnl_pct`
  - `net_pnl_pct`
  - `gross_realized_r`
  - `realized_r`
  - `entry_fee_pct`
  - `exit_fee_pct`
  - `account_return_pct`
- end-of-backtest forced closes now also use net accounting instead of gross-only accounting
- event-tier risk sizing:
  - `breakout_touch_up = 0.35x`
  - `breakout_touch_down = 0.50x`
  - `effective_long_breakout = 0.80x`
  - `effective_short_breakdown = 1.00x`
  - `second_breakout_long = 0.70x`
  - `second_breakdown_short = 1.00x`

## Decision

### 5. Cost-aware replay with moderate caps and event-tier risk sizing

Source:
- [paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.md](C:\Users\User\Desktop\Codex\check_price\paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.md)
- [paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.json](C:\Users\User\Desktop\Codex\check_price\reports\paper_trading_backtest_2025-12-31_2026-04-01_event_weighted.json)

Assumptions:
- Same cost model
- Moderate caps:
  - `max_open_positions = 5`
  - `max_same_side_positions = 3`
  - `max_symbol_positions = 2`
  - `daily_loss_limit_pct = 0`
  - `drawdown_halt_pct = 0`
- Event-tier risk sizing enabled

Headline:
- Orders: `200`
- Closed trades: `144`
- Win rate: `31.2%`
- Avg gross realized R: `+0.160`
- Avg net realized R: `+0.106`
- Latest equity: `11749.09`

Interpretation:
- This is better than using the same risk size for every event under the same cap profile.
- The biggest improvement comes from shrinking trial entries (`breakout_touch_*`) and preserving larger size for stronger confirmation events.
- This is the current best-tested paper default.

### Keep

- Keep fees and slippage enabled by default in paper replay and paper engine.
- Keep guard logic implemented.
- Keep current default posture of "costs on, moderate portfolio caps on, daily loss / drawdown halts off until calibrated, event-tier risk sizing on".

### Do Not Keep

- Do not use the strict guard thresholds from this experiment as runtime defaults.
- Do not judge strategy quality from the old no-cost replay anymore.

## Current Best Reading

- The strategy is still research-worthy after realistic simple costs.
- The strategy is not yet healthy enough for unattended real-money automation.
- The largest remaining execution gap is not "no costs"; it is "uncalibrated portfolio guard behavior".

## Next Recommended Step

1. Calibrate `daily_loss_limit_pct` and `drawdown_halt_pct` from replay distributions instead of fixed intuition.
2. Add symbol-aware cost presets, especially if lower-liquidity symbols continue to be traded in paper mode.
3. Use the new event-level expectancy table to demote or disable symbol/event combinations that stay net negative.
