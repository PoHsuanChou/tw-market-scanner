"""準確率與推薦追蹤：給網站「主題」「推薦」兩頁顯示，每天跑完掃描後重算。

輸出 out/accuracy.json：
  themes        波段強／不強的主題，之後 5／20／60／120 個交易日，前 3 大報酬中位數的超額
  theme_events  波段版「轉強當天」之後的表現
  picks         各推薦版本（只看每版前 3 名）的表現
  picks_list    每一筆「真實」推薦到今天為止的報酬（不必等到期）
每一項都分 real（2026-10-22 起的真實紀錄）與 replay（回放，名單事後挑選、偏樂觀，只供參考）。

「超額」= 該股票（或主題前 3 大的中位數）報酬 − 同一天 focus 範圍電子股報酬的中位數。
看中位數與勝率（超額 > 0 的比例），不看平均；n < 30 的格子網站會標「樣本不足」。
"""
import os
import json
from datetime import datetime

import pandas as pd

from scanner import ledger, score, grade


def stats(values):
    v = [x for x in values if x is not None and x == x]
    if not v:
        return {"n": 0, "median": None, "win": None}
    s = pd.Series(v)
    return {"n": int(len(s)), "median": round(float(s.median()), 2), "win": round(float((s > 0).mean()), 3)}


def _by(rows, horizons, key=None):
    out = {}
    for h in horizons:
        hr = [r for r in rows if r["horizon"] == h]
        if key is None:
            out[str(h)] = stats([r.get("excess") for r in hr])
        else:
            for k in sorted({str(key(r)) for r in hr}):
                out.setdefault(k, {})[str(h)] = stats([r.get("excess") for r in hr if str(key(r)) == k])
    return out


def summarize(outcomes, horizons):
    th = [r for r in outcomes if r["ref_type"] == "theme"]
    ev = [r for r in outcomes if r["ref_type"] == "theme_event" and r.get("kind") == "swing"]
    pk = [r for r in outcomes if r["ref_type"] == "pick" and (r.get("rank") or 99) <= 3]
    return {
        "themes": _by([r for r in th if r.get("swing_strong") is not None], horizons,
                      key=lambda r: "強" if r["swing_strong"] else "不強"),
        "theme_events": _by(ev, horizons),
        "picks": _by(pk, horizons, key=lambda r: r.get("variant", "base")),
    }


def picks_to_date(root, rules, data_date):
    """真實推薦到今天為止的報酬與超額（以推薦當天收盤價為基準）。"""
    picks = ledger.read(root, "picks")
    if not picks:
        return []
    dates, close, common = grade._closes(root, data_date)
    turnover = common.pivot(index="date", columns="code", values="turnover").sort_index()
    last = dates[-1]
    done = {(r["ref_id"], r["horizon"]): r for r in ledger.read(root, "outcomes") if r["ref_type"] == "pick"}
    out = []
    for p in sorted(picks, key=lambda r: (r["data_date"], r.get("variant", ""), r["rank"]), reverse=True):
        d0, code = p["data_date"], p["code"]
        row = {k: p.get(k) for k in ("pick_id", "week", "data_date", "code", "name", "industry", "variant",
                                     "sent", "rank", "price_at_pick", "themes")}
        row.update(last_date=last, days_held=None, ret_to_date=None, excess_to_date=None, last_close=None)
        if d0 in close.index and code in close.columns and d0 <= last:
            ret, delisted = grade._ret(close, code, d0, last)
            bench = grade._window_rets(close, grade.focus_codes(common, turnover, d0, rules), d0, last)
            row["days_held"] = dates.index(last) - dates.index(d0)
            row["ret_to_date"] = None if ret is None else round(ret, 2)
            if ret is not None and len(bench) >= rules["grade"].get("min_bench_stocks", 30):
                row["excess_to_date"] = round(ret - float(bench.median()), 2)
            lc = close[code].loc[:last].dropna()
            row["last_close"] = float(lc.iloc[-1]) if len(lc) else None
            row["delisted"] = delisted
        row["outcomes"] = {str(h): (done.get((p["pick_id"], h)) or {}).get("excess")
                           for h in rules["grade"]["horizons"]}
        out.append(row)
    return out


def build(root, rules, data_date):
    hz = rules["grade"]["horizons"]
    acc = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "data_date": data_date, "rules_version": rules["version"], "horizons": hz,
        "real": summarize(ledger.read(root, "outcomes"), hz),
        "replay": summarize(ledger.read(root, "outcomes", "ledger_replay"), hz),
        "picks_list": picks_to_date(root, rules, data_date),
    }
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    with open(os.path.join(root, "out", "accuracy.json"), "w", encoding="utf-8") as f:
        json.dump(acc, f, ensure_ascii=False, separators=(",", ":"))
    return acc
