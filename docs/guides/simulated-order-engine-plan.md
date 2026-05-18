# Simulated Order Engine Plan

## 目標

讓 [check_price](C:\Users\User\Desktop\Codex\check_price) 從「提醒與決策輔助」再往前一步，具備：

- 根據既有事件自動建立模擬掛單
- 價格到達指定條件時，自動模擬成交
- 依固定風報比或策略止損/目標，自動模擬出場
- 產生可回顧的模擬訂單、成交、持倉、績效資料

這一層只做 `paper trading / simulated orders`，不碰真實交易 API。

## 結論

可以做，而且很適合現在這個專案。

但不應該直接做成「全功能交易引擎」。最適合的第一版是：

- 使用現有的事件分層作為進場候選
- 建立一個獨立的 `simulated order engine`
- 先支援：
  - `pending -> filled -> closed/canceled`
  - 限價/停損/止盈
  - 固定 `RR = 3:1`
  - 超時取消

## 外部優秀案例

### 1. Freqtrade：Dry-run + simulated wallet

參考：

- [Freqtrade configuration: dry-run](https://www.freqtrade.io/en/stable/configuration/)
- [Freqtrade GitHub](https://github.com/freqtrade/freqtrade)

值得學的點：

- 有明確的 `dry_run` 模式與獨立資料庫
- 有模擬錢包 `dry_run_wallet`
- 模擬限價單、market order、timeout、stop loss
- 在官方文件中清楚定義 dry-run 的成交假設

對這個專案的啟發：

- `check_price` 也應該有獨立的模擬資金帳戶，不和 live 提醒資料混雜
- 要先寫清楚成交規則，不然回測與 live paper 結果會不一致

### 2. Hummingbot：Paper trade connector

參考：

- [Hummingbot paper trade](https://hummingbot.org/client/global-configs/paper-trade/)
- [Hummingbot GitHub](https://github.com/hummingbot/hummingbot)

值得學的點：

- paper trading 是獨立運行模式，不會誤送真單
- 可以維持和 live 幾乎相同的策略流程，只換成 paper exchange
- 有獨立 paper balance

對這個專案的啟發：

- 我們不需要先做真實交易所整合，先做一個「paper broker」抽象層就夠
- 事件判斷與通知流程可沿用，執行層則切給模擬 broker

### 3. vectorbt：Order records / portfolio simulation

參考：

- [vectorbt Portfolio base](https://vectorbt.dev/api/portfolio/base/)

值得學的點：

- 先定義 order records，再做分析
- 交易模擬分成：
  - `from_orders`
  - `from_signals`
  - `from_order_func`
- 分析重點在於：
  - orders
  - trades
  - positions
  - drawdowns

對這個專案的啟發：

- 模擬下單層的核心不是 UI，而是「訂單事件紀錄 schema」
- 要先把 `sim_orders / sim_fills / sim_positions / sim_trades` 做對

### 4. QuantConnect：Paper trading + order ticket

參考：

- [QuantConnect paper trading](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/quantconnect-paper-trading)
- [QuantConnect trading and orders](https://www.quantconnect.com/docs/v1/algorithm-reference/trading-and-orders)

值得學的點：

- live data + simulated fills
- 有 `OrderTicket` 來更新/取消限價單
- 明確區分：
  - Market
  - Limit
  - Stop Market
  - Stop Limit

對這個專案的啟發：

- 需要一個 `order ticket` 概念，而不是只記「是否買進」
- 要支援更新、取消、超時失效

## 對目前專案最適合的做法

### 不建議

- 直接把 [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py) 改成真實下單機器人
- 直接接交易所私有 API
- 一開始就做複雜的倉位金字塔、追蹤止損、部分成交模擬

### 建議

先做 `paper order engine v1`：

- 輸入：現有事件與分析結果
- 執行：建立模擬掛單
- 監控：用即時價格推進訂單狀態
- 輸出：模擬成交、持倉、績效

## 第一版功能範圍

### 進場來源

只允許這些事件建立模擬單：

- `breakout_touch_up`
- `breakout_touch_down`
- `effective_long_breakout`
- `effective_short_breakdown`
- `second_breakdown_short`
- `second_breakout_long`

以下事件不得直接開模擬單：

- `approach_up`
- `approach_down`
- `retest_hold_long`
- `retest_hold_short`

理由：

- 這和目前專案的資料結論一致
- `approach` 與 `retest_hold_long` 目前更像觀察訊號，不適合直接轉成 paper order

### 訂單型態

第一版只支援：

- `limit_entry`
- `stop_entry`
- `stop_loss`
- `take_profit`
- `time_cancel`

不做：

- trailing stop
- partial fill
- OCO 真實交易所映射
- 分批止盈

### 風報比

先支援兩種模式：

1. `fixed_rr`
   - 例如 `RR = 3:1`
   - 進場後由 stop distance 推出 TP
2. `plan_based`
   - 使用既有 `take_profit_1 / take_profit_2 / stop_loss`

你提的需求屬於第一種，適合先做。

### 決策時點

模擬引擎不直接生成訊號，只吃現有訊號。

建議流程：

1. [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py) 產生事件
2. 若事件屬於可交易層，且未被 protection 擋住
3. [paper_order_engine.py](C:\Users\User\Desktop\Codex\check_price\paper_order_engine.py) 根據規則建立 `pending order`
4. 當價格碰到：
   - 進場價 -> `filled`
   - 止損價 -> `stopped`
   - 目標價 -> `take_profit`
   - timeout -> `canceled`

## 建議資料結構

第一版可直接新增一個 SQLite：

- `paper_trading.db`

建議表：

### `sim_orders`

- `id`
- `created_at`
- `symbol`
- `event_id`
- `event_type`
- `side`
- `order_type`
- `status`
- `entry_price`
- `stop_loss`
- `take_profit`
- `risk_reward_ratio`
- `cancel_after_ts`
- `notes_json`

### `sim_fills`

- `id`
- `order_id`
- `fill_ts`
- `fill_price`
- `fill_reason`

### `sim_positions`

- `id`
- `order_id`
- `opened_at`
- `closed_at`
- `entry_price`
- `exit_price`
- `exit_reason`
- `pnl_pct`
- `mfe_pct`
- `mae_pct`

### `sim_equity_curve`

- `ts`
- `equity`
- `open_positions`
- `closed_trades`

## 與目前架構的整合方式

### 已有模組可直接重用

- [shadow_mode.py](C:\Users\User\Desktop\Codex\check_price\shadow_mode.py)
  - 提供進場價、止損、目標、結構資訊
- [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py)
  - 提供事件與狀態機
- [event_types.py](C:\Users\User\Desktop\Codex\check_price\event_types.py)
  - 提供事件角色
- [protections.py](C:\Users\User\Desktop\Codex\check_price\protections.py)
  - 提供風險濾網
- [analytics_pipeline.py](C:\Users\User\Desktop\Codex\check_price\analytics_pipeline.py)
  - 後續可擴充為模擬訂單分析層

### 建議新增模組

- [paper_order_engine.py](C:\Users\User\Desktop\Codex\check_price\paper_order_engine.py)
  - 單次或常駐執行
  - 管理模擬掛單與持倉
- [paper_order_report.py](C:\Users\User\Desktop\Codex\check_price\paper_order_report.py)
  - 輸出 paper trading 績效報告

## 第一版規則建議

### 做多

- 事件：`breakout_touch_up` 或 `effective_long_breakout`
- 進場：
  - `breakout_touch_up` 用 `stop_entry`
  - `effective_long_breakout` 可用 `limit_entry` 回踩或 `stop_entry`
- 止損：
  - 用現有 `failure_level` / `stop_loss`
- 止盈：
  - 若 `fixed_rr`，用 `entry + 3 * risk`

### 做空

- 事件：`effective_short_breakdown` 或 `second_breakdown_short`
- 進場：
  - `effective_short_breakdown` 直接 stop-entry
  - `second_breakdown_short` 保守掛 stop-entry
- 止損：
  - 用現有 `failure_level`
- 止盈：
  - 同樣採 `3:1`

## 第一版驗證方式

完成後應驗證：

1. 同一事件不重複開單
2. 保護層會阻擋模擬下單
3. timeout 會取消未成交掛單
4. 止損/止盈會正確關閉持倉
5. 報表能回答：
   - 哪種事件最值得模擬
   - `RR=3:1` 是否比現有 TP1/TP2 更適合

## 我對這個專案的建議

### 建議新增

- `paper_trading.db`
- `paper_order_engine.py`
- `paper_order_report.py`
- 模擬下單報告與 Telegram 摘要

### 建議維持

- 不碰真單
- 不動目前 live 提醒邏輯
- 先讓 paper order engine 只消費既有事件

### 建議刪除的想法

- 不要一開始就做「看到任何 signal 都模擬下單」
- 不要讓 `approach` 直接自動建倉

## 實作優先順序

1. 建立 `paper_trading.db` schema
2. 建立 `paper_order_engine.py`
3. 先支援 `breakout_touch_up / effective_short_breakdown / second_breakdown_short`
4. 支援 `RR = 3:1`
5. 輸出第一版 paper trading 報告
6. 再決定要不要擴到更多事件

## 決策

我認為：

- `可以發展`
- `而且值得做`
- 但要定位成：
  - `模擬下單 / paper trading`
  - `不是自動實盤下單`

對這個專案來說，最合理的下一步不是「直接交易」，而是：

> 先讓既有提醒系統，長出一層可驗證的模擬執行層。
