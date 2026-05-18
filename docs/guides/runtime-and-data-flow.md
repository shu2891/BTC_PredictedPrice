# Runtime And Data Flow

## 適用範圍

本文件補充 [Project Conventions](C:\Users\User\Desktop\Codex\check_price\docs\guides\project-conventions.md)，聚焦在：

- 常駐腳本的責任分界
- 主要資料檔案的所有權
- 報告/分析層輸出位置
- 變更時應同步檢查的流程

## 1. 核心執行流程

### 已存在的專案慣例

- [shadow_mode.py](C:\Users\User\Desktop\Codex\check_price\shadow_mode.py)
  - 建立單一標的分析結果
  - 輸出 `trade_plan`、`actionable_levels`、`short_term_signal`、`long_short_plan`、`protections`
- [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py)
  - 週期性呼叫 `shadow_mode`
  - 產生 Telegram 事件提醒
  - 將事件與狀態寫入 [alert_state.db](C:\Users\User\Desktop\Codex\check_price\alert_state.db)
- [alert_performance_tracker.py](C:\Users\User\Desktop\Codex\check_price\alert_performance_tracker.py)
  - 讀取 `alert_events`
  - 回填 `alert_event_performance`
  - 更新成效報告與分析層
- [analytics_pipeline.py](C:\Users\User\Desktop\Codex\check_price\analytics_pipeline.py)
  - 從 SQLite 匯出 Parquet / DuckDB
  - 建立條件機率與調參報告
- [hourly_wait_reporter.py](C:\Users\User\Desktop\Codex\check_price\hourly_wait_reporter.py)
  - 固定週期輸出四小時摘要

### 建議新增的規範

- 若變更會影響事件欄位或角色，應把影響範圍視為完整鏈：
  - `shadow_mode -> daemon -> tracker -> analytics`
- 新增任何常駐腳本前，先明確指定它屬於哪一層：
  - `analysis`
  - `delivery`
  - `backfill`
  - `analytics`

## 2. 主要資料檔案與所有權

### 已存在的專案慣例

- [shadow_mode.db](C:\Users\User\Desktop\Codex\check_price\shadow_mode.db)
  - 由 [shadow_mode.py](C:\Users\User\Desktop\Codex\check_price\shadow_mode.py) 維護
- [alert_state.db](C:\Users\User\Desktop\Codex\check_price\alert_state.db)
  - 由 [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py) 與 [alert_performance_tracker.py](C:\Users\User\Desktop\Codex\check_price\alert_performance_tracker.py) 共同維護
- [analytics\market_context_snapshots.parquet](C:\Users\User\Desktop\Codex\check_price\analytics\market_context_snapshots.parquet)
- [analytics\event_outcomes.parquet](C:\Users\User\Desktop\Codex\check_price\analytics\event_outcomes.parquet)
- [analytics\market_samples.duckdb](C:\Users\User\Desktop\Codex\check_price\analytics\market_samples.duckdb)
- [analytics\market_samples_v2.duckdb](C:\Users\User\Desktop\Codex\check_price\analytics\market_samples_v2.duckdb)
  - 由 [analytics_pipeline.py](C:\Users\User\Desktop\Codex\check_price\analytics_pipeline.py) 產生

### 建議新增的規範

- 不得手動編輯 SQLite、Parquet、DuckDB 產物內容。
- 若 schema 有變更，必須由對應腳本自動遷移或重建，不靠手動 SQL patch。
- Pi 快照資料庫屬於分析輸入，不得誤當成 runtime 主資料庫覆蓋本地運行檔。

## 3. 報告輸出位置

### 已存在的專案慣例

- 使用者可讀的報告多數留在 repo 根目錄。
- 細節 JSON 常放在 [reports](C:\Users\User\Desktop\Codex\check_price\reports)。
- analytics 詳細表格放在 [analytics](C:\Users\User\Desktop\Codex\check_price\analytics)。

### 建議新增的規範

- 後續新增輸出時，依下列規則放置：
  - 使用者手動閱讀的總結：repo 根目錄
  - 結構化回測/分析 JSON：`reports/`
  - 樣本庫與分析資料集：`analytics/`
  - 已過期或被替代的輸出：`archive_legacy_data_20260318/`

## 4. Service 與部署同步

### 已存在的專案慣例

- repo 內有 service example 檔：
  - [market-alert-daemon.service.example](C:\Users\User\Desktop\Codex\check_price\market-alert-daemon.service.example)
  - [hourly-wait-reporter.service.example](C:\Users\User\Desktop\Codex\check_price\hourly-wait-reporter.service.example)
  - [alert-performance-tracker.service.example](C:\Users\User\Desktop\Codex\check_price\alert-performance-tracker.service.example)
- Pi 上有長駐 service，需要手動同步更新。

### 建議新增的規範

- 若修改下列檔案，提交時必須明確註記是否需要同步 Pi：
  - [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py)
  - [hourly_wait_reporter.py](C:\Users\User\Desktop\Codex\check_price\hourly_wait_reporter.py)
  - [alert_performance_tracker.py](C:\Users\User\Desktop\Codex\check_price\alert_performance_tracker.py)
  - [watchlist.json](C:\Users\User\Desktop\Codex\check_price\watchlist.json)
- 若修改 service example，應同步檢查實際 Pi service 參數是否仍一致。

## 5. 最小驗證矩陣

### 建議新增的規範

依變更類型至少執行以下檢查：

- 純文件變更：
  - 確認路徑與索引正確
- 分析/策略邏輯變更：
  - `py_compile`
  - `unittest`
  - 至少一個實跑腳本
- 事件/回填/分析層變更：
  - `py_compile`
  - `unittest`
  - tracker 或 analytics 匯出至少跑一次
- 常駐服務邏輯變更：
  - 本地 `--once` 或單次執行驗證
  - 必要時重啟 Pi service
