"""歷史回放：用有帶日期的公開端點補齊過去的每日資料，再用同一套評分重算，寫進 ledger_replay/。

為什麼要有：公開 API 平常只給最新一天，系統要累積約 21 個交易日 A 類才會有值。
回放讓你不用等，馬上有歷史可以看、可以回測。

只在本機跑，不要放 GitHub Actions（一天要打 6 個請求、每個隔數秒，跑完要幾十分鐘）。

資料來源（2026-10-24 驗證過，1/5 與 10/22 兩個日期都有回資料）：
  上市：MI_INDEX（個股收盤＋加權指數）、T86（三大法人）、MI_MARGN（融資融券）
  上櫃：afterTrading/otc、insti/dailyTrade、margin/balance
  月營收：公開資訊觀測站 mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{民國年}_{月}.html（Big5-HKSCS）

回放的已知偏誤（報告裡一定要寫）：
  1. 存活者偏誤：產業別用「現在」的對照表，之後下市的股票沒有產業名稱，不會進入 focus 樣本。
  2. 集保沒有歷史，C 類一律用融資代理。
  3. 注意／處置股旗標沒有歷史，P.no_trap 的這一項在回放期間不生效。
  4. 月營收的可見日用法定截止日（次月 10 日）當保守值，真實公布日通常更早，所以 D 類會晚一點才亮，不會偷看未來。
  5. 成分股名稱、股本等基本資料用現在的。

用法：
  python -m scanner.replay fetch  --from 2025-12-01 --to 2026-10-21 [--sleep 2.5]   # 補歷史（可中斷，重跑會續）
  python -m scanner.replay revenue --from 2025-10 --to 2026-09                       # 補月營收
  python -m scanner.replay score  --from 2026-02-01 --to 2026-10-22                  # 重算並寫 ledger_replay
  python -m scanner.replay verify --date 2026-10-22                                  # 帶日期版 vs 當日版逐欄比對
"""
import io
import os
import re
import json
import time
import logging
import argparse
from datetime import date, timedelta

import pandas as pd
import requests

from scanner import normalize as N, history, score, picks as picks_mod, ledger, grade, themes, replay_store
from scanner.http import get_json, FetchError, UA

log = logging.getLogger("replay")

TWSE_WWW = "https://www.twse.com.tw/rwd/zh"
TPEX_WWW = "https://www.tpex.org.tw/www/zh-tw"
MOPS = "https://mopsov.twse.com.tw/nas/t21/{mkt}/t21sc03_{y}_{m}.html"
REPLAY_LEDGER = "ledger_replay"


def weekdays(a, b):
    d, end = date.fromisoformat(a), date.fromisoformat(b)
    while d <= end:
        if d.weekday() < 5:
            yield d.isoformat()
        d += timedelta(days=1)


def _state_path(root):
    return os.path.join(root, "history", "replay_state.json")


def load_state(root):
    try:
        with open(_state_path(root), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"holidays": [], "failed": {}}


def save_state(root, st):
    os.makedirs(os.path.dirname(_state_path(root)), exist_ok=True)
    st["holidays"] = sorted(set(st["holidays"]))
    with open(_state_path(root), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def industry_map(root):
    """最新一天的 code → 產業名稱（回放用現在的對照，見檔頭偏誤 1）。"""
    latest = score.trading_dates(root)[-1]
    df = pd.read_csv(os.path.join(root, "history", "daily", f"{latest}.csv.gz"), dtype={"code": str})
    df = df[df["industry"].notna() & (df["industry"] != "")]
    return dict(zip(df["code"], df["industry"]))


# ---------------- 單日抓取 ----------------

def fetch_one_day(d, sleep):
    """回傳 ('ok', bundle) | ('holiday', None) | ('fail', 原因)。
    休市判斷只信證交所明確回「沒有符合條件」；其他異常（被擋、逾時）一律當失敗，之後會重試。"""
    dnum, dslash = d.replace("-", ""), d.replace("-", "/")

    def call(url, params):
        time.sleep(sleep)
        return get_json(url, params=params, retries=3, sleep=5.0)

    try:
        mi = call(f"{TWSE_WWW}/afterTrading/MI_INDEX", {"date": dnum, "type": "ALLBUT0999", "response": "json"})
        day, twii = N.twse_mi_index(mi, d)
        if not day:
            if "沒有符合條件" in str((mi or {}).get("stat", "")):
                return "holiday", None
            return "fail", f"MI_INDEX 無資料：{(mi or {}).get('stat')}"
        t86 = N.twse_t86(call(f"{TWSE_WWW}/fund/T86", {"date": dnum, "selectType": "ALLBUT0999", "response": "json"}))
        mg = N.twse_margin_dated(call(f"{TWSE_WWW}/marginTrading/MI_MARGN",
                                      {"date": dnum, "selectType": "STOCK", "response": "json"}))
        otc = N.tpex_otc_dated(call(f"{TPEX_WWW}/afterTrading/otc", {"date": dslash, "type": "EW", "response": "json"}), d)
        tinst = N.tpex_inst_dated(call(f"{TPEX_WWW}/insti/dailyTrade",
                                       {"type": "Daily", "sect": "EW", "date": dslash, "response": "json"}))
        tmg = N.tpex_margin_dated(call(f"{TPEX_WWW}/margin/balance", {"date": dslash, "response": "json"}))
    except FetchError as e:
        return "fail", str(e)
    missing = [n for n, v in (("T86", t86), ("上市融資", mg), ("上櫃行情", otc), ("上櫃法人", tinst), ("上櫃融資", tmg)) if not v]
    if missing:
        return "fail", "缺 " + "、".join(missing)
    return "ok", {"day": {**day, **otc}, "margin": {**mg, **tmg}, "inst": {**t86, **tinst}, "twii": twii}


def cmd_fetch(root, start, end, sleep):
    st = load_state(root)
    mapping = industry_map(root)
    done = skipped = 0
    for d in weekdays(start, end):
        if os.path.exists(os.path.join(root, "history", "daily", f"{d}.csv.gz")) or d in st["holidays"]:
            skipped += 1
            continue
        kind, res = fetch_one_day(d, sleep)
        if kind == "holiday":
            st["holidays"].append(d)
            st["failed"].pop(d, None)
            log.info(f"{d} 休市")
        elif kind == "fail":
            st["failed"][d] = res
            log.warning(f"{d} 失敗：{res}（之後重跑會再試）")
        else:
            df = history.build_daily(d, res["day"], res["margin"], res["inst"], {}, {}, {}, {"TWSE", "TPEx"}, None)
            df["industry"] = df["code"].map(mapping).fillna("")
            p = os.path.join(root, "history", "daily", f"{d}.csv.gz")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            df.to_csv(p, index=False, compression={"method": "gzip", "mtime": 0})
            if res["twii"] is not None:
                history.upsert_index(root, d, res["twii"])
            st["failed"].pop(d, None)
            done += 1
            log.info(f"{d} OK（{len(df)} 檔）")
        save_state(root, st)
    print(f"新增 {done} 天，已存在或休市略過 {skipped} 天，失敗 {len(st['failed'])} 天 {sorted(st['failed'])[:5]}")


# ---------------- 月營收 ----------------

def _months(a, b):
    y, m = map(int, a.split("-"))
    ey, em = map(int, b.split("-"))
    while (y, m) <= (ey, em):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def fetch_mops(mkt, roc_y, m):
    r = requests.get(MOPS.format(mkt=mkt, y=roc_y, m=m), headers=UA, timeout=90)
    if r.status_code != 200:
        return []
    return pd.read_html(io.StringIO(r.content.decode("big5hkscs", errors="replace")), flavor="lxml")


def cmd_revenue(root, start, end, sleep):
    for y, m in _months(start, end):
        ym = f"{y}-{m:02d}"
        p = os.path.join(root, "history", "revenue", f"{ym}.csv.gz")
        if os.path.exists(p):
            log.info(f"{ym} 已存在，略過（當日版本有真實出表日期，不覆蓋）")
            continue
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        published = f"{ny}-{nm:02d}-10"      # 法定截止日當保守可見日
        rows = {}
        for mkt, market in (("sii", "TWSE"), ("otc", "TPEx")):
            time.sleep(sleep)
            try:
                tabs = fetch_mops(mkt, y - 1911, m)
            except Exception as e:  # noqa: BLE001
                log.warning(f"{ym} {mkt} 失敗：{e}")
                continue
            rows.update(N.mops_revenue_table(tabs, market, ym, published))
        if len(rows) < 500:
            log.warning(f"{ym} 只抓到 {len(rows)} 檔，不寫入")
            continue
        df = pd.DataFrame([{"code": c, **v} for c, v in rows.items()])
        df.to_csv(p, index=False, compression={"method": "gzip", "mtime": 0})
        log.info(f"{ym} 寫入 {len(df)} 檔（可見日 {published}）")


# ---------------- 重算 ----------------

def cmd_score(root, start, end, themes_draft=False):
    """themes_draft=True：themes.yaml 還沒 reviewed 也拿來預覽，theme_scores 每筆標 draft=True。"""
    rules = score.load_rules(root)
    tcfg = themes.load(root, allow_draft=themes_draft)
    shares = history.load_shares(root)
    sig_hist = {}                                   # 波段版要看最近 10 天每檔的訊號數
    ver = rules["version"]
    dates = [d for d in score.trading_dates(root) if start <= d <= end]
    all_dates = score.trading_dates(root)
    n_fri = 0
    for d in dates:
        df, summary = score.score_stocks(root, d, rules)
        if df.empty:
            continue
        i = all_dates.index(d)
        n = rules["industry"]["delta_trading_days"]
        prev = {}
        if i >= n:
            prev = {r["industry"]: r["score"] for r in ledger.read(root, "industry_scores", REPLAY_LEDGER)
                    if r["data_date"] == all_dates[i - n]}
        inds = score.score_industries(df, rules, prev)
        ledger.append(root, "industry_scores", [{"data_date": d, "rules_version": ver, **x} for x in inds],
                      key=("data_date", "industry"), base=REPLAY_LEDGER)
        universes = None
        for r in df.to_dict("records"):
            sig_hist[(d, r["code"])] = sum(1 for k in "ABCD" if r[k] is True)
        if tcfg:
            trows, strong_top = themes.score(df, tcfg, shares, rules)
            win = [x for x in all_dates[:i + 1] if x >= start][-rules["themes"]["swing"]["lookback_days"]:]
            swing_top = themes.swing(trows, df, sig_hist, win, rules)
            universes = {"strong_theme_top": strong_top, "strong_theme_swing_top": swing_top}
            ledger.append(root, "theme_scores", [{"data_date": d, "rules_version": ver, "draft": tcfg["draft"], **t}
                                                  for t in trows], key=("data_date", "theme"), base=REPLAY_LEDGER)
        if picks_mod.is_week_close(d):
            ps, rj = picks_mod.select(df, inds, rules, d, universes)
            ledger.append(root, "picks", [{"rules_version": ver, **p} for p in ps], key=("week", "code", "variant"), base=REPLAY_LEDGER)
            ledger.append(root, "pick_universe", [{"rules_version": ver, **r} for r in rj], key=("week", "code"), base=REPLAY_LEDGER)
            n_fri += 1
    if tcfg:
        rows = ledger.read(root, "theme_scores", REPLAY_LEDGER)
        ev = (themes.events_from_ledger(rows, "strong", "daily")
              + themes.events_from_ledger(rows, "swing_strong", "swing"))
        ledger.append(root, "theme_events", [{"rules_version": ver, **e} for e in ev],
                      key=("data_date", "theme", "kind"), base=REPLAY_LEDGER)
    added = grade.run(root, rules, all_dates[-1], base=REPLAY_LEDGER)
    s = replay_store.export(root, rules)          # 明細只留本機，摘要與最近 130 天主題紀錄寫進 replay/（進 git）
    print(f"已匯出摘要 replay/summary.json（{s['period']['from']} 到 {s['period']['to']}，分年度：{sorted(s['by_year'])}）")
    print(f"重算 {len(dates)} 天，其中 {n_fri} 個週五產生推薦；對答案新增 {added} 筆 → {REPLAY_LEDGER}/")


# ---------------- 驗證：帶日期版 vs 當日版 ----------------

def cmd_verify(root, d, sleep):
    live_path = os.path.join(root, "history", "daily", f"{d}.csv.gz")
    if not os.path.exists(live_path):
        raise SystemExit(f"{live_path} 不存在，請先對這一天跑過當日版（或先備份再比對）")
    live = pd.read_csv(live_path, dtype={"code": str}).set_index("code")
    kind, res = fetch_one_day(d, sleep)
    if kind != "ok":
        raise SystemExit(f"帶日期版抓取失敗：{kind} {res}")
    df = history.build_daily(d, res["day"], res["margin"], res["inst"], {}, {}, {}, {"TWSE", "TPEx"}, None).set_index("code")
    common = live.index.intersection(df.index)
    live_c = live.loc[[c for c in common if N.is_common_stock(c)]]
    out = {}
    for col in ["close", "volume", "turnover", "foreign_net", "trust_net", "margin_bal", "short_bal"]:
        a, b = live_c[col], df.loc[live_c.index, col]
        both = a.notna() & b.notna()
        same = ((a[both] - b[both]).abs() < 1e-6).mean() if both.any() else float("nan")
        out[col] = {"compared": int(both.sum()), "match": round(float(same), 4)}
    print(json.dumps({"date": d, "stocks_compared": int(len(live_c)),
                      "only_in_live": len(live.index.difference(df.index)),
                      "only_in_dated": len(df.index.difference(live.index)), "columns": out},
                     ensure_ascii=False, indent=1))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "revenue", "score", "verify"])
    ap.add_argument("--root", default=".")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--date")
    ap.add_argument("--sleep", type=float, default=2.5)
    ap.add_argument("--themes-draft", action="store_true", help="themes.yaml 未 reviewed 也用來預覽")
    a = ap.parse_args()
    if a.cmd == "fetch":
        cmd_fetch(a.root, a.start, a.end, a.sleep)
    elif a.cmd == "revenue":
        cmd_revenue(a.root, a.start, a.end, a.sleep)
    elif a.cmd == "score":
        cmd_score(a.root, a.start, a.end, a.themes_draft)
    else:
        cmd_verify(a.root, a.date, a.sleep)


if __name__ == "__main__":
    main()
