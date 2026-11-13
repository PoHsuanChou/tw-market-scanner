"""敏感度檢查（只讀）：repro_monthly_leadlag 的結果換「4 週一段的起點」（0～3 週）還在不在；
以及每段做截面迴歸 tn ~ fp + tp（Fama-MacBeth），係數序列用 Newey-West（lag 2）算 t。
用法：python research/audit/sensitivity_monthly.py [--closes json]
"""
import os
import sys
import json
import argparse

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repro_monthly_leadlag as R  # noqa: E402


def panel_offset(closes, off):
    peers = R.theme_peers.load(R.ROOT)
    tw = R.theme_peers.tw_weekly(R.ROOT, R.themes.load(R.ROOT), R.history.load_shares(R.ROOT))
    fw = R.theme_peers.foreign_weekly(closes, peers, R.score.trading_dates(R.ROOT))
    rows = []
    for th in peers:
        if th not in tw or th not in fw:
            continue
        w = pd.period_range(min(tw[th].index.min(), fw[th].index.min()), max(tw[th].index.max(), fw[th].index.max()), freq="W-FRI")
        t, f = tw[th].reindex(w), fw[th].reindex(w)
        for s in range(4 + off, len(w) - 4, 4):
            fp, tp, tn = f.iloc[s - 4:s].sum(min_count=4), t.iloc[s - 4:s].sum(min_count=4), t.iloc[s:s + 4].sum(min_count=4)
            if not np.isnan([fp, tp, tn]).any():
                rows.append((th, w[s], fp, tp, tn))
    d = pd.DataFrame(rows, columns=["th", "w", "fp", "tp", "tn"])
    for c in ("fp", "tp", "tn"):
        d[c + "_r"] = d.groupby("w")[c].rank(pct=True) - 0.5
    return d


def fama_macbeth(d, lags=2):
    coefs = []
    for _, g in d.groupby("w"):
        X = np.c_[np.ones(len(g)), g.fp_r, g.tp_r]
        b, *_ = np.linalg.lstsq(X, g.tn_r, rcond=None)
        coefs.append(b[1:])
    c = np.array(coefs)
    out = {}
    for j, name in enumerate(("f", "tw")):
        x = c[:, j]
        n = len(x)
        e = x - x.mean()
        gam = [np.sum(e[k:] * e[:n - k]) / n for k in range(lags + 1)]
        var = gam[0] + 2 * sum((1 - k / (lags + 1)) * gam[k] for k in range(1, lags + 1))
        out[name] = {"beta": round(float(x.mean()), 3), "t_nw": round(float(x.mean() / np.sqrt(var / n)), 2),
                     "t_plain": round(float(x.mean() / (x.std(ddof=1) / np.sqrt(n))), 2)}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--closes")
    a = ap.parse_args()
    if a.closes:
        closes = json.load(open(a.closes))
    else:
        peers = R.theme_peers.load(R.ROOT)
        closes, _ = R.theme_peers.fetch_yahoo(sorted({t for v in peers.values() for t in v}), period="3y")
    res = {}
    for off in range(4):
        d = panel_offset(closes, off)
        res[f"offset_{off}"] = {**{k: v for k, v in R.stats(d).items() if k in ("periods", "fm_mean_corr", "fm_t", "fm_positive_share")},
                               "fama_macbeth": fama_macbeth(d)}
    print(json.dumps(res, ensure_ascii=False, indent=1))
