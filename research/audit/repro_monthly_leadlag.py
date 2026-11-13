"""重現 2026-11-13 對話裡「國外過去 4 週 → 台灣下 4 週，相關 0.13、t≈3.1」的計算（當時只在暫存檔跑過，沒進 repo）。

只讀資料、不改任何東西。用法：
  python research/audit/repro_monthly_leadlag.py [--closes 當時抓的 Yahoo 收盤 json]
不給 --closes 就重新向 Yahoo 抓 3 年（Yahoo 會修正歷史資料，數字可能和當時差一點）。

當時的做法（原樣保留，問題寫在 research/audit/statistical_method_audit.md）：
  1. 週相對報酬：scanner.theme_peers.tw_weekly / foreign_weekly（台股前 3 大用最新市值固定選）
  2. 從第 4 週起每 4 週一段、不重疊：國外過去 4 週合計 fp、台灣過去 4 週合計 tp、台灣下 4 週合計 tn
  3. 每段內把 25 個主題排名（百分位 − 0.5）
  4. (a) 全部合起來的 Pearson；(b) OLS tn ~ fp + tp 的 t（一般 OLS 標準誤）；(c) 每段算一個相關，平均 / 標準差 × √段數
"""
import os
import sys
import json
import argparse

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from scanner import theme_peers, themes, history, score  # noqa: E402


def panel(closes):
    peers = theme_peers.load(ROOT)
    tw = theme_peers.tw_weekly(ROOT, themes.load(ROOT), history.load_shares(ROOT))
    fw = theme_peers.foreign_weekly(closes, peers, score.trading_dates(ROOT))
    rows = []
    for th in peers:
        if th not in tw or th not in fw:
            continue
        w = pd.period_range(min(tw[th].index.min(), fw[th].index.min()), max(tw[th].index.max(), fw[th].index.max()), freq="W-FRI")
        t, f = tw[th].reindex(w), fw[th].reindex(w)
        for s in range(4, len(w) - 4, 4):
            fp, tp, tn = f.iloc[s - 4:s].sum(min_count=4), t.iloc[s - 4:s].sum(min_count=4), t.iloc[s:s + 4].sum(min_count=4)
            if not np.isnan([fp, tp, tn]).any():
                rows.append((th, w[s], fp, tp, tn))
    d = pd.DataFrame(rows, columns=["th", "w", "fp", "tp", "tn"])
    for c in ("fp", "tp", "tn"):
        d[c + "_r"] = d.groupby("w")[c].rank(pct=True) - 0.5
    return d


def stats(d):
    X = np.c_[np.ones(len(d)), d.fp_r, d.tp_r]
    b, *_ = np.linalg.lstsq(X, d.tn_r, rcond=None)
    res = d.tn_r - X @ b
    s2 = res.var() * len(d) / (len(d) - 3)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))
    per = d.groupby("w").apply(lambda g: g.fp_r.corr(g.tn_r))
    return {"periods": int(d.w.nunique()), "obs": int(len(d)),
            "pooled_corr_f_to_next_tw": round(float(d.fp_r.corr(d.tn_r)), 3),
            "pooled_corr_tw_to_next_tw": round(float(d.tp_r.corr(d.tn_r)), 3),
            "ols_beta_f": round(float(b[1]), 3), "ols_t_f": round(float(b[1] / se[1]), 2),
            "ols_beta_tw": round(float(b[2]), 3), "ols_t_tw": round(float(b[2] / se[2]), 2),
            "fm_mean_corr": round(float(per.mean()), 3),
            "fm_t": round(float(per.mean() / per.std() * np.sqrt(len(per))), 2),
            "fm_positive_share": round(float((per > 0).mean()), 2)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--closes")
    a = ap.parse_args()
    if a.closes:
        closes = json.load(open(a.closes))
    else:
        peers = theme_peers.load(ROOT)
        closes, _ = theme_peers.fetch_yahoo(sorted({t for v in peers.values() for t in v}), period="3y")
    print(json.dumps(stats(panel(closes)), ensure_ascii=False, indent=1))
