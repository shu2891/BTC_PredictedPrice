# Public Data Guide

這份文件說明 GitHub 公開版中保留了哪些內容、為什麼保留，以及讀者應該從哪裡開始看。

## 公開版的整理原則

這個 repo 的公開版本以兩個目標為主：

1. **讓別人能理解這個專案做了什麼**
2. **讓別人能直接看到已經整理出的研究結果**

因此，公開版優先保留：

- 程式碼
- 測試
- 策略與設計文件
- 回測與 paper trading 結果
- 條件機率分析資料
- 歷史報表與封存資料

而不是保留每一個執行期暫存檔。

## 建議閱讀順序

### 1. 先理解專案定位

- [`../專案初心與方法論.md`](../專案初心與方法論.md)
- [`../專案總覽與操作手冊.md`](../專案總覽與操作手冊.md)

### 2. 再看目前整理出的成果

- [`../提醒事件成效報告.md`](../提醒事件成效報告.md)
- [`../條件機率分析報告.md`](../條件機率分析報告.md)
- [`../paper_trading_execution_optimization_20260331.md`](../paper_trading_execution_optimization_20260331.md)

### 3. 想深入看設計與限制時

- [`./guides/runtime-and-data-flow.md`](./guides/runtime-and-data-flow.md)
- [`./guides/simulated-order-engine-plan.md`](./guides/simulated-order-engine-plan.md)
- [`./guides/paper-trading-health-and-execution-analysis.md`](./guides/paper-trading-health-and-execution-analysis.md)
- [`./guides/btc-v82-reference-analysis.md`](./guides/btc-v82-reference-analysis.md)

## 主要資料區域

| 目錄 | 內容 | 用途 |
| --- | --- | --- |
| [`../reports/`](../reports) | replay、shadow、market outlook、paper trading 輸出 | 直接查看各次運行與回測結果 |
| [`../analytics/`](../analytics) | `Parquet` 與 `DuckDB` 分析資料 | 重做條件機率分析或進一步研究 |
| [`../archive_legacy_data_20260318/`](../archive_legacy_data_20260318) | 舊版歷史報表與封存結果 | 追蹤專案早期版本與演進 |

## 這個公開版刻意不放什麼

### 1. 私密憑證

例如：

- `.pi_telegram_env`
- `.pi_telegram_env_ascii`

這些檔案只適合留在部署環境，不應進入公開 repo。

### 2. 執行期暫存與狀態

例如：

- `__pycache__/`
- `*.log`
- 即時 runtime state `*.db`
- Raspberry Pi snapshot database

這些檔案對理解專案價值幫助有限，卻會增加雜訊與暴露風險。

### 3. 只屬於本機或部署環境的東西

例如：

- 本機 service runtime state
- 某台機器上的部署快照

公開版重點是讓人看懂**方法與結果**，不是複製原作者當時那台機器的全部狀態。

## 如果你只想快速看成果

最短路徑可以直接看：

1. [`../README.md`](../README.md)
2. [`../提醒事件成效報告.md`](../提醒事件成效報告.md)
3. [`../條件機率分析報告.md`](../條件機率分析報告.md)
4. [`../paper_trading_execution_optimization_20260331.md`](../paper_trading_execution_optimization_20260331.md)
5. [`../reports/`](../reports)

## 如果你想重跑分析

可以從這些腳本開始：

- [`../analytics_pipeline.py`](../analytics_pipeline.py)
- [`../historical_replay_backtest.py`](../historical_replay_backtest.py)
- [`../paper_order_backtest.py`](../paper_order_backtest.py)

配合：

- [`../analytics/`](../analytics)
- [`../watchlist.json`](../watchlist.json)
- [`../requirements.txt`](../requirements.txt)
