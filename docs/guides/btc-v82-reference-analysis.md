# BTC v8.2 參考策略分析

## 目的

本文件分析 [BTC_live_v8.2.py](C:\Users\User\Desktop\Codex\Stock_gemini\BTC_live_v8.2.py) 與其底層 [BTC_live_v8.1.py](C:\Users\User\Desktop\Codex\Stock_gemini\BTC_live_v8.1.py) 的設計，整理哪些做法適合導入 `check_price`，哪些不適合直接複製。

本文件只根據現有程式碼事實與目前 `check_price` 架構做比較，不把 `BTC v8.2` 視為完整替代方案。

## 先講結論

`BTC v8.2` 值得參考的不是「直接下單機器人」這件事，而是它把單一策略壓縮成幾個非常明確的模塊：

- 多週期方向分數
- 進場時間過濾
- 資金費率情緒過濾
- 固定 R 倍數停利與保本管理

對 `check_price` 最值得借的，是「讓執行層更簡潔、更明確」；不值得直接搬的是「單一商品、單一交易所、直接實盤下單」。

## BTC v8.2 / v8.1 的實際做法

### 1. 單一標的、單一執行場景

根據 [BTC_live_v8.1.py](C:\Users\User\Desktop\Codex\Stock_gemini\BTC_live_v8.1.py) 與 [BTC_live_v8.2.py](C:\Users\User\Desktop\Codex\Stock_gemini\BTC_live_v8.2.py)：

- 標的固定為 BTC futures
- 交易所固定為 Binance Futures
- 直接連交易所 API 下單
- 用本地 `json` 檔保存單筆交易狀態

這代表它是一個「單策略、單標的、直接實盤執行 bot」，不是分析平台。

### 2. 多週期方向分數非常簡潔

`StrategyV8.get_mtf_score()` 使用：

- `4h` 權重 `0.5`
- `1h` 權重 `0.3`
- `15m` 權重 `0.2`

分數來自：

- `MA20 / MA50` 相對位置
- 收盤價是否站在 `MA20`
- `RSI`
- 5 根報酬動能

結果壓成單一 `0-100` 分數，再用門檻決定：

- `>= MTF_LONG_THRESHOLD` 才考慮 long
- `< MTF_SHORT_THRESHOLD` 才考慮 short

這是它最有價值的特點之一：方向判定非常清楚，不用過多事件分層。

### 3. 進場規則其實很保守

進場不是單靠分數，還要求：

- `ADX` 達門檻
- `MA20 > MA50 且 close > MA20` 才做多
- `MA20 < MA50 且 close < MA20` 才做空

也就是說，它的進場條件是：

- 方向分數支持
- 趨勢強度支持
- 價格結構支持

這比單純看突破價更像「趨勢型波段策略」。

### 4. 有明確的時間過濾與情緒過濾

`BTC v8.1` 內建：

- `ENABLE_SESSION_FILTER`
- `BLOCKED_UTC_HOURS`
- `FundingRateAnalyzer`

其中資金費率過濾會：

- 阻擋過熱 long
- 阻擋過熱 short

這其實是在做一件很重要的事：避免在「方向沒錯，但情緒極端」時進場。

### 5. v8.2 真正新增的是出場管理

`BTC v8.2` 對 `v8.1` 的核心增量是：

- `TP = 3R`
- `Break-even trigger = 1.5R`
- 一旦到達 `1.5R`，把 stop 拉回保本
- 到達 `3R` 就平倉

這套邏輯非常機械化，對研究和回測很友善。

## 與 check_price 的比較

## 已經相似的地方

`check_price` 目前已經有下列能力，不需要重做：

- `1h / 4h` 主導的波段判讀
- `ATR` 基礎的 entry / stop / take-profit 結構
- `breakeven_trigger / breakeven_stop`
- `paper trading` 固定 `RR`
- `event_type` 分級風險
- `protections` 風控保護層

也就是說，`BTC v8.2` 的「固定風報比 + 保本管理」這部分，`check_price` 已經有八成骨架。

## 明顯不同的地方

`BTC v8.2`：

- 單一 BTC 策略
- 直接實盤執行
- 單一分數決策
- Telegram 主要是下單/持倉通知

`check_price`：

- 多標的
- 分析、提醒、回填、回測、paper trading 分層
- 事件式決策，不是單一 score 決策
- Telegram 主要是決策輔助，不是交易所回報

所以不能直接把 `BTC v8.2` 複製進來取代現有架構。

## 建議導入的部分

### A. 值得導入：Funding Rate / Perp 情緒濾網

`BTC v8.2` 最值得借的第一件事，是資金費率過濾的概念。

對 `check_price` 的適合導入方式：

- 僅對 `BTCUSDT`、`ETHUSDT` 啟用
- 只做 `risk overlay`，不直接生成事件
- 只影響：
  - `effective_long_breakout`
  - `effective_short_breakdown`
  - `paper order risk multiplier`

不建議的做法：

- 不要讓 funding rate 直接改 `approach_*`
- 不要把 `XRP / SOL` 也硬套同一組 funding filter

### B. 值得導入：Session Filter 作為可選 research 開關

`BTC v8.2` 用 UTC 時段封鎖進場，這對 `check_price` 的 paper trading 很有研究價值。

適合的方式：

- 加在 `paper_order_engine.py`
- 先作為可選參數，不要先硬開在 runtime
- 用來驗證：
  - 哪些時段進場品質較差
  - 是否存在「高波動噪音時段」

不建議的做法：

- 不要先讓 live Telegram 因時段而完全閉嘴

### C. 值得導入：簡化版 MTF Score 當 secondary confidence

`check_price` 現在的事件分層比較細，但缺點是有時不夠直觀。

適合借用的做法：

- 額外產出一個 `0-100` 的 `swing_mtf_score`
- 只當：
  - 報告補充欄位
  - paper trading 加減碼依據
  - analytics 條件機率分桶欄位

不建議的做法：

- 不要用它直接取代目前的 `approach / effective / second_*` 架構

### D. 值得導入：保本規則更標準化

`BTC v8.2` 的 `1.5R -> BE` 很乾淨。

`check_price` 雖然已經有 `breakeven_trigger`，但目前：

- 不同事件的風險倍率已分級
- 模擬單引擎已有固定 `RR`

下一步可以考慮把保本規則再標準化成：

- `0.8x` 事件：`1.2R -> BE`
- `1.0x` 事件：`1.5R -> BE`
- `0.35x / 0.50x` 試單層：不移保本或更晚移

這比用單一保本邏輯套全部事件更合理。

## 不建議直接導入的部分

### 1. 不要直接搬實盤下單

`BTC v8.2` 直接：

- `MARKET` 開倉
- `STOP_MARKET` 掛保護單
- 寫狀態檔

這對 `check_price` 目前太早，因為：

- 現在仍在 paper trading / event screening 階段
- 多標的、多事件層比單標的 bot 更複雜

### 2. 不要把策略壓成單一 score 決策

`BTC v8.2` 的單一 score 模型很俐落，但 `check_price` 的優勢正好在：

- 事件分層
- 條件機率分析
- symbol / event_type 級別優化

所以比較好的做法是「新增 `swing_mtf_score` 作為補充」，不是改成只靠 score。

### 3. 不要直接複製單一 BTC 門檻

例如：

- `ADX_MIN = 25`
- `FR_BLOCK_LONG_ABOVE = 0.0005`

這些都高度依賴：

- 標的
- 槓桿
- 交易所
- 週期

只能當研究起點，不能直接視為 `check_price` 的通用值。

## 我對 check_price 的具體建議

### 第一優先

1. 在 `shadow_mode.py` 新增 `swing_mtf_score`
2. 在 `paper_order_engine.py` 新增 optional `session_filter`
3. 對 `BTCUSDT / ETHUSDT` 新增 funding overlay

### 第二優先

1. 讓 `paper_order_backtest.py` 可以比較：
   - 有 funding overlay
   - 無 funding overlay
2. 讓 `analytics_pipeline.py` 可以依 `swing_mtf_score` 分桶

### 第三優先

1. 重新校準不同事件的保本規則
2. 做 `event_type + mtf_score bucket + symbol` 的條件機率分析

## 建議先做的最小實作

若只選一個最值得先做的點，我建議：

### 先加 `BTC / ETH funding overlay`

理由：

- 這是 `BTC v8.2` 裡最成熟、最有辨識度的 edge
- 對中長線波段判斷幫助最大
- 不需要破壞 `check_price` 現有事件架構

預期效果：

- 過熱 long / 過熱 short 的事件會被降級
- 模擬單風險倍率可跟著調整
- 比直接加實盤下單安全很多

## 本文件對應的結論

可以參考 `BTC v8.2`，但不應複製成 `check_price v9`。

正確做法是：

- 借它的濾網思路
- 借它的保本與固定 R 管理
- 借它的 MTF 壓縮分數
- 保留 `check_price` 自己的多標的、事件分層、paper trading、analytics 架構

一句話總結：

`BTC v8.2` 值得學的是「簡潔的執行邏輯」，不是「單標的直接實盤 bot 形態」。 
