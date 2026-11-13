# 資料來源 Audit

範圍：tw-market-scanner（scanner/、history/、config/）與 track_stock 網站讀到的國外強度資料。只讀、未修改程式。
檢查日：2026-11-13，資料到 2026-11-12。

## 1. 來源一覽

| 資料 | 來源 | 程式 | 還原 | 備註 |
|---|---|---|---|---|
| 台股日收盤（全市場） | 證交所／櫃買 OpenAPI 與 rwd 端點（`scanner/fetch.py` 的 TWSE、TPEX、MI_INDEX） | `fetch.py`、`history.py` | **未還原**存檔；計算時用 `history/adjust.csv` 還原 | 2023-09-01～2023-10 以後由 `replay.py fetch` 回補 |
| 除權息、減資 | 證交所 TWT49U、TWTAUU；櫃買 exDailyQ | `adjust.py` | — | div 7,844、reduction 75、jump 336 筆 |
| 加權指數 | 證交所 MI_INDEX | `history.upsert_index` | — | 775 天，和台股交易日完全一致 |
| 發行股數 | 證交所／櫃買公司基本資料 | `history.upsert_shares` | — | **只有最新一份**（as_of 全是 2026-11-12，1,996 檔） |
| 國外個股 | Yahoo Finance（yfinance 1.7.0） | `theme_peers.fetch_yahoo` | `auto_adjust=True`（分割＋股利） | 每天抓 3 年 |
| 美股 ETF、^TNX | Yahoo Finance | `replay_us_sectors.fetch_yahoo` | `auto_adjust=True` | 研究用 |
| 匯率 TWD=X、^GSPC/^IXIC/^SOX/^VIX/^TNX | Yahoo Finance | `market.py` | — | 只給燈號與網站用；**研究裡沒有用匯率** |
| 美股財報（us_leaders） | SEC EDGAR companyconcept | `edgar_client.py` | — | 一季一次，不在這次研究範圍 |
| FinMind | track_stock `common/finmind_client.py` | — | — | 這次研究沒有用；有呼叫次數限制 |

## 2. 逐項確認

**是否用還原價**
- 台股：存檔是實際成交價；`score.score_stocks`、`theme_peers.tw_weekly`、`replay_us_sectors.tw_panel` 都會先 `adjust.adjusted()` 再算報酬。✅
- 國外、ETF：`auto_adjust=True`，用 Yahoo **今天**的調整因子往回調整。算報酬沒問題（比例不變），但不能拿來算歷史市值或價格水準。✅（報酬）⚠（價格水準）

**除權息、拆股、合併**
- 台股：官方除權息、減資有處理。沒有官方資料的跳空（單日超過 ±10.5%）用 `jump` 處理，共 336 筆。**這種處理會把當天真正的漲跌（最多 10%）一起吃掉**，屬於設計取捨，影響單日報酬、對 20 日以上的影響比較小。⚠
- `adjust.csv` 裡有 ex_date 在資料日之後的事件（最晚 2026-11-23，是已公告、還沒除息的）。它只會把全部歷史價格乘上同一個係數，不影響報酬，但因子是怎麼算的要再確認。⚠（待確認）
- 國外：交給 Yahoo；**合併、下市的公司 Yahoo 抓不到**（例：QRVO 只剩 1 筆，Qorvo 與 Skyworks 合併後下市的可能性高，未查證）。⚠

**缺漏**
- 台股交易日 775 天，和加權指數日期一對一相符。✅
- 國外：QRVO 抓不到 → 化合物半導體實際只剩 SWKS 一檔。SNDK 2025-02、285A.T（Kioxia）2024-12 才有資料，之前那兩個主題的國外對照股比較少。⚠

**有沒有拿最新資料回填歷史（前視）**
- **產業別：有。** `replay.py` 回補 2023-09～2026-10 的 history/daily 時，產業欄位用的是 `industry_map()`，也就是**最新一天**的「代號 → 產業」對照（程式註解有寫「回放用現在的對照」）。後果：
  1. 換過產業的公司，整段歷史都用現在的產業。
  2. **已下市、下櫃的公司**在最新一天沒有資料，所以回補時產業是空白（2023-09-01 有 344 檔空白，2026-11-12 是 418 檔）。用產業選股的研究（`replay_us_sectors` 的產業籃）會**漏掉這些已下市的公司** → 族群層級的存活者偏差。🔴
  - 全市場層級沒有這個問題：30 檔中途消失的普通股還在 history 裡，大盤基準有包含它們。✅
- **發行股數：有。** 只有最新一份，回放時的歷史市值 = 歷史還原價 × 現在股數（`history.upsert_shares` 的 docstring 有寫明）。🔴
- **主題名單：有。** `config/themes.yaml` 是 2026-10-25 定稿，`config/theme_peers.yaml` 是 2026-10-27 列的，回測整段都用它們。🔴（設計限制，見 findings）

## 3. 小結

| 項目 | 狀態 |
|---|---|
| 台股價格、指數完整度 | ✅ |
| 台股還原 | ✅（跳空偵測會吃掉真實漲跌，⚠） |
| 國外還原 | ✅（報酬） |
| 國外下市公司 | ⚠ 抓不到，有存活者偏差 |
| 歷史產業別 | 🔴 用最新對照回填，下市公司空白 |
| 歷史股數／市值 | 🔴 只有最新股數 |
| 主題與對照名單 | 🔴 2026-10 才列，事後挑選 |
| 匯率 | 研究沒有使用，國外報酬都是當地貨幣 |
