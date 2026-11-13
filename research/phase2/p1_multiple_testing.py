"""Phase 1：多重檢定 Audit（plan.md §3）。

登記表：把到目前為止看過的檢定（F1～F5、遺失的兩個）和第二階段的兩個主要檢定（P）都列出來，
用同一套方法重算原始單尾 p，再做 Benjamini-Hochberg（全部一起、各族群內）與 Benjamini-Yekutieli。
H1、H2 另外做 stationary block bootstrap（10,000 次）與 White's Reality Check。

各族群的 p 怎麼算（都是單尾，方向：國外強 → 台灣強）：
  F1 週誰先動（原始設定）：Pearson r → t = r√((n−2)/(1−r²))，df n−2（週與週之間當獨立，和原始一樣）
  F2 月資料（原始設定，重現腳本）：每段 IC 序列 → Newey-West t；另列原始報告用的 pooled OLS t
  F3 美股 ETF、F4 國外強度標籤：區塊 = 該狀態的超額 vs「平常」的超額，Welch t；事件 = 事件超額 vs 全部區塊，Welch t；
     合計（以日期為單位的差距序列）→ Newey-West t
  F5 SMH→AI 起算日：每個起算日 Welch t
  P  第二階段主要檢定（B2、B4 修正後、全部成員等權）：H1 每段 IC → NW t；H2 每個決策日差距 → NW t
樣本不足（任一邊 < 2 筆）的格子 p 記 NaN，不參與校正，但保留在表上。

用法：python research/phase2/p1_multiple_testing.py
輸出：research/phase2/out/test_registry.csv、mt_summary.csv、mt_meta.json
"""
import os
import sys
import json

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
from common import RU, RT, theme_peers, themes, history, score  # noqa: E402

sys.path.insert(0, os.path.join(C.ROOT, "research", "audit"))
import repro_monthly_leadlag as REPRO  # noqa: E402
import sensitivity_monthly as SENS  # noqa: E402

ORIG_3Y_START = "2023-11-13"   # 第一階段的月資料是 2026-11-13 用 period=3y 抓的，從這天開始；重現時把 4 年資料裁到這裡


def trim(closes, start):
    return {t: [p for p in v if p[0] >= start] for t, v in closes.items()}


def to_series(closes):
    return {t: pd.Series([p[1] for p in v], index=[p[0] for p in v], dtype=float) for t, v in closes.items()}


def row(family, test, stat, n, p, note="", **kw):
    return {"family": family, "test": test, "stat": stat, "n": n, "p_raw": p, "note": note, **kw}


# ---------------- F1 ----------------

def f1(closes, tw):
    peers = theme_peers.load(C.ROOT)
    tww = theme_peers.tw_weekly(C.ROOT, themes.load(C.ROOT), history.load_shares(C.ROOT))
    fw = theme_peers.foreign_weekly(trim(closes, ORIG_3Y_START), peers, tw.dates)
    out = []
    for th in peers:
        if th not in tww or th not in fw:
            continue
        w = pd.period_range(min(tww[th].index.min(), fw[th].index.min()), max(tww[th].index.max(), fw[th].index.max()), freq="W-FRI")
        f, t = fw[th].reindex(w), tww[th].reindex(w)
        for k in (-4, -3, -2, -1, 1, 2, 3, 4):
            j = pd.DataFrame({"f": f.shift(k), "t": t}).dropna()
            r = float(j.f.corr(j.t)) if len(j) > 3 else np.nan
            out.append(row("F1 週誰先動", f"{th}｜錯開 {k:+d} 週", r, len(j), C.corr_p(r, len(j)),
                           "k>0 國外先動；k=±3、±4 是當時看過後捨棄的"))
    return out


# ---------------- F2 ----------------

def f2(closes):
    c3 = trim(closes, ORIG_3Y_START)
    out = []
    d = REPRO.panel(c3)
    s = REPRO.stats(d)
    out.append(row("F2 月資料", "原始報告：pooled OLS 國外係數（控制台灣）", s["ols_beta_f"], s["obs"],
                   C.p_one_sided(s["ols_t_f"], s["obs"] - 3), "原始報告用的 t（把 825 筆當獨立，已知高估）"))
    for off in range(4):
        po = SENS.panel_offset(c3, off)
        ic = po.groupby("w").apply(lambda g: g.fp_r.corr(g.tn_r), include_groups=False)
        r = C.nw(ic)
        out.append(row("F2 月資料", f"原始設定 IC｜起點 +{off} 週", r["mean"], r["n"], r["p"], "每段 IC → NW t"))
        fm = SENS.fama_macbeth(po)["f"]
        out.append(row("F2 月資料", f"原始設定 控制台灣後國外係數｜起點 +{off} 週", fm["beta"], r["n"],
                       C.p_one_sided(fm["t_nw"], r["n"] - 1), "Fama-MacBeth + NW"))
    return out


# ---------------- F3、F4、F5 ----------------

def _cells(family, name, st, exc, hs_by_h, states, base_states, cost):
    """一組配對／主題的區塊與事件檢定。st：狀態 Series；exc：{h: 超額 Series}。"""
    out = []
    for h, e in exc.items():
        b = RU.blocks(st, e, h, 20)
        allv = [v for _, _, v in b]
        for s in states:
            on = [v for _, x, v in b if x == s]
            off = [v for _, x, v in b if x == base_states]
            w = C.welch(on, off)
            out.append(row(family, f"{name}｜區塊｜{s}｜{h} 日", w["diff"], w["n_a"], w["p"], f"vs 平常 n={w['n_b']}"))
            ev = [v for _, _, v in RU.events(st, e, h, s)]
            w = C.welch(ev, allv)
            out.append(row(family, f"{name}｜事件｜{s}｜{h} 日", w["diff"], w["n_a"], w["p"], f"vs 全部區塊 n={w['n_b']}"))
    return out


def f3(us_closes, tw):
    cfg = RU.load(C.ROOT)
    us = to_series(us_closes)
    states = RU.us_states(us, cfg, tw.dates)
    liquid = tw.avg_turn >= cfg["min_avg_turnover_billion"] * 1e8
    out, per_date, rc_data = [], {}, {}
    for p in cfg["pairs"]:
        st = states.get((p["us"], p.get("kind", "etf")))
        codes = [c for c in RU.members(p, tw.ind, C.ROOT) if c in tw.close.columns]
        ok = tw.close[codes].notna()
        if p["tw"].get("liquidity", True):
            ok &= liquid[codes]
        idx = RU.basket_index(tw.close[codes], ok)
        exc = {h: RU.fwd(idx, h) - RU.fwd(tw.bench_ew, h) for h in cfg["hold"]}
        out += _cells("F3 美股 ETF", p["name"], st, exc, None, ("在漲", "抗跌"), "平常", cfg["cost_pct"])
        for h in cfg["hold"]:
            for d, s, v in RU.blocks(st, exc[h], h, 20):
                per_date.setdefault(h, {}).setdefault(d, {}).setdefault(s, []).append(v)
        rc_data[p["name"]] = [(d, s, v) for d, s, v in RU.blocks(st, exc[20], 20, 20)]
    for h, byd in per_date.items():
        for s in ("在漲", "抗跌"):
            ser = pd.Series({d: np.mean(v[s]) - np.mean(v["平常"]) for d, v in byd.items() if s in v and "平常" in v}, dtype=float)
            r = C.nw(ser)
            out.append(row("F3 美股 ETF", f"合計｜{s}｜{h} 日", r["mean"], r["n"], r["p"], "以日期為單位 → NW t"))
    return out, rc_data


def f4(closes, tw):
    peers = theme_peers.load(C.ROOT)
    cfg = RU.load(C.ROOT)
    labels = C.h2_labels(closes, peers, tw.dates)
    out, per_date, rc_data = [], {}, {}
    for th, st in labels.items():
        idx = tw.theme_index(th, "top3")                       # 原始 replay_theme_peers：前 3 大
        if idx is None:
            continue
        exc = {h: RU.fwd(idx, h) - RU.fwd(tw.bench_ew, h) for h in cfg["hold"]}
        out += _cells("F4 國外強度標籤", th, st, exc, None, ("國外在動", "國外抗跌"), "平常", cfg["cost_pct"])
        for h in cfg["hold"]:
            for d, s, v in RU.blocks(st, exc[h], h, 20):
                per_date.setdefault(h, {}).setdefault(d, {}).setdefault(s, []).append(v)
        rc_data[th] = [(d, s, v) for d, s, v in RU.blocks(st, exc[20], 20, 20)]
    for h, byd in per_date.items():
        for s in ("國外在動", "國外抗跌"):
            ser = pd.Series({d: np.mean(v[s]) - np.mean(v["平常"]) for d, v in byd.items() if s in v and "平常" in v}, dtype=float)
            r = C.nw(ser)
            out.append(row("F4 國外強度標籤", f"合計｜{s}｜{h} 日", r["mean"], r["n"], r["p"],
                           "以日期為單位 → NW t（20 日、國外在動＝第一階段的 +2.27）"))
    return out, rc_data, labels


def f5(us_closes, tw):
    cfg = RU.load(C.ROOT)
    st = RU.us_states(to_series(us_closes), cfg, tw.dates)[("SMH", "etf")]
    liquid = tw.avg_turn >= cfg["min_avg_turnover_billion"] * 1e8
    p = next(x for x in cfg["pairs"] if x["name"].startswith("半導體→"))
    codes = [c for c in RU.members(p, tw.ind, C.ROOT) if c in tw.close.columns]
    idx = RU.basket_index(tw.close[codes], tw.close[codes].notna() & liquid[codes])
    e = RU.fwd(idx, 20) - RU.fwd(tw.bench_ew, 20)
    out = []
    for off in range(20):
        b = RU.blocks(st, e, 20, 20 + off)
        w = C.welch([v for _, s, v in b if s == "在漲"], [v for _, s, v in b if s == "平常"])
        out.append(row("F5 SMH→AI 起算日", f"起算日 +{off}", w["diff"], w["n_a"], w["p"], f"vs 平常 n={w['n_b']}"))
    return out


# ---------------- 第二階段主要檢定 ----------------

def primary(closes, tw, labels):
    peers = theme_peers.load(C.ROOT)
    fx = C.foreign_daily(closes, tw.dates)                                 # B2 修正
    h1 = C.h1_panel(tw.daily, fx, tw.members, peers, C.bounds_weekly(tw.dates))
    ic = C.ic_series(h1)
    idx = {th: tw.theme_index(th, "all_ew") for th in labels}
    rec = C.h2_records(tw, labels, idx, skip=1)                             # B4 修正：d+1 收盤進場
    diff = C.h2_diff(rec)
    r1, r2 = C.nw(ic), C.nw(diff)
    rows = [row("P 第二階段主要", "H1：國外過去 4 週排名 → 台灣下 4 週（IC）", r1["mean"], r1["n"], r1["p"],
                "B2 修正、全部成員等權、每 4 週、起點 0"),
            row("P 第二階段主要", "H2：國外在動 − 平常（20 日、d+1 進場）", r2["mean"], r2["n"], r2["p"],
                "B4 修正、全部成員等權、起算日 0")]
    return rows, ic, diff, h1


def rc_states(data, on, off="平常", min_on=3):
    """data：{策略: [(日期, 狀態, 超額)]}（同一組區塊日期）。回傳 reality_check 結果。"""
    dates = sorted({d for v in data.values() for d, _, _ in v})
    names = [k for k, v in data.items() if sum(1 for _, s, _ in v if s == on) >= min_on]
    E = np.full((len(dates), len(names)), np.nan)
    S = np.full((len(dates), len(names)), np.nan)
    pos = {d: i for i, d in enumerate(dates)}
    for j, k in enumerate(names):
        for d, s, v in data[k]:
            E[pos[d], j] = v
            S[pos[d], j] = 1 if s == on else (0 if s == off else np.nan)

    def stat(ix):
        e, s = E[ix], S[ix]
        with np.errstate(invalid="ignore"):
            a = np.nanmean(np.where(s == 1, e, np.nan), axis=0)
            b = np.nanmean(np.where(s == 0, e, np.nan), axis=0)
        return a - b
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        res = C.reality_check(stat, len(dates), reps=2000, block=3)
    res["strategies"] = names
    res["best_name"] = names[res["best"]]
    return res


def main():
    tw = C.TW()
    peers = theme_peers.load(C.ROOT)
    closes = C.cached_yahoo("peers_4y", [t for v in peers.values() for t in v])
    cfg = RU.load(C.ROOT)
    us_closes = C.cached_yahoo("us_etf_4y", sorted(set(cfg["universe"]) | {p["us"] for p in cfg["pairs"]}))

    rows = f1(closes, tw) + f2(closes)
    r3, rc3 = f3(us_closes, tw)
    r4, rc4, labels = f4(closes, tw)
    rows += r3 + r4 + f5(us_closes, tw)
    rows += [row("F6 遺失", "2026-10-27 原始國外強度回測", None, None, np.nan, "程式遺失，檢定數未知，無法納入"),
             row("F6 遺失", "跌深＋高殖利率", None, None, np.nan, "docs/status.md 只有文字，無法納入")]
    prim, ic, diff, h1 = primary(closes, tw, labels)
    rows += prim

    reg = pd.DataFrame(rows)
    reg["q_bh_all"] = C.bh(reg["p_raw"])
    reg["q_by_all"] = C.bh(reg["p_raw"], dependent=True)
    reg["q_bh_family"] = np.nan
    for fam, g in reg.groupby("family"):
        reg.loc[g.index, "q_bh_family"] = C.bh(g["p_raw"])
    C.save_out(reg, "test_registry.csv")

    # H1、H2：bootstrap 與 Reality Check
    ic_lags = {}
    fx = C.foreign_daily(closes, tw.dates)
    for lag in (1, 2, 3, 4):
        ic_lags[lag] = C.ic_series(C.h1_panel(tw.daily, fx, tw.members, peers, C.bounds_weekly(tw.dates), lag=lag))
    M = pd.DataFrame(ic_lags)
    rc_h1 = C.reality_check(lambda ix: np.nanmean(M.to_numpy()[ix], axis=0), len(M), reps=2000, block=3)
    rc_f3 = rc_states(rc3, "在漲")
    rc_f4 = rc_states(rc4, "國外在動")
    p_rows = reg[reg.family == "P 第二階段主要"]
    summ = []
    for (_, r), ser, rc, rcname in zip(p_rows.iterrows(), (ic, diff), (rc_h1, rc_f4),
                                       ("H1：錯開 1～4 段的 4 個版本裡挑最好", "F4：25 個主題（國外在動、20 日、前 3 大）裡挑最好")):
        summ.append({"hypothesis": r["test"], "mean": r["stat"], "n": r["n"], "p_raw_nw": r["p_raw"],
                     "q_bh_all": r["q_bh_all"], "q_by_all": r["q_by_all"], "q_bh_family": r["q_bh_family"],
                     "p_boot": C.boot_p_mean(ser, reps=10000, block=3), "rc_family": rcname, "p_rc": rc["p"],
                     "rc_best": rc.get("best_name", f"錯開 {rc['best'] + 1} 段")})
    summ.append({"hypothesis": "F3：11 組美股 ETF 配對（在漲、20 日）裡挑最好", "rc_family": "F3", "p_rc": rc_f3["p"],
                 "rc_best": rc_f3["best_name"]})
    C.save_out(pd.DataFrame(summ), "mt_summary.csv")
    meta = {"n_tests": int(len(reg)), "n_with_p": int(reg.p_raw.notna().sum()),
            "by_family": reg.groupby("family").agg(n=("test", "size"), with_p=("p_raw", lambda s: int(s.notna().sum())),
                                                     raw_lt_05=("p_raw", lambda s: int((s < 0.05).sum())),
                                                     bh_lt_05=("q_bh_all", lambda s: int((s < 0.05).sum()))).reset_index().to_dict("records"),
            "rc_h1": rc_h1, "rc_f3": {k: v for k, v in rc_f3.items() if k != "T"}, "rc_f4": {k: v for k, v in rc_f4.items() if k != "T"},
            "h1_lag_ic_means": {str(k): float(np.nanmean(v)) for k, v in ic_lags.items()}}
    with open(os.path.join(C.OUT, "mt_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1, default=float)
    print(pd.DataFrame(summ).to_string())
    print(pd.DataFrame(meta["by_family"]).to_string())


if __name__ == "__main__":
    main()
