"""Phase 3：樣本外（plan.md §5）。

H1（有估計係數）：擴張視窗 walk-forward。
  Model A：台灣自己過去 4 週排名；Model B：A ＋ 國外過去 4 週排名；（參考）Model F：只有國外。
  每段截面迴歸、訓練期係數取平均。測試段 j 只用 i ≤ j−2 的段訓練（i = j−1 的標籤剛好在決策當天結束，
  再空一段當 embargo），而且至少 12 段才開始。特徵都只用決策日以前的資料（B2 修正後）。
  評估：IC（預測 vs 下段台灣報酬）、Rank IC（vs 下段排名）、方向準確率、前 1/3 − 後 1/3（d+1 進場、扣多空成本）、
  樣本外 R²（對 Model A、對 0）、Clark-West（B 比 A）。
H2（門檻事先固定、沒有估計參數）：前 12 個決策日之後當樣本外，逐季列出「國外在動 − 平常」。
真正的樣本外 = 2026-10-27 起的真實紀錄（ledger/theme_peers.jsonl），只列出還不能評估。

用法：python research/phase2/p3_walk_forward.py
輸出：research/phase2/out/oos_h1.csv（每個測試段）、oos_h1_summary.csv、oos_h2.csv、oos_checks.json
"""
import os
import sys
import json

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
from common import theme_peers  # noqa: E402
from scanner import ledger  # noqa: E402

MIN_TRAIN = 12
EMBARGO = 1
MODELS = {"A": ("tp_r",), "B": ("fp_r", "tp_r"), "F": ("fp_r",)}


def fit(train, xs):
    fm = C.fm_series(train, xs=xs)
    return fm.mean().to_numpy()


def walk_forward(panel, panel_d1):
    dates = sorted(panel.date.unique())
    rows = []
    for j in range(MIN_TRAIN + EMBARGO, len(dates)):
        train = panel[panel.date.isin(dates[: j - EMBARGO])]
        test = panel[panel.date == dates[j]]
        test1 = panel_d1[panel_d1.date == dates[j]]
        r = {"date": dates[j], "n_train": j - EMBARGO, "n_themes": len(test)}
        preds = {}
        for m, xs in MODELS.items():
            b = fit(train, xs)
            preds[m] = test[list(xs)].to_numpy() @ b
            y = test["tn"].to_numpy()
            r[f"ic_{m}"] = float(np.corrcoef(preds[m], y)[0, 1])
            r[f"rankic_{m}"] = float(np.corrcoef(preds[m], test["tn_r"])[0, 1])
            r[f"dir_{m}"] = float(np.mean((preds[m] > np.median(preds[m])) == (y > np.median(y))))
            if len(test1):
                g = test1.assign(score=pd.Series(dict(zip(test.theme, preds[m]))).reindex(test1.theme).to_numpy())
                r[f"spread_d1_{m}"] = float(C.tercile_spread(g.dropna(subset=["score"]), score_col="score").iloc[0]) \
                    if g["score"].notna().sum() >= 6 else np.nan
            r[f"sse_{m}"] = float(np.sum((test["tn_r"].to_numpy() - preds[m]) ** 2))
        r["sse_0"] = float(np.sum(test["tn_r"].to_numpy() ** 2))
        r["cw"] = float(np.mean(C.clark_west(test["tn_r"].to_numpy(), preds["A"], preds["B"])))
        rows.append(r)
    return pd.DataFrame(rows)


def main():
    tw = C.TW()
    peers = theme_peers.load(C.ROOT)
    closes = C.cached_yahoo("peers_4y", [t for v in peers.values() for t in v])
    fx = C.foreign_daily(closes, tw.dates)
    b = C.bounds_weekly(tw.dates)
    panel = C.h1_panel(tw.daily, fx, tw.members, peers, b)
    panel_d1 = C.h1_panel(tw.daily, fx, tw.members, peers, b, skip=1)
    wf = walk_forward(panel, panel_d1)
    C.save_out(wf, "oos_h1.csv")
    summ = []
    for m in MODELS:
        s = {"model": m, "periods": len(wf)}
        for k in ("ic", "rankic", "dir", "spread_d1"):
            base = 0.5 if k == "dir" else 0.0                     # 方向準確率要和 50%（亂猜）比，不是和 0 比
            r = C.nw(wf[f"{k}_{m}"] - base)
            s[f"{k}_mean"], s[f"{k}_t"], s[f"{k}_p"] = r["mean"] + base, r["t"], r["p"]
        s["r2_vs_A"] = float(1 - wf[f"sse_{m}"].sum() / wf["sse_A"].sum())
        s["r2_vs_0"] = float(1 - wf[f"sse_{m}"].sum() / wf["sse_0"].sum())
        summ.append(s)
    cw = C.nw(wf["cw"])
    dic = C.nw(wf["ic_B"] - wf["ic_A"])
    summ.append({"model": "B − A", "periods": len(wf), "ic_mean": dic["mean"], "ic_t": dic["t"], "ic_p": dic["p"],
                 "cw_mean": cw["mean"], "cw_t": cw["t"], "cw_p": cw["p"]})
    sdf = pd.DataFrame(summ)
    C.save_out(sdf, "oos_h1_summary.csv")

    # H2：主要版本的決策日序列，前 12 個之後當樣本外，逐季
    labels = C.h2_labels(closes, peers, tw.dates)
    rec = C.h2_records(tw, labels, {th: tw.theme_index(th, "all_ew") for th in labels}, skip=1)
    ser = C.h2_diff(rec)
    oos = ser.iloc[MIN_TRAIN:]
    q = oos.groupby(pd.PeriodIndex(pd.to_datetime(oos.index), freq="Q").astype(str)).agg(["mean", "count"])
    q = q.reset_index().rename(columns={"index": "quarter"})
    C.save_out(q, "oos_h2.csv")
    live = ledger.read(C.ROOT, "theme_peers")
    live_dates = sorted({r["tw_data_date"] for r in live})
    h2r = C.nw(oos)
    m_b = sdf[sdf.model == "B"].iloc[0]
    out = {
        "H1": {"oos_periods": int(len(wf)), "ic_B_mean": m_b["ic_mean"], "ic_B_t": m_b["ic_t"],
               "clark_west_p": cw["p"], "spread_d1_B_mean": m_b["spread_d1_mean"],
               "pass_rule3": bool(m_b["ic_mean"] > 0 and m_b["ic_t"] >= 2 and cw["p"] < 0.05 and m_b["spread_d1_mean"] > 0),
               "weak_oos": bool(m_b["ic_mean"] > 0 and m_b["ic_t"] >= 1)},
        "H2": {"oos_dates": int(len(oos)), "mean": h2r["mean"], "t": h2r["t"], "p": h2r["p"],
               "quarters": int(len(q)), "quarters_positive": int((q["mean"] > 0).sum()),
               "pass_rule3": bool(oos.sum() > 0 and (q["mean"] > 0).mean() >= 2 / 3),
               "weak_oos": bool(h2r["mean"] > 0 and (h2r["t"] or 0) >= 1)},
        "live_ledger": {"first": live_dates[0] if live_dates else None, "last": live_dates[-1] if live_dates else None,
                        "trading_days": len(live_dates), "matured_20d": 0,
                        "note": "真實紀錄還沒有任何一筆滿 20 個交易日，Insufficient Data"},
    }
    with open(os.path.join(C.OUT, "oos_checks.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=float)
    pd.set_option("display.width", 250)
    print(sdf.to_string())
    print(q.to_string())
    print(json.dumps(out, ensure_ascii=False, default=float, indent=1))


if __name__ == "__main__":
    main()
