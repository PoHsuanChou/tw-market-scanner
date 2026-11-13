"""敏感度檢查（只讀）：SMH → AI 硬體細分主題（config/us_sectors.yaml 第 2 組）。
重現 2026-11-13 對話裡的複查：20 種區塊起算日、每天（重疊）比較、只在 SMH 在漲時持有 vs 一直持有。
用法：python research/audit/sensitivity_smh_ai.py
"""
import os
import sys
import json

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from scanner import replay_us_sectors as R  # noqa: E402

if __name__ == "__main__":
    cfg = R.load(ROOT)
    close, avg, ind = R.tw_panel(ROOT, cfg)
    dates = list(close.index)
    us = R.fetch_yahoo(sorted(set(cfg["universe"]) | {"SMH"}), start="2023-07-01")
    st = R.us_states(us, cfg, dates)[("SMH", "etf")]
    liq = avg >= cfg["min_avg_turnover_billion"] * 1e8
    bench = R.basket_index(close, liq & close.notna())
    p = next(x for x in cfg["pairs"] if x["name"].startswith("半導體→"))
    codes = [c for c in R.members(p, ind, ROOT) if c in close.columns]
    idx = R.basket_index(close[codes], close[codes].notna() & liq[codes])
    exc = R.fwd(idx, 20) - R.fwd(bench, 20)
    offs = []
    for off in range(20):
        b = R.blocks(st, exc, 20, 20 + off)
        a, o = [e for _, s, e in b if s == "在漲"], [e for _, s, e in b if s == "平常"]
        offs.append({"offset": off, "diff_median": round(float(np.median(a) - np.median(o)), 2), "n_up": len(a)})
    a, o = exc[st == "在漲"].dropna(), exc[st == "平常"].dropna()
    r = idx.pct_change() - bench.pct_change()
    hold = st.shift(1) == "在漲"
    print(json.dumps({
        "share_of_days_up": round(float((st == "在漲").mean()), 2),
        "block_offsets": offs,
        "positive_offsets": round(float(np.mean([x["diff_median"] > 0 for x in offs])), 2),
        "daily_overlapping": {"up": [round(float(a.median()), 2), round(float((a > 0).mean()), 2), len(a)],
                              "normal": [round(float(o.median()), 2), round(float((o > 0).mean()), 2), len(o)]},
        "always_hold_rel_pct": round(float(((1 + r.fillna(0)).prod() - 1) * 100), 1),
        "hold_only_when_up_rel_pct_before_cost": round(float(((1 + r.where(hold, 0).fillna(0)).prod() - 1) * 100), 1),
        "switches": int((hold.astype(int).diff().abs() == 1).sum())}, ensure_ascii=False, indent=1))
