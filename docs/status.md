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

## 2026-10-25 波段版主題強度（網頁與通知預設）
- 主題波段強 = 前 3 大 20 日報酬中位數贏 focus 中位數，且過半成員最近 10 天至少一半日子 ≥3 訊號。
- 回放：一段強平均約 9 天（單日版 3 天）、每週約 2 個轉強（單日版 7 個）。
- 轉強事件分 kind=swing（通知用）與 kind=daily（對照）；推薦多一個並行版本 theme_swing_top3。
- theme_scores 的 key 含 rules_version，改版後同一天另記新版本，畫圖用最後一筆。

## 2026-10-27 三年回放與股價還原
- 歷史 2023-09-01 到 2026-10-22 共 762 個交易日，回放評分 2023-10-02 起 742 天；0 天抓取失敗。
- `scanner/adjust.py`：除權息（TWT49U、exDailyQ）、減資（TWTAUU）、偵測到的跳空 → `history/adjust.csv`；
  評分、對答案、市值排名都用還原價；每日掃描會補當天的事件。
- 回放明細（約 120 MB，outcomes.jsonl 就 95 MB）不進 git；進 git 的是 `replay/summary.json`（分年度摘要）與 `replay/theme_recent.json`。
- 報告：`docs/replay-report.md`（重點與限制）、`docs/replay-by-year.md`（完整分年度表）。
- 重算回放：`python -m scanner.adjust build --from 2023-09-01 --to <最新日>` → `python -m scanner.replay score --from 2023-10-02 --to <最新日>`。

## 2026-10-27 開始記錄每日市場燈號
- `ledger/market_modes.jsonl`：每個台股交易日一筆（只增不改，同一天第一筆為準），欄位 tw_data_date、mode、inputs（VIX、SOX/NASDAQ 5/20 日漲跌）、us_asof、stale。
- 燈號是「台股 d 日 17:30 能知道的」：只用日期 < d 的美股資料（`market.mode_asof`），補跑、重跑結果一致，不偷看之後。
- `out/accuracy.json` 的 `real_by_mode`、`mode_days`：真實紀錄依判斷當天燈號分組；網站「主題」頁有對應表格。
- 目的：之後看強主題的優勢是不是集中在特定燈號。**規則不因此調整**；每種燈號要 ≥ 30 筆樣本，通常要累積數個月。
- 回放期間（2023–2026）的燈號沒有逐日記錄；若要事後分析回放，可用 Yahoo 的長期資料依同一個函式重建，屬於探索，不是規則依據。

## 2026-10-27 推薦版本調整（rules 版號 2026-10-27.1）
- 回放分析（只讀 ledger_replay）：A–D 單獨看都沒讓勝率明顯高於 50%；波段強主題池子裡隨機挑，3 個月中位數超額 +10.2%，
  比寄出的波段版 +7.8% 好；池子中被 P.not_priced（20 日已漲 ≥ 25%）擋掉的股票之後 3 個月中位數 +23.3%。
- base 設 `record: false`：不寄也不記推薦（回放勝率約 51%），但關卡仍用來記 pick_universe 的 failed_at。
- 新增 `theme_swing_uncapped`（id 形如 2026-W44-U01）：同寄出的波段版，只差不擋已漲 ≥ 25%；只記錄不寄。
- 版號 2026-10-27.2：寄出的版本改成 theme_swing_uncapped（使用者決定）；舊的 theme_swing_top3 改為只記錄，並行比較。
- 新版本目前沒有回放紀錄，要重跑回放才會有。
- 主題名單事後挑、回放多數是多頭，所以要用真實紀錄和寄出版比較後再決定。

## 2026-10-27 國外強度（rules 版號 2026-10-27.3，只顯示、不影響推薦）
- 回測（2023-10～2026-10，141 週、25 個主題）：國外同類公司 20 日報酬比全部對照股多 ≥ 5 個百分點的主題，
  之後 1 個月超額中位數多約 +3.2（t≈2.2）、3 個月多約 +11.9；「國外動、台灣還沒動」沒有比只看國外更好。
  名單是事後列的（列的時候已知哪些國外股票很紅），台股主題名單也是事後挑，結果偏樂觀。
- 名單：`config/theme_peers.yaml`（25 個主題、62 檔；BMC、機殼/滑軌沒有好對照）。門檻：rules.yaml `theme_peers`。
- `python -m scanner.theme_peers`（daily.yml 在 market 之後跑，失敗不影響 job）：
  `out/theme_peers.json` 給主題頁；`ledger/theme_peers.jsonl` 每個台股交易日 × 主題一筆（只增不改）。
- 只用日期 < 台股日期的國外收盤；Yahoo 抓不到時沿用上一次並標 stale，不寫 ledger。
- 網站主題頁的顯示要在私人 repo 改。
- 同期也測了「跌深＋高殖利率」：之後反而比較差（殖利率陷阱），沒有放進流程。

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
