# Project Conventions

## 適用範圍

本文件適用於整個 [check_price](C:\Users\User\Desktop\Codex\check_price) 工作區，包含：

- 核心策略與通知腳本
- 分析層與回測腳本
- API 介面
- 測試
- 文件與 service 範例

本文件刻意區分：

- `已存在的專案慣例`：已被目前程式碼採用，新增變更應優先遵守。
- `建議新增的規範`：為了降低未來維護成本而補上的約束，設計上避免和現有程式大量衝突。

## 1. 專案目錄約定

### 已存在的專案慣例

- 執行入口檔位於 repo 根目錄，而不是 `src/` 套件目錄。
  - 例： [shadow_mode.py](C:\Users\User\Desktop\Codex\check_price\shadow_mode.py) 、 [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py) 、 [alert_performance_tracker.py](C:\Users\User\Desktop\Codex\check_price\alert_performance_tracker.py)
- 設定集中在 [watchlist.json](C:\Users\User\Desktop\Codex\check_price\watchlist.json)。
- 測試集中在 [tests](C:\Users\User\Desktop\Codex\check_price\tests)，且檔名對應模組名稱。
  - 例： [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py) 對應 [test_market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\tests\test_market_alert_daemon.py)
- 產生的市場報告與回測輸出多數放在 repo 根目錄或 [reports](C:\Users\User\Desktop\Codex\check_price\reports)。
- 分析層輸出集中在 [analytics](C:\Users\User\Desktop\Codex\check_price\analytics)。
- 舊資料與淘汰輸出集中在 [archive_legacy_data_20260318](C:\Users\User\Desktop\Codex\check_price\archive_legacy_data_20260318)。

### 建議新增的規範

- 新增「規範型文件」一律放在 [docs\guides](C:\Users\User\Desktop\Codex\check_price\docs\guides)。
  - 理由：目前根目錄已有大量報告與說明檔，將規範文件集中可避免和生成報告混在一起。
- 新增「可重複執行的分析產物」時，優先放在 [analytics](C:\Users\User\Desktop\Codex\check_price\analytics) 或 [reports](C:\Users\User\Desktop\Codex\check_price\reports)，不要再把新的中間產物散落在根目錄。
- 若新增新的執行入口腳本，除非必須與既有腳本並列，否則先評估是否應放在 `x_skills/` 或未來的子目錄；不要無限制增加根目錄入口檔。

## 2. 模組邊界與分層方式

### 已存在的專案慣例

- [shadow_mode.py](C:\Users\User\Desktop\Codex\check_price\shadow_mode.py) 是策略與分析核心。
  - 包含行情取得、新聞/鏈上摘要、訊號計算、交易計畫、報告輸出。
- [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py) 負責即時通知與狀態機。
  - 依賴 `shadow_mode` 的分析結果，不自行重寫策略。
- [alert_performance_tracker.py](C:\Users\User\Desktop\Codex\check_price\alert_performance_tracker.py) 負責事件回填與成效報告。
- [analytics_pipeline.py](C:\Users\User\Desktop\Codex\check_price\analytics_pipeline.py) 負責從 SQLite 匯出樣本庫與條件機率分析資料。
- [advisor_api_server.py](C:\Users\User\Desktop\Codex\check_price\advisor_api_server.py) 是本地查詢介面，只應呼叫分析層，不應複製策略邏輯。
- [protections.py](C:\Users\User\Desktop\Codex\check_price\protections.py) 是保護層，提供額外風控判斷。
- [event_types.py](C:\Users\User\Desktop\Codex\check_price\event_types.py) 是事件角色/方向的共用定義。

### 建議新增的規範

- 事件類型、角色、方向映射只能在 [event_types.py](C:\Users\User\Desktop\Codex\check_price\event_types.py) 定義；其他檔案只能讀取，不可再自建平行常數。
  - 理由：目前 tracker、daemon、analytics 都依賴事件角色，一旦各自定義就會漂移。
- 保護層判斷只能在 [protections.py](C:\Users\User\Desktop\Codex\check_price\protections.py) 新增或修改；呼叫端只組合與顯示結果。
- 若某功能需要同時影響 `分析 -> 提醒 -> 回填 -> 分析層報表`，變更必須涵蓋全部相關層，而不是只改單一腳本。

## 3. 命名慣例

### 已存在的專案慣例

- Python 檔名與函式名稱使用 `snake_case`。
- 常數使用全大寫。
  - 例：`USER_AGENT`、`RSS_FEEDS`
- 事件名稱使用英文字串且帶語義層級。
  - 例：`approach_up`、`breakout_touch_up`、`effective_long_breakout`、`second_breakdown_short`
- 測試檔名使用 `test_<module>.py`。
- 測試案例類別使用 `<Feature>Tests`，測試函式使用 `test_<behavior>()`。

### 建議新增的規範

- 新增事件名稱必須同時表達：
  - 方向
  - 階段
  - 行為
  - 例：`second_breakout_long`
- JSON 設定鍵名一律用 `snake_case`，不得混用 camelCase。
- 新增報告檔名若屬於時間快照，必須包含日期或時間範圍。
  - 例：`歷史重播回測報告_2025-12-25_2026-03-25_長鏈收斂版.md`

## 4. 程式碼風格要求

### 已存在的專案慣例

- 入口腳本多半包含：
  - `parse_args()`
  - `utc_now()`
  - `main()` 或等效控制流程
- 型別標註已大量存在，尤其在回測與分析層。
- 使用標準函式庫 + 少量外部依賴。
  - 目前依賴在 [requirements.txt](C:\Users\User\Desktop\Codex\check_price\requirements.txt)：`requests`、`duckdb`、`polars`
- 測試使用 `unittest` 與 `unittest.mock`，不是 pytest。

### 建議新增的規範

- 新增 Python 程式碼需維持與現有腳本相同風格：
  - 以函式為主
  - 只在有明顯資料結構需求時使用 `dataclass`
  - 不引入大型框架
- 新增文字檔、JSON、Markdown 一律使用 UTF-8。
  - 理由：目前已有多處亂碼；新檔不能再延續編碼問題。
- 前端字串輸出到 HTML 時，只能用 `textContent`/`createElement` 這類安全方式；不得重新引入 `innerHTML` 直接拼接外部內容。
- 新增依賴前必須先更新 [requirements.txt](C:\Users\User\Desktop\Codex\check_price\requirements.txt) 與對應測試/文件。

## 5. 測試與驗證要求

### 已存在的專案慣例

- 每個核心模組都有對應 `unittest`。
- 常見驗證方式：
  - `python -m py_compile ...`
  - `python -m unittest discover -s tests -p 'test_*.py'`
- 測試偏向單元測試與流程測試，常用 mock 隔離外部依賴。

### 建議新增的規範

- 任何影響以下任一項的變更，必須至少新增或更新一個測試：
  - 事件生成
  - 事件角色/方向
  - 保護層
  - 報表輸出
  - 分析層 schema
  - API 回應格式
- 修改 SQLite schema、JSON payload、DuckDB/Parquet 欄位時，必須補相對應測試。
- 交付前至少執行：
  - `python -m py_compile` 覆蓋被修改檔案
  - `python -m unittest discover -s tests -p 'test_*.py'`

## 6. 變更回填要求

### 已存在的專案慣例

- 策略調整後通常會：
  - 重跑三個月 replay
  - 更新成效報告
  - 必要時同步 Pi service
- 分析層已能輸出：
  - 樣本庫
  - 條件機率報告
  - 調校建議報告

### 建議新增的規範

- 變更若會影響 `event_type`、`event_role`、觸發條件、保護層或報表分類，必須至少回填以下其中兩項：
  - 重跑 [historical_replay_backtest.py](C:\Users\User\Desktop\Codex\check_price\historical_replay_backtest.py)
  - 重跑 [alert_performance_tracker.py](C:\Users\User\Desktop\Codex\check_price\alert_performance_tracker.py)
  - 重建 [analytics_pipeline.py](C:\Users\User\Desktop\Codex\check_price\analytics_pipeline.py) 的輸出
- 若變更會影響 Pi 上常駐服務，提交前應明確記錄是否需要同步：
  - [market-alert-daemon.service.example](C:\Users\User\Desktop\Codex\check_price\market-alert-daemon.service.example)
  - [hourly-wait-reporter.service.example](C:\Users\User\Desktop\Codex\check_price\hourly-wait-reporter.service.example)
  - [alert-performance-tracker.service.example](C:\Users\User\Desktop\Codex\check_price\alert-performance-tracker.service.example)
- 負優化參數調整不得直接保留在 runtime 設定；若 replay 變差，應撤回並保留報告結論。

## 7. 禁止事項

### 已存在的專案慣例

- 本專案目前沒有套件化結構，也沒有 ORM 或大型 web framework。
- 生成資料與運營資料已高度依賴 SQLite、JSON、Parquet、DuckDB。

### 建議新增的規範

- 不得未經證據就新增「看起來很聰明」的策略規則；必須有 replay、live 樣本或分析層資料支持。
- 不得把 macro/on-chain 資訊直接硬塞進短線或 swing 觸發層，除非先說明它是 metadata、濾網還是直接決策因子。
- 不得在多個模組各自實作相同事件規則。
- 不得跳過測試直接修改 service 運行邏輯。
- 不得把新的規範文件散放在 repo 根目錄；新規範必須放入 `docs/guides/`。

## 8. 範例

### 範例 A：新增事件角色

正確做法：

1. 在 [event_types.py](C:\Users\User\Desktop\Codex\check_price\event_types.py) 新增事件角色與方向。
2. 更新 [market_alert_daemon.py](C:\Users\User\Desktop\Codex\check_price\market_alert_daemon.py) 實際生成該事件。
3. 更新 [alert_performance_tracker.py](C:\Users\User\Desktop\Codex\check_price\alert_performance_tracker.py) 與 [analytics_pipeline.py](C:\Users\User\Desktop\Codex\check_price\analytics_pipeline.py) 的分類使用。
4. 補對應測試。

錯誤做法：

- 只在 daemon 裡新增字串事件，但沒有更新事件角色映射與報表。

### 範例 B：新增分析欄位

正確做法：

1. 先在事件來源寫出欄位。
2. 再更新 [analytics_pipeline.py](C:\Users\User\Desktop\Codex\check_price\analytics_pipeline.py) 匯出欄位。
3. 最後補 [tests\test_analytics_pipeline.py](C:\Users\User\Desktop\Codex\check_price\tests\test_analytics_pipeline.py)。

### 範例 C：調整通知條件

正確做法：

1. 在 [watchlist.json](C:\Users\User\Desktop\Codex\check_price\watchlist.json) 或單一邏輯模組中做最小化調整。
2. 重跑相關測試。
3. 至少重跑一次 replay 或 live 成效回填。

## 9. 尚未定案的地方

以下事項目前仍是專案現況，不視為既定規範：

- 是否要把根目錄入口腳本逐步搬入真正的 Python package。
- 是否要把使用者可讀報告從 repo 根目錄移到專門的 `docs/reports/`。
- 是否要建立正式的編碼修復計畫，統一清掉現有亂碼字串。
