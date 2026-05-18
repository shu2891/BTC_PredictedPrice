# Report Index

這份索引是給第一次進 repo 的讀者用的。  
如果你只想快速了解這個專案的**方法、結果、限制與演進**，先看這幾份就夠。

## 1. 最推薦先看的 6 份文件

| 順序 | 文件 | 為什麼先看 |
| --- | --- | --- |
| 1 | [`../專案初心與方法論.md`](../專案初心與方法論.md) | 先理解專案不是在做「神預測」，而是在做可驗證的交易決策流程 |
| 2 | [`../專案總覽與操作手冊.md`](../專案總覽與操作手冊.md) | 快速知道系統模組、事件層級與日常使用方式 |
| 3 | [`../提醒事件成效報告.md`](../提醒事件成效報告.md) | 直接看真實提醒事件的後續表現 |
| 4 | [`../條件機率分析報告.md`](../條件機率分析報告.md) | 看不同事件、方向與市場狀態下的條件機率 |
| 5 | [`../paper_trading_execution_optimization_20260331.md`](../paper_trading_execution_optimization_20260331.md) | 看模擬執行層如何從樂觀回測走向成本感知 |
| 6 | [`./guides/paper-trading-health-and-execution-analysis.md`](./guides/paper-trading-health-and-execution-analysis.md) | 看目前還離真實自動交易差哪些關鍵條件 |

## 2. 如果你想看「成果」

### 事件與條件機率

- [`../提醒事件成效報告.md`](../提醒事件成效報告.md)
- [`../條件機率分析報告.md`](../條件機率分析報告.md)
- [`../現有數據再分析_20260330_新版本全量統計.md`](../現有數據再分析_20260330_新版本全量統計.md)

### Paper trading / 模擬執行

- [`../paper_trading_execution_optimization_20260331.md`](../paper_trading_execution_optimization_20260331.md)
- [`../paper_trading_event_weighting_20260401.md`](../paper_trading_event_weighting_20260401.md)
- [`../paper_trading_backtest_2025-12-31_2026-04-01_結論.md`](../paper_trading_backtest_2025-12-31_2026-04-01_結論.md)

### 歷史 replay / 參數演進

- [`../歷史重播回測報告_2025-12-20_2026-03-20_中長線版.md`](../歷史重播回測報告_2025-12-20_2026-03-20_中長線版.md)
- [`../歷史重播回測報告_2025-12-20_2026-03-20_調參版.md`](../歷史重播回測報告_2025-12-20_2026-03-20_調參版.md)
- [`../歷史重播回測報告_2025-12-25_2026-03-25_funding_mtf版.md`](../歷史重播回測報告_2025-12-25_2026-03-25_funding_mtf版.md)

## 3. 如果你想看「方法」

- [`../策略設計與演進筆記.md`](../策略設計與演進筆記.md)
- [`../市場樣本庫與條件機率分析方案.md`](../市場樣本庫與條件機率分析方案.md)
- [`../中長線策略與鏈上分析調整說明.md`](../中長線策略與鏈上分析調整說明.md)
- [`./guides/runtime-and-data-flow.md`](./guides/runtime-and-data-flow.md)
- [`./guides/simulated-order-engine-plan.md`](./guides/simulated-order-engine-plan.md)

## 4. 如果你想看「演進脈絡」

- [`../外部優秀交易專案研究與導入建議.md`](../外部優秀交易專案研究與導入建議.md)
- [`../外部方案導入進度_20260320.md`](../外部方案導入進度_20260320.md)
- [`../參數調校建議報告.md`](../參數調校建議報告.md)
- [`../第三階段調參驗證結論_20260323.md`](../第三階段調參驗證結論_20260323.md)
- [`../中長線版_vs_舊版短線_三個月比較_20260323.md`](../中長線版_vs_舊版短線_三個月比較_20260323.md)

## 5. 如果你只想看原始輸出

| 位置 | 內容 |
| --- | --- |
| [`../reports/`](../reports) | 最新 replay、shadow、market outlook、paper trading JSON / Markdown |
| [`../analytics/`](../analytics) | `Parquet` 與 `DuckDB` 分析資料 |
| [`../archive_legacy_data_20260318/`](../archive_legacy_data_20260318) | 舊版報表與早期結果封存 |
