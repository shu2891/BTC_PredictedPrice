# BTC_PredictedPrice / check_price

`check_price` 是一套加密市場決策輔助系統。它不是保證獲利的自動交易機器人，也不是單純猜漲跌的訊號工具；它的核心目標是把交易流程拆成可理解、可執行、可回測、可持續修正的模組。

## 這個專案在做什麼

專案目前涵蓋四條主線：

1. **多時間框架市場分析**
   - 由 `shadow_mode.py` 整合 `24h / 4h / 1h / 5m` 資料
   - 產出價格地圖、方向條件、停損停利與風控資訊

2. **事件分層與即時提醒**
   - 由 `market_alert_daemon.py` 監控 watchlist
   - 把訊號拆成 `approach`、`breakout_touch`、`effective`、`retest`、`second_*` 等層級
   - 讓「先觀察」和「可執行」分開

3. **成效追蹤與條件機率分析**
   - 由 `alert_performance_tracker.py`、`analytics_pipeline.py`、`historical_replay_backtest.py` 追蹤事件後續表現
   - 用 live 成效與歷史 replay 檢查哪些事件真的有 edge

4. **模擬下單 / paper trading**
   - 由 `paper_order_engine.py` 與 `paper_order_backtest.py` 模擬實際執行層
   - 納入手續費、滑價、部位上限與事件分級風險

## 先看這幾份文件

如果你第一次看這個 repo，建議依序閱讀：

1. [`專案初心與方法論.md`](./專案初心與方法論.md)
2. [`專案總覽與操作手冊.md`](./專案總覽與操作手冊.md)
3. [`docs/public-data-guide.md`](./docs/public-data-guide.md)
4. [`提醒事件成效報告.md`](./提醒事件成效報告.md)
5. [`條件機率分析報告.md`](./條件機率分析報告.md)
6. [`paper_trading_execution_optimization_20260331.md`](./paper_trading_execution_optimization_20260331.md)

## 目前可以直接看到的成果

### 事件成效追蹤

目前整理出的提醒事件資料中：

- 提醒事件總數：`304`
- 已完成成效回填：`1209`
- watch-only 觀察事件：`231`
- 非 watch-only 事件：`73`

較值得注意的結果可以先看：

- [`提醒事件成效報告.md`](./提醒事件成效報告.md)
- [`條件機率分析報告.md`](./條件機率分析報告.md)

### Paper trading / 回測

三個月 paper backtest（`2025-12-31` 到 `2026-04-01`）曾得到：

- baseline replay：
  - `465` 筆模擬訂單
  - `350` 筆已平倉交易
  - 平均 realized R：`+0.235`
- 加入成本與事件分級風險後，目前較平衡的研究版本：
  - `200` 筆訂單
  - `144` 筆已平倉交易
  - 平均 net realized R：`+0.106`
  - 最新 equity：`11749.09`

細節請看：

- [`paper_trading_execution_optimization_20260331.md`](./paper_trading_execution_optimization_20260331.md)
- [`docs/guides/paper-trading-health-and-execution-analysis.md`](./docs/guides/paper-trading-health-and-execution-analysis.md)

## 專案結構

| 位置 | 內容 |
| --- | --- |
| `*.py` | 主要 runtime、回測、分析與報表腳本 |
| [`tests/`](./tests) | `unittest` 測試 |
| [`reports/`](./reports) | 歷史 replay、shadow report、paper trading JSON / Markdown 輸出 |
| [`analytics/`](./analytics) | 條件機率分析用 Parquet / DuckDB 資料 |
| [`archive_legacy_data_20260318/`](./archive_legacy_data_20260318) | 較早期的歷史資料與報表封存 |
| [`docs/guides/`](./docs/guides) | 專案規範、runtime flow、paper trading 設計文件 |

## 主要腳本

| 腳本 | 用途 |
| --- | --- |
| [`shadow_mode.py`](./shadow_mode.py) | 多時間框架市場分析與 JSON / Markdown 輸出 |
| [`market_alert_daemon.py`](./market_alert_daemon.py) | 即時提醒與事件狀態追蹤 |
| [`alert_performance_tracker.py`](./alert_performance_tracker.py) | 回填提醒事件成效 |
| [`analytics_pipeline.py`](./analytics_pipeline.py) | 從 SQLite 輸出分析資料並建立條件機率統計 |
| [`historical_replay_backtest.py`](./historical_replay_backtest.py) | 歷史 replay 回測 |
| [`paper_order_engine.py`](./paper_order_engine.py) | 模擬下單引擎 |
| [`paper_order_backtest.py`](./paper_order_backtest.py) | paper trading 歷史回測 |
| [`advisor_api_server.py`](./advisor_api_server.py) | 本地 API / 查詢頁 |

## 快速開始

安裝相依套件：

```powershell
pip install -r requirements.txt
```

執行一次市場分析：

```powershell
python shadow_mode.py BTC ETH XRP SOL --llama off
```

啟動本地查詢頁：

```powershell
python advisor_api_server.py --host 127.0.0.1 --port 8765 --llama off --risk-profile conservative
```

執行測試：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

## 公開版本說明

這個 GitHub 版本保留的是**程式碼、測試、分析資料、歷史報表與研究文件**。  
基於安全與可讀性考量，公開版刻意排除了：

- Telegram 憑證與本機環境檔
- Python cache
- log
- 即時 runtime state database
- Raspberry Pi 部署時留下的 snapshot database

完整說明見 [`docs/public-data-guide.md`](./docs/public-data-guide.md)。

## 免責聲明

這個專案是研究與決策輔助用途，不構成投資建議，也不保證任何收益。  
所有輸出都應視為研究結果，而不是自動交易指令。
