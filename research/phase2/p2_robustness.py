"""Phase 2：穩健性（plan.md §4）。H1、H2 的每一種變形都列出來，不挑。

H1 主要版本：B2 修正、全部成員等權、每 4 週（週五切）、起點 0、d 日收盤進場；統計量 = 每段 IC。
H2 主要版本：國外強度原始標籤、全部成員等權、d+1 收盤進場、每 20 日、起算日 0、基準全市場等權；統計量 = 每個決策日的「國外在動 − 平常」。
每列：n、平均、中位數、NW t、單尾 p、序列為正的比例。

用法：python research/phase2/p2_robustness.py
輸出：research/phase2/out/robustness_h1.csv、robustness_h2.csv、robustness_h2_contrib.csv、robustness_checks.json
"""
import os
import sys
import json

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
from common import theme_peers  # noqa: E402

STARTS = ["2023-10-01", "2024-01-01", "2024-04-01", "2024-07-01", "2025-01-01"]


def summ(group, name, ser, **extra):
    r = C.nw(ser)
    s = pd.Series(ser).dropna()
    return {"group": group, "variant": name, "n": r["n"], "mean": r["mean"], "median": r["median"], "t_nw": r["t"],
            "p": r["p"], "pos_share": float((s > 0).mean()) if len(s) else None, **extra}


def regime_rows(prefix, ser, twii, dates_pos):
    out = []
    for ma, thr, tag in ((120, 0.05, "主要"), (60, 0.05, "均線 60"), (200, 0.05, "均線 200"), (120, 0.03, "門檻 3%"), (120, 0.08, "門檻 8%")):
        reg = C.regimes(twii, ma, thr)
        lab = pd.Series({d: reg.iloc[dates_pos[d]] for d in ser.index})
        for g in ("Bull", "Sideways", "Bear", "NA"):
            out.append(summ(f"{prefix}行情（{tag}）", g, ser[lab == g]))
    return out


def extremes(prefix, ser, mkt):
    out = []
    lo, hi = mkt.quantile(0.05), mkt.quantile(0.95)
    keep = (mkt >= lo) & (mkt <= hi)
    out.append(summ(prefix + "排除極端", "拿掉大盤最高、最低各 5% 的期間", ser[keep.reindex(ser.index).fillna(False)]))
    ts = [C.nw(ser.drop(d))["t"] for d in ser.index]
    out.append(summ(prefix + "排除極端", f"一次拿掉一期（jackknife），最小 t = {min(ts):.2f}", ser, jackknife_min_t=min(ts)))
    return out


def h1(tw, closes, peers):
    fx = C.foreign_daily(closes, tw.dates)
    fx_orig = C.foreign_daily(closes, tw.dates, asia_same_day=True)
    pos = {d: i for i, d in enumerate(tw.dates)}
    b0 = C.bounds_weekly(tw.dates)
    top3 = {t: tw.top3.get(t, []) for t in tw.members}
    rows = []

    def run(group, name, bounds=b0, fxd=fx, members=tw.members, skip=0, agg="mean", start=None, end=None, **kw):
        p = C.h1_panel(tw.daily, fxd, members, peers, bounds, skip=skip, tw_agg=agg, **kw)
        if start:
            p = p[p.date >= start]
        if end:
            p = p[p.date <= end]
        ic = C.ic_series(p)
        fm = C.fm_series(p)
        sp = C.tercile_spread(p)
        r = summ(group, name, ic, fm_beta_f=float(fm["fp_r"].mean()) if len(fm) else None,
                 fm_t_f=C.nw(fm["fp_r"])["t"] if len(fm) else None,
                 spread_mean=float(sp.mean()) if len(sp) else None, spread_median=float(sp.median()) if len(sp) else None)
        rows.append(r)
        return p, ic

    prim, ic = run("H1 主要", "主要版本")
    run("H1 原始 vs 修正", "原始：亞股同一天＋前 3 大中位數", fxd=fx_orig, members=top3, agg="median")
    run("H1 原始 vs 修正", "只修 B2：前 3 大中位數", members=top3, agg="median")
    run("H1 原始 vs 修正", "只改權重：亞股同一天＋全部等權", fxd=fx_orig)
    run("H1 進場", "d+1 收盤進場", skip=1)
    for off in range(4):
        run("H1 切割", f"每 4 週，起點 +{off} 週", bounds=C.bounds_weekly(tw.dates, offset=off))
    run("H1 切割", "日曆月（月底切）", bounds=C.bounds_month(tw.dates))
    for s in STARTS[1:]:
        run("H1 起始日", f"從 {s} 起", start=s)
    run("H1 起始日", "砍掉最後 3 個月", end=str(pd.Timestamp(tw.dates[-1]) - pd.DateOffset(months=3))[:10])
    run("H1 平均 vs 中位數", "台灣成員用中位數（不是平均）", agg="median")
    mkt = pd.Series({d: (tw.twii.iloc[b0[k + 1]] / tw.twii.iloc[b0[k]] - 1) for k, d in enumerate([tw.dates[i] for i in b0[:-1]])})
    rows.extend(extremes("H1 ", ic, mkt))
    rows.extend(regime_rows("H1 ", ic, tw.twii, pos))
    for th in tw.members:
        run("H1 一次拿掉一個主題", th, members={k: v for k, v in tw.members.items() if k != th})
    run("H1 主題", "拿掉國外只有 1 檔的 4 個主題", members={k: v for k, v in tw.members.items() if k not in C.SINGLE_PEER})
    return pd.DataFrame(rows)


def h2(tw, closes, peers):
    labels = C.h2_labels(closes, peers, tw.dates)
    pos = {d: i for i, d in enumerate(tw.dates)}
    idx_all = {th: tw.theme_index(th, "all_ew") for th in labels}
    idx_top = {th: tw.theme_index(th, "top3") for th in labels}
    rows = []
    base = C.h2_records(tw, labels, idx_all, skip=1)

    def run(group, name, rec=base, agg="mean", exclude=(), start=None, end=None):
        r = rec if not start else rec[rec.date >= start]
        r = r if not end else r[r.date <= end]
        ser = C.h2_diff(r, agg=agg, exclude=exclude)
        rows.append(summ(group, name, ser))
        return ser

    ser = run("H2 主要", "主要版本")
    run("H2 原始 vs 修正", "原始：前 3 大、d 日進場", rec=C.h2_records(tw, labels, idx_top, skip=0))
    run("H2 原始 vs 修正", "只修 B4：前 3 大、d+1 進場", rec=C.h2_records(tw, labels, idx_top, skip=1))
    run("H2 原始 vs 修正", "只改權重：全部等權、d 日進場", rec=C.h2_records(tw, labels, idx_all, skip=0))
    for off in range(20):
        run("H2 切割", f"起算日 +{off}", rec=C.h2_records(tw, labels, idx_all, skip=1, offset=off))
    for s in STARTS[1:]:
        run("H2 起始日", f"從 {s} 起", start=s)
    run("H2 起始日", "砍掉最後 3 個月", end=str(pd.Timestamp(tw.dates[-1]) - pd.DateOffset(months=3))[:10])
    run("H2 平均 vs 中位數", "主題間用中位數", agg="median")
    run("H2 基準", "基準改加權指數", rec=C.h2_records(tw, labels, idx_all, skip=1, bench="twii"))
    run("H2 標籤", "國外抗跌 − 平常（參考）", rec=base.replace({"label": {"國外抗跌": "_on", "國外在動": "_x"}}).replace({"label": {"_on": "國外在動"}}))
    mkt = base.groupby("date")["mkt"].first()
    rows.extend(extremes("H2 ", ser, mkt))
    rows.extend(regime_rows("H2 ", ser, tw.twii, pos))
    for th in labels:
        run("H2 一次拿掉一個主題", th, exclude=(th,))
    run("H2 主題", "拿掉國外只有 1 檔的 4 個主題", exclude=C.SINGLE_PEER)
    # 每個主題的貢獻：每天的差距 = Σ_{j∈在動} (exc_j − 平常平均) / 在動數
    contrib = {}
    for d, g in base.groupby("date"):
        on, off = g[g.label == "國外在動"], g[g.label == "平常"]
        if len(on) and len(off):
            for _, r in on.iterrows():
                contrib[r.theme] = contrib.get(r.theme, 0) + (r.exc - off.exc.mean()) / len(on)
    tot = sum(contrib.values())
    cdf = pd.DataFrame([{"theme": k, "contribution": v, "share": v / tot} for k, v in contrib.items()]).sort_values("share", ascending=False)
    return pd.DataFrame(rows), cdf


def checks(df, prefix, groups):
    """plan.md §7 第 2 條：起始日、切割方式的變形裡 ≥ 90% 同方向、≥ 75% 的 t ≥ 2；一次拿掉一個主題都同方向。"""
    v = df[df.group.isin([f"{prefix} {g}" for g in groups])]
    loo = df[df.group == f"{prefix} 一次拿掉一個主題"]
    return {"variants": int(len(v)), "same_sign_share": float((v["mean"] > 0).mean()),
            "t_ge_2_share": float((v["t_nw"] >= 2).mean()),
            "loo_all_same_sign": bool((loo["mean"] > 0).all()), "loo_min_t": float(loo["t_nw"].min()),
            "pass_rule2": bool((v["mean"] > 0).mean() >= 0.9 and (v["t_nw"] >= 2).mean() >= 0.75 and (loo["mean"] > 0).all()),
            "weak_rule": bool((v["mean"] > 0).mean() >= 0.75)}


def main():
    tw = C.TW()
    peers = theme_peers.load(C.ROOT)
    closes = C.cached_yahoo("peers_4y", [t for v in peers.values() for t in v])
    r1 = h1(tw, closes, peers)
    r2, contrib = h2(tw, closes, peers)
    C.save_out(r1, "robustness_h1.csv")
    C.save_out(r2, "robustness_h2.csv")
    C.save_out(contrib, "robustness_h2_contrib.csv")
    out = {"H1": checks(r1, "H1", ("切割", "起始日")), "H2": checks(r2, "H2", ("切割", "起始日"))}
    with open(os.path.join(C.OUT, "robustness_checks.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    pd.set_option("display.width", 250)
    cols = ["group", "variant", "n", "mean", "median", "t_nw", "p", "pos_share"]
    print(r1[~r1.group.str.contains("拿掉一個|行情（[^主]")][cols + ["fm_t_f", "spread_mean", "spread_median"]].to_string())
    print(r2[~r2.group.str.contains("拿掉一個|行情（[^主]")][cols].to_string())
    print(contrib.head(6).to_string())
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
