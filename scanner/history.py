"""把每天的統一資料寫進 history/，並做跨日連續性檢查。

history/
  daily/YYYY-MM-DD.csv.gz     每檔股票一列：價量、法人、融資融券、產業名稱
  revenue/YYYY-MM.csv.gz      每月營收（資料年月）；同月重跑會覆蓋，營收公布後才有的就是新檔
  tdcc/YYYY-MM-DD.csv.gz      集保股權分散（每週一份，檔名用集保的資料日期）

缺值一律是空白（讀回來是 NaN），不是 0。
"""
import os
import logging

import pandas as pd

from scanner import normalize as N

log = logging.getLogger("history")

DAILY_COLS = ["date", "code", "market", "name", "industry", "close", "open", "high", "low",
              "volume", "turnover", "foreign_net", "trust_net",
              "margin_bal", "margin_prev", "short_bal", "short_prev", "flag"]


def _path(root, *parts):
    p = os.path.join(root, "history", *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def build_daily(data_date, day, margin, inst, profile, rev, ind_map, inst_markets=(), flags=None):
    """inst_markets：三大法人來源當天抓成功的市場。交易所只列「有法人交易」的股票，
    所以成功抓到的市場裡沒列出的股票，法人買賣超是 0；來源沒抓成功的市場則留空。
    （實測：10/22 沒被列出的 118 檔普通股，成交金額最大也只有 262 萬元。）"""
    rows = []
    for code, d in day.items():
        m = margin.get(code, {})
        i = inst.get(code) or ({"foreign_net": 0, "trust_net": 0} if d["market"] in inst_markets else {})
        ind = (rev.get(code) or {}).get("industry") or ind_map.get((profile.get(code) or {}).get("industry_code"))
        rows.append({
            "date": data_date, "code": code, "market": d["market"], "name": d["name"],
            "industry": ind or "",
            "close": d["close"], "open": d["open"], "high": d["high"], "low": d["low"],
            "volume": d["volume"], "turnover": d["turnover"],
            "foreign_net": i.get("foreign_net"), "trust_net": i.get("trust_net"),
            "margin_bal": m.get("margin_bal"), "margin_prev": m.get("margin_prev"),
            "short_bal": m.get("short_bal"), "short_prev": m.get("short_prev"),
            "flag": (flags or {}).get(code, ""),
        })
    return pd.DataFrame(rows, columns=DAILY_COLS)


def margin_continuity(root, data_date, df):
    """今日的「前日餘額」應該等於上一個交易日存下來的「今日餘額」。
    對不上代表融資融券資料的日期可能和價量不同天（證交所 MI_MARGN 沒有日期欄位）。
    回傳 (比對檔數, 相符比例) 或 None（沒有上一天可比）。"""
    d = os.path.join(root, "history", "daily")
    prev_files = sorted(f for f in os.listdir(d) if f < f"{data_date}.csv.gz") if os.path.isdir(d) else []
    if not prev_files:
        return None
    prev = pd.read_csv(os.path.join(d, prev_files[-1]), dtype={"code": str})
    j = df.merge(prev[["code", "margin_bal"]], on="code", suffixes=("", "_yday")).dropna(
        subset=["margin_prev", "margin_bal_yday"])
    if j.empty:
        return None
    ok = (j["margin_prev"] == j["margin_bal_yday"]).mean()
    return {"compared": int(len(j)), "match_ratio": round(float(ok), 4), "prev_file": prev_files[-1]}


def write_all(root, data_date, day, margin, inst, profile, rev, ind_map, tdcc, inst_markets=(), flags=None):
    out = {}
    df = build_daily(data_date, day, margin, inst, profile, rev, ind_map, inst_markets, flags)
    out["daily_rows"] = len(df)
    out["margin_continuity"] = margin_continuity(root, data_date, df)
    cont = out["margin_continuity"]
    if cont and cont["match_ratio"] < 0.95:
        log.warning(f"融資餘額連續性只有 {cont['match_ratio']:.1%}，融資融券資料日期可能和價量不同天")
        for c in ("margin_bal", "margin_prev", "short_bal", "short_prev"):
            df[c] = None
        out["margin_dropped"] = True
    df.to_csv(_path(root, "daily", f"{data_date}.csv.gz"), index=False, compression={"method": "gzip", "mtime": 0})

    # 月營收：依資料年月存；同一個月只保留最新抓到的版本
    by_ym = {}
    for code, r in rev.items():
        if r.get("ym"):
            by_ym.setdefault(r["ym"], []).append({"code": code, **r})
    for ym, rows in by_ym.items():
        pd.DataFrame(rows).to_csv(_path(root, "revenue", f"{ym}.csv.gz"), index=False, compression={"method": "gzip", "mtime": 0})
    out["revenue_months"] = sorted(by_ym)

    if tdcc:
        tdate = next(iter(tdcc.values()))["date"]
        tdf = pd.DataFrame([{"code": c, **v} for c, v in tdcc.items()])
        tdf.to_csv(_path(root, "tdcc", f"{tdate}.csv.gz"), index=False, compression={"method": "gzip", "mtime": 0})
        out["tdcc_date"] = tdate
    return out


def upsert_index(root, data_date, close):
    """history/index/twii.csv：加權指數收盤，一天一列，同一天重跑會覆蓋。"""
    p = _path(root, "index", "twii.csv")
    rows = {}
    if os.path.exists(p):
        old = pd.read_csv(p, dtype={"date": str})
        rows = dict(zip(old["date"], old["close"]))
    rows[data_date] = close
    pd.DataFrame(sorted(rows.items()), columns=["date", "close"]).to_csv(p, index=False)


def upsert_shares(root, profile, as_of):
    """history/shares.csv：各股發行股數（算市值排名用）。每次抓到基本資料就更新。
    只存最新值，所以回放時歷史市值 = 歷史收盤價 × 現在的股數；遇到分割、減資、增資的股票會有誤差。"""
    p = _path(root, "shares.csv")
    rows = {}
    if os.path.exists(p):
        old = pd.read_csv(p, dtype={"code": str})
        rows = {c: (s, a) for c, s, a in zip(old["code"], old["issued_shares"], old["as_of"])}
    for code, v in profile.items():
        if v.get("issued_shares"):
            rows[code] = (v["issued_shares"], as_of)
    pd.DataFrame([(c, s, a) for c, (s, a) in sorted(rows.items())],
                 columns=["code", "issued_shares", "as_of"]).to_csv(p, index=False)


def load_shares(root):
    p = os.path.join(root, "history", "shares.csv")
    if not os.path.exists(p):
        return {}
    df = pd.read_csv(p, dtype={"code": str})
    return dict(zip(df["code"], df["issued_shares"]))
