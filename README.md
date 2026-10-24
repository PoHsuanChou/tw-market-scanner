# tw-market-scanner

每個交易日收盤後，掃描台股上市與上櫃全市場的公開資料，依產業與個股打分，
並記錄每一個判斷，事後對答案。**資料整理與研究判讀，不是買賣建議。**

## 公開了什麼、沒公開什麼
- 公開：掃描程式、證交所／櫃買／集保的公開資料、產業與個股評分、推薦紀錄、對答案結果。
- 沒有：任何人的持倉、停損價、分析卡、SOP、email 或金鑰。這個 repo 不含它們。

## 每天做什麼（`scanner/run_daily.py`）
1. `fetch`：抓上市、上櫃、集保的最新公開資料，存到 `raw/` 與 `history/`
2. `score`：個股 A 價量、B 法人、C 大戶、D 基本面，再加總成產業分數
3. `ledger`：把判斷寫進 `ledger/*.jsonl`（只增不改）
4. `picks`：週五才產生推薦，最多寄出 3 檔，最多記錄 10 檔
5. `grade`：到期的判斷（5、20、60 個交易日後）算實際報酬，寫進 `ledger/outcomes.jsonl`
6. 輸出 `out/latest.json` 與 `out/fetch_status.json`

## 重要特性
- 公開 API 只給最新一個交易日，沒有歷史，所以每天都要存。資料日期以回傳內容為準，不是執行日。
- 歷史不足時 A、B、C 回傳 `null`（不算分也不算失敗）。**從第一天開始約 21 個交易日後才有 A 類。**
- 缺值是空白，不是 0。唯一例外：交易所只列有法人交易的股票，已成功抓到的市場裡沒被列出的普通股，法人買賣超記 0。
- 所有門檻在 `config/rules.yaml`，改動要改 `version`，ledger 每筆都記版本。

## 本機執行
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m scanner.run_daily              # 完整流程
.venv/bin/python -m scanner.run_daily --no-fetch   # 用 history 最新一天重算
```

## 已知限制（也寫在 `docs/status.md`）
- 基準是「全部普通股 20 日報酬中位數」的暫代，尚未驗證大盤指數來源。
- 集保只有最新一週，C 類在累積滿 4 週前用融資變化代理。
- 週五休市的那週沒有推薦。
- 當沖比例條件尚未接入。
- GitHub 的雲端 runner 能不能順利連上交易所 API，還沒有實測。
