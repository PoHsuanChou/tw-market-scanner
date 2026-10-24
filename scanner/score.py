"""個股四類訊號（A 價量、B 法人、C 大戶、D 基本面）與產業評分。

規則的 id 與規格對照：
  A.rs_and_ma / B.inst_5d / C.tdcc_big / C.margin_proxy / D.revenue / U.liquidity / U.focus

每一類回傳 True / False / None。None = 資料不足，不算分、也不算失敗。
歷史不足時一律回 None，不用現有幾天硬湊。
"""
import os
import re
import glob
import statistics
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yaml

COMMON = re.compile(r"[1-9]\d{3}")


def load_rules(root):
    with open(os.path.join(root, "config", "rules.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _files(root, sub, upto=None, suffix=".csv.gz"):
    d = os.path.join(root, "history", sub)
    if not os.path.isdir(d):
        return []
    names = sorted(f[: -len(suffix)] for f in os.listdir(d) if f.endswith(suffix))
    return [n for n in names if upto is None or n <= upto]


def trading_dates(root, upto=None):
    return _files(root, "daily", upto)


def load_daily(root, upto, n=70):
    dates = trading_dates(root, upto)[-n:]
    frames = [pd.read_csv(os.path.join(root, "history", "daily", f"{d}.csv.gz"), dtype={"code": str})
              for d in dates]
    return dates, (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame())


def index_series(root, upto=None):
    """history/index/twii.csv：加權指數收盤。回傳 pandas Series（index=日期字串）。"""
    p = os.path.join(root, "history", "index", "twii.csv")
    if not os.path.exists(p):
        return pd.Series(dtype=float)
    df = pd.read_csv(p, dtype={"date": str}).dropna().sort_values("date")
    if upto:
        df = df[df["date"] <= upto]
    return df.set_index("date")["close"]


def _norm_industry(name, alias):
    name = name if isinstance(name, str) else ""
    return alias.get(name, name)


# ---------------- 各類訊號 ----------------

def _r20(close, w=20):
    if len(close) < w + 1 or close.iloc[-1] != close.iloc[-1] or close.iloc[-w - 1] != close.iloc[-w - 1]:
        return None
    return float(close.iloc[-1] / close.iloc[-w - 1] - 1) * 100


def signal_A(close, bench_r20, rules):
    w = rules["signals"]["A"]["ma_window"]
    r = _r20(close, rules["signals"]["A"]["return_window"])
    if r is None or bench_r20 is None or close.iloc[-w:].isna().any():
        return None, r
    return bool(close.iloc[-1] > close.iloc[-w:].mean() and r > bench_r20), r


def signal_B(foreign, trust, rules):
    w = rules["signals"]["B"]["window"]
    if len(foreign) < w:
        return None, None, None
    f, t = foreign.iloc[-w:], trust.iloc[-w:]
    if f.isna().any() or t.isna().any():
        return None, None, None
    f5, t5 = float(f.sum()), float(t.sum())
    return bool(f5 + t5 > 0 and t5 >= 0), f5, t5


def _tdcc_pair(root, data_date, rules):
    """回傳 (最新一週, 約 4 週前) 的 DataFrame；找不到對應的舊檔就回 (最新, None)。"""
    names = _files(root, "tdcc", data_date)
    if not names:
        return None, None
    cur = names[-1]
    target = date.fromisoformat(cur) - timedelta(weeks=rules["signals"]["C"]["tdcc_weeks_back"])
    tol = rules["signals"]["C"]["tdcc_tolerance_days"]
    best = None
    for n in names[:-1]:
        gap = abs((date.fromisoformat(n) - target).days)
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, n)
    read = lambda n: pd.read_csv(os.path.join(root, "history", "tdcc", f"{n}.csv.gz"), dtype={"code": str}).set_index("code")
    return read(cur), (read(best[1]) if best else None)


def signal_C(code, tdcc_now, tdcc_old, margin_bal, r20, rules):
    """回傳 (True/False/None, 依據, 細節)。"""
    if tdcc_now is not None and tdcc_old is not None and code in tdcc_now.index and code in tdcc_old.index:
        a, b = tdcc_now.loc[code], tdcc_old.loc[code]
        up = a["big400_pct"] > b["big400_pct"]
        fewer = a["holders"] < b["holders"]
        return bool(up and fewer), "tdcc", {"big400_now": float(a["big400_pct"]), "big400_old": float(b["big400_pct"])}
    w = rules["signals"]["C"]["proxy_window"]
    if len(margin_bal) > w and not margin_bal.iloc[-w - 1:].isna().any() and r20 is not None:
        change = float(margin_bal.iloc[-1] - margin_bal.iloc[-w - 1])
        return bool(change < 0 and r20 > 0), "margin_proxy", {"margin_change_5d": change}
    return None, None, {}


def _revenue_frames(root, data_date):
    """每個月營收檔，只保留「出表日期 ≤ 資料日期」的列，避免偷看未來才公布的營收。"""
    out = {}
    for ym in _files(root, "revenue"):
        df = pd.read_csv(os.path.join(root, "history", "revenue", f"{ym}.csv.gz"), dtype={"code": str})
        df = df[df["published"].fillna("9999-12-31") <= data_date]
        if len(df):
            out[ym] = df.set_index("code")
    return out


def signal_D(code, rev_by_ym, rules):
    """回傳 (True/False/None, 依據, 細節)。"""
    yms = [ym for ym in sorted(rev_by_ym) if code in rev_by_ym[ym].index]
    if not yms:
        return None, None, {}
    cur = rev_by_ym[yms[-1]].loc[code]
    yoy = cur["yoy"]
    if yoy != yoy:
        return None, None, {}
    prev = None
    if len(yms) >= 2:
        py = rev_by_ym[yms[-2]].loc[code]["yoy"]
        prev = None if py != py else float(py)
    if prev is not None:
        accel, basis, ref = yoy > prev, "prev_month_yoy", prev
    else:
        ytd = cur["ytd_yoy"]
        if ytd != ytd:
            return None, None, {}
        accel, basis, ref = yoy > ytd, "ytd_yoy_proxy", float(ytd)
    ok = bool(yoy >= rules["signals"]["D"]["min_yoy"] and accel)
    return ok, basis, {"ym": yms[-1], "yoy": float(yoy), "ref_yoy": ref}


# ---------------- 個股彙總 ----------------

def score_stocks(root, data_date, rules=None):
    """對資料日期當天的 focus 普通股逐檔打分。回傳 (DataFrame, 摘要 dict)。"""
    rules = rules or load_rules(root)
    dates, hist = load_daily(root, data_date)
    if hist.empty or dates[-1] != data_date:
        raise ValueError(f"history 裡沒有 {data_date}")
    alias = rules["universe"]["industry_alias"]
    hist["industry"] = hist["industry"].map(lambda x: _norm_industry(x, alias))
    common = hist[hist["code"].str.fullmatch(COMMON)]
    close = common.pivot(index="date", columns="code", values="close").sort_index()
    foreign = common.pivot(index="date", columns="code", values="foreign_net").sort_index()
    trust = common.pivot(index="date", columns="code", values="trust_net").sort_index()
    margin = common.pivot(index="date", columns="code", values="margin_bal").sort_index()
    turnover = common.pivot(index="date", columns="code", values="turnover").sort_index()

    w = rules["signals"]["A"]["return_window"]
    idx = index_series(root, data_date)
    if len(idx) > w and idx.index[-1] == data_date:
        bench, bench_name = float(idx.iloc[-1] / idx.iloc[-w - 1] - 1) * 100, "twii"
    else:
        all_r20 = [r for r in (_r20(close[c], w) for c in close.columns) if r is not None]
        bench = statistics.median(all_r20) if len(all_r20) > 200 else None
        bench_name = "median_all_common" if bench is not None else None

    today = common[common["date"] == data_date].set_index("code")
    focus = set(rules["universe"]["focus_industries"])
    tdcc_now, tdcc_old = _tdcc_pair(root, data_date, rules)
    rev = _revenue_frames(root, data_date)
    min_turn = rules["universe"]["min_avg_turnover_billion"] * 1e8

    rows = []
    for code, t in today.iterrows():
        if t["industry"] not in focus or t["close"] != t["close"]:
            continue
        avg_turn = float(turnover[code].iloc[-20:].mean())
        if avg_turn != avg_turn or avg_turn < min_turn:
            continue
        A, r20 = signal_A(close[code], bench, rules)
        B, f5, t5 = signal_B(foreign[code], trust[code], rules)
        C, c_basis, c_info = signal_C(code, tdcc_now, tdcc_old, margin[code], r20, rules)
        D, d_basis, d_info = signal_D(code, rev, rules)
        m = margin[code]
        mg5 = None
        if len(m) > 5 and m.iloc[-6] == m.iloc[-6] and m.iloc[-1] == m.iloc[-1] and m.iloc[-6] > 0:
            mg5 = float(m.iloc[-1] / m.iloc[-6] - 1) * 100
        rows.append({
            "code": code, "name": t["name"], "market": t["market"], "industry": t["industry"],
            "close": float(t["close"]), "avg_turnover": avg_turn, "flag": t["flag"] if isinstance(t["flag"], str) else "",
            "A": A, "B": B, "C": C, "D": D, "c_basis": c_basis, "d_basis": d_basis,
            "r20": r20, "bench_r20": bench, "foreign_5d": f5, "trust_5d": t5, "margin_growth_5d": mg5,
            **{f"c_{k}": v for k, v in c_info.items()}, **{f"d_{k}": v for k, v in d_info.items()},
        })
    df = pd.DataFrame(rows)
    summary = {"data_date": data_date, "history_days": len(dates), "benchmark_r20": bench, "benchmark": bench_name,
               "stocks": len(df), "tdcc_pair": tdcc_old is not None}
    return df, summary


# ---------------- 產業評分 ----------------

def _cat(series, rules):
    known = series.dropna()
    if len(known) < rules["industry"]["min_known_stocks"]:
        return None
    return bool(known.astype(bool).mean() >= rules["industry"]["member_share"])


def score_industries(df, rules, prev_scores=None):
    """prev_scores：{industry: score}，約 20 個交易日前的分數，用來算 delta_4w。"""
    out = []
    for ind, g in df.groupby("industry"):
        cats = {k: _cat(g[k].map(lambda x: None if x != x else x), rules) for k in "ABCD"}
        score = sum(1 for v in cats.values() if v)
        prev = (prev_scores or {}).get(ind)
        out.append({"industry": ind, "n_stocks": int(len(g)), **cats, "score": score,
                    "known": sum(1 for v in cats.values() if v is not None),
                    "delta_4w": (score - prev) if prev is not None else None})
    return sorted(out, key=lambda r: (-r["score"], r["industry"]))
