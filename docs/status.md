# 實作狀態（2026-10-24）

規格：私人 repo track_stock 的 `docs/market-scanner-plan.md`。

## Phase 1：已完成並驗收
- 上市、上櫃、集保共 11 個必要來源＋4 個選用來源（注意股、處置股）都抓得到，資料日期 2026-10-22。
- 2330（上市）與 3324（上櫃）兩檔都有：收盤、成交金額、外資、投信、融資餘額、產業名稱、月營收 YoY。
- 融資資料的日期用證交所有帶日期的 MI_MARGN 對過（2330 前日 35,330 → 今日 35,472，兩邊一致）。
- 上市與上櫃的產業名稱一致（半導體業、電子零組件業…）；只有「金融保險業」（上市）與「金融業」（上櫃）不同，已在 rules.yaml 設別名。
- `config/tdcc_levels.yaml`：分級 17 = 合計（股數與 2330 已發行股數完全相同）、16 = 差異數調整。
- 實測發現並處理：TPEx 融資檔案曾下載到一半（JSON 截斷）→ `http.py` 檢查 Content-Length 並重試。

## Phase 2：已完成並驗收
- A/B/C/D 評分、產業評分、週五推薦、ledger、對答案、`out/latest.json`。
- 22 個測試全過：歷史不足回 null、營收出表日在資料日期之後不可見、每週最多寄 3 檔但最多記 10 檔、
  各關卡刷掉的股票都記錄 failed_at、ledger 不可改寫且同日重跑不重複、下市股票留在樣本內。

## Phase 4：已完成（歷史回放）
- `scanner/replay.py`：fetch / revenue / score / verify 四個指令；已補 2025-10-23 到 2026-10-21 共 235 個交易日與 2025-08 至 2026-08 月營收，0 天失敗。
- 帶日期版與當日版 2026-10-22 逐欄比對 100% 一致。
- 結果見 `docs/replay-report.md`（推薦幾乎不出現、產業分數看不出預測力，以及為什麼不能直接據此改規則）。
- 基準已改用加權指數（`history/index/twii.csv`），rules 版號 2026-10-24.2。

## 2026-10-25 修正（不看結果也該修的三項＋並行版本）
- 產業硬關卡關閉（rules.yaml `picks.industry_gate: false`），產業分數只顯示。
- 評估基準改為同日 focus 股票等權中位數（`grade.benchmark: ew_median_focus`），加權指數另記 `bench_twii`。
- 對照組：每週 focus 全部股票寫進 `pick_universe`（取代原本只記被刷掉的 `pick_rejects`）。
- 推薦分 base（寄出）與 momentum（只記錄）兩版；`out/latest.json` 的 `shadow_picks_this_week` 是 momentum。
- 報告產生器 `python -m scanner.report --base ledger|ledger_replay`。
- 回放已依新規則整份重算；結果見 `docs/replay-report.md`。

## 2026-10-25 細分主題（草稿）
- `config/themes.yaml`：27 個主題、84 檔，使用者改過第二版，仍是 `reviewed: false`（每日流程不使用）。
- 主題強度：前 3 大（當天市值）裡，有 ≥3 個訊號的成員過半就算強（1→1、2→2、3→2）。
- 第三個版本 `theme_top3`：只從強主題前 3 大挑，其他關卡同 base，只記錄不寄。
- `history/shares.csv`：發行股數，算市值用。
- 回放預覽結果與「後見之明偏誤」說明見 `docs/replay-report.md`。

## 還沒做 / 已知限制
1. history 現在有 236 個交易日，A、B、C 都有值。
2. 基準：有加權指數時用指數，不足 21 天才退回中位數。
3. C 類集保：openapi 只有最新一週，滿 4 週前用融資變化代理（`c_basis` 欄位會標示）。
4. D 類的「較前月加速」在只有一個月歷史時用「高於累計 YoY」代理（`d_basis` 欄位會標示）。
5. 當沖比例條件（P.no_trap 的一部分）尚未接入。
6. 週五休市的那週沒有推薦。
7. GitHub 雲端 runner 能否順利連上交易所 API **還沒實測**，第一次請手動按 Run workflow 看結果。
8. Phase 3（私人 repo 接上）、Phase 5（回測報告頁）還沒做。
9. 回放的 ledger_replay 與真實 ledger 分開；真實 ledger 從 2026-10-22 開始累積。
