# Paper Trading Health And Execution Analysis

## Scope

This document evaluates whether the current `check_price` simulated order engine is healthy enough for continued paper trading and what must still be added before any real-money automation should be considered.

It is based on:
- Current workspace runtime behavior
- The historical paper trading backtest output from [paper_trading_backtest_2025-12-31_2026-04-01.md](C:\Users\User\Desktop\Codex\check_price\paper_trading_backtest_2025-12-31_2026-04-01.md)
- External reference implementations and official documentation

## Current Status

### Current Result Snapshot

- Three-month paper backtest range: `2025-12-31` to `2026-04-01` UTC
- Total simulated orders: `465`
- Closed trades: `350`
- Canceled trades: `115`
- Win rate: `31.7%`
- Average realized R: `+0.235`
- Latest equity from `10000` start: `21493.14`

### Current Interpretation

- The strategy is **still positive expectancy** under a cost-aware `3:1` paper trading setup when portfolio guards are left effectively off until calibrated.
- The strategy is **not yet healthy enough for real-money automation**.
- The result is still heavily influenced by `breakout_touch_*` events, which are currently closer to trial-entry signals than fully validated execution signals.
- The paper engine is now useful for forward testing and filtering bad ideas, but its portfolio guard defaults still require calibration before they should be treated as production-ready.

## Health Checklist

Status markers:
- `PASS`: already implemented and usable
- `PARTIAL`: present but incomplete
- `FAIL`: missing or materially unrealistic

| Area | Status | Current State | Why It Matters |
|---|---|---|---|
| Shared strategy logic between alerts and paper trading | `PASS` | [paper_order_engine.py](C:\Users\User\Desktop\Codex\check_price\paper_order_engine.py) uses the same event payloads and long/short plans as runtime alerts | Prevents research/runtime drift |
| Event-tier position sizing | `PASS` | Paper execution now assigns smaller risk to trial-entry events and fuller risk to stronger confirmations | Prevents weak setup layers from dominating portfolio risk |
| Dedicated paper trading database | `PASS` | Simulated orders are isolated in `paper_trading.db` or a dedicated backtest DB | Avoids mixing live telemetry with simulated fills |
| Fixed R-based order construction | `PASS` | Supports fixed `RR=3:1` and `plan_based` take profit mode | Gives a deterministic baseline |
| Pending/fill/close lifecycle | `PASS` | `pending -> filled -> closed/canceled` is modeled | Required for meaningful paper testing |
| Timeout cancellation | `PASS` | Orders cancel after `cancel_after_minutes` | Prevents stale setups from lingering forever |
| Historical OHLC-based execution replay | `PASS` | [paper_order_backtest.py](C:\Users\User\Desktop\Codex\check_price\paper_order_backtest.py) fills orders from historical candles | Makes multi-month replay possible |
| Fee model | `PASS` | Entry and exit fees are modeled in basis points and applied to net PnL / realized R | Gross edge can now be compared against net edge |
| Slippage model | `PASS` | Entry, stop, and take-profit exits each have configurable adverse slippage assumptions | Makes fills less optimistic |
| Spread-aware fills | `PARTIAL` | A simple adverse slippage model exists, but there is still no bid/ask or symbol-specific spread model | Lower-liquidity symbols can still look too optimistic |
| Max open positions | `PASS` | Portfolio cap exists in paper engine and backtest, and current research defaults are moderate rather than fully disabled | Guard behavior can now be tested explicitly |
| Same-side exposure cap | `PASS` | Same-side and symbol-level caps exist in paper engine and backtest | Prevents unlimited clustering when enabled |
| Daily/global drawdown guard | `PASS` | Daily loss and drawdown halts exist, but strict values tested so far were too restrictive | Needed before any automation can be trusted |
| Symbol-level stoploss guard | `FAIL` | No pause after repeated losses for one symbol/event class | Mature frameworks do this explicitly |
| Session restart resilience | `PARTIAL` | DB persists orders, but no daemon/service wrapper for paper engine yet | Important for continuous paper forward-testing |
| Event-quality segmentation in paper reports | `PARTIAL` | Event-type counts are tracked, but no separate expectancy table per event type yet | Needed to know which events deserve capital |
| Cost-aware performance reporting | `PASS` | Reports now show gross and net PnL / realized R plus total fees | Gross edge can now be separated from net edge |

## External Reference Patterns Worth Copying

### 1. Freqtrade: Dry-run should simulate order behavior, not just count signals

Relevant references:
- [Freqtrade Introduction](https://docs.freqtrade.io/en/stable/)
- [Freqtrade Configuration: dry-run behavior](https://www.freqtrade.io/en/stable/configuration/)
- [Freqtrade Protections](https://www.freqtrade.io/en/2022.12/includes/protections/)

What matters:
- Freqtrade explicitly recommends starting in dry-run before real money.
- Dry-run simulates wallets, orders, unfilled timeouts, stoploss assumptions, and order behavior.
- Freqtrade also separates dry-run state from production state and recommends different databases.
- Protection plugins like `StoplossGuard` and `MaxDrawdown` are treated as first-class runtime controls, not optional afterthoughts.

What `check_price` should copy:
- Add fee and slippage modeling to the paper engine.
- Add `stoploss guard`, `max drawdown`, and `max concurrent trades` to paper execution, not just alert generation.
- Keep paper DB separate from live alert DB.

### 2. Hummingbot: Paper trading is useful because it keeps live control flow but removes capital risk

Relevant reference:
- [Hummingbot Paper Trade](https://hummingbot.org/client/global-configs/paper-trade/)

What matters:
- Hummingbot exposes paper connectors as a separate execution mode instead of changing strategy semantics.
- Users can test the bot without risking real assets.
- Paper mode is operationally close to live usage.

What `check_price` should copy:
- Keep paper trading as a distinct execution mode, not a one-off script only.
- Eventually run the paper engine continuously alongside live alerts, so paper results reflect what the user would have done in real time.

### 3. QuantConnect LEAN: Paper trading should be the bridge between backtest and real deployment

Relevant references:
- [QuantConnect Paper Trading](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/quantconnect-paper-trading)
- [QuantConnect Risk Management Key Concepts](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/risk-management/key-concepts?ref=v1)

What matters:
- QuantConnect positions paper trading as a way to ensure the backtest was not overfit before risking money.
- The platform models order types, account types, fills, fees, margin, settlements, and risk management as explicit components.
- Risk management is modular and composable.

What `check_price` should copy:
- Treat paper trading as the mandatory gate between replay and any live automation.
- Add modular paper-only risk controls:
  - max drawdown halt
  - trailing equity stop
  - per-position risk cap
  - daily loss stop

### 4. Jesse: One code path should cover backtest, paper, and live semantics as much as possible

Relevant references:
- [Jesse GitHub README](https://github.com/jesse-ai/jesse)
- [Jesse product overview](https://jesse.trade/)

What matters:
- Jesse emphasizes a unified framework for backtesting, optimizing, and live trading.
- It highlights smart ordering, metrics, multiple symbols/timeframes, and built-in risk helpers.

What `check_price` should copy:
- Avoid building a separate paper-only strategy language.
- Keep event generation shared; only execution and fill assumptions should differ by mode.
- Add better metrics around order quality, not just raw PnL.

## What The Current Project Should Not Copy Yet

- Do **not** add a heavy exchange abstraction layer yet.
- Do **not** move to a full portfolio optimizer yet.
- Do **not** introduce real brokerage APIs before paper execution includes costs and risk guards.
- Do **not** add machine learning to the order engine before execution realism is acceptable.

## Recommended Execution Plan For `check_price`

### Phase 1: Make paper results less optimistic

Status: `completed`

Required:
- Add fee modeling for entry and exit.
- Add a simple slippage model:
  - market/stop entries: penalty in basis points
  - stop losses: larger penalty than profit targets
- Add spread-aware adverse fill assumptions for `breakout_touch_*`.

Observed outcome:
- The project now knows the positive expectancy survives a simple cost model, but the edge is much smaller.

### Phase 2: Add portfolio-level risk controls

Status: `implemented, not calibrated`

Required:
- Add `max_open_positions`.
- Add `max_same_side_positions`.
- Add `max_positions_per_symbol`.
- Add `daily_loss_limit_pct`.
- Add `equity_drawdown_halt_pct`.

Observed outcome:
- The guard framework works, but early strict settings (`3%` daily stop / `8%` drawdown halt) suppressed too many orders and turned replay negative.
- Current default posture is to keep only the portfolio caps enabled at research-safe defaults and continue calibrating harder stop-trading rules with more samples.

### Phase 3: Separate signal quality from execution quality

Status: `partially completed`

Required:
- Extend paper reports with event-level expectancy tables:
  - `breakout_touch_up`
  - `effective_long_breakout`
  - `effective_short_breakdown`
  - `second_breakdown_short`
- Report gross vs. net performance after fees/slippage.
- Report expectancy per event type, not just aggregate totals.

Observed outcome:
- Aggregate reports now include cost-aware metrics.
- The next missing piece is a stable event-level net expectancy table that is first-class in the paper trading report, not just inferred from other analytics outputs.

### Phase 4: Run continuous forward paper trading

Status: `not started`

Required:
- Run the paper engine as a service using current live alerts.
- Produce a rolling forward-test report.
- Compare:
  - historical replay paper results
  - live forward paper results

Expected outcome:
- The project can detect whether historical replay edge survives real-time operation.

## Concrete Decision For The Current Version

Current decision:
- Continue using the paper engine for research and forward simulation.
- Keep fees and slippage enabled by default.
- Keep position and drawdown guards configurable, with current research defaults set to:
  - `max_open_positions = 5`
  - `max_same_side_positions = 3`
  - `max_symbol_positions = 2`
  - `daily_loss_limit_pct = 0`
  - `drawdown_halt_pct = 0`
- Do **not** use it for real-money automation yet.

Reason:
- The strategy still has a positive paper-trading baseline after fees and slippage.
- But strict guard settings can easily turn the replay negative, which means portfolio controls are not yet calibrated well enough for unattended automation.
- The current research defaults are:
  - `max_open_positions = 5`
  - `max_same_side_positions = 3`
  - `max_symbol_positions = 2`
  - `daily_loss_limit_pct = 0`
  - `drawdown_halt_pct = 0`
- The current paper sizing defaults are:
  - `breakout_touch_up = 0.35x`
  - `breakout_touch_down = 0.50x`
  - `effective_long_breakout = 0.80x`
  - `effective_short_breakdown = 1.00x`
  - `second_breakout_long = 0.70x`
  - `second_breakdown_short = 1.00x`

## Immediate Next Steps

1. Add symbol-aware fee and slippage presets instead of one global basis-point assumption.
2. Calibrate `max_open_positions`, `max_same_side_positions`, `daily_loss_limit_pct`, and `drawdown_halt_pct` from replay data instead of intuition.
3. Extend the backtest report to show net expectancy per event type.
4. Run a rolling forward paper-trading service, then compare live forward paper results against replay assumptions.
