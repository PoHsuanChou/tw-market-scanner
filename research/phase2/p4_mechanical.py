"""Phase 4：機械式配對（plan.md §6）。不靠人挑國外公司，和現在的人工名單（Expert v1）比較。

規則（plan.md 寫定，這裡照做）：
  Expert v1：config/themes.yaml ＋ config/theme_peers.yaml 原樣凍結成 research/universe/universe_v1_2026-11.csv
  M1：每個台股主題，成員在 Yahoo 的產業別（industryKey）取多數（照成員數，同票取字母序第一個）→
      國外對照 = yf.Industry(該產業).top_companies 依 market weight 前 K=5 檔，排除台股（.TW/.TWO）與台股公司的 ADR。
      Yahoo 的分類與名單都是「現在」的，沒有歷史版 → 有存活者與後見之明偏差（不補造歷史）。
  M2：Yahoo 產業 → 產業 ETF（對照表寫在下面 M2_ETF，跑之前寫定）。H1 要在主題間排名、H2 要和「全部對照」比，
      所以至少要 5 檔不同的 ETF 才能做；不夠就整個標 Insufficient Data。
判斷（plan.md §7 第 4 條）：M1 和 Expert 同方向且 t ≥ 1.5。

用法：python research/phase2/p4_mechanical.py
輸出：research/universe/universe_v1_2026-11.csv、universe_m1_yahoo_2026-11.csv、universe_m2_etf_2026-11.csv、
      research/phase2/out/mechanical.csv、mechanical_meta.json
"""
import os
import sys
import json
from collections import Counter

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
from common import theme_peers  # noqa: E402

K = 5
TW_ADR = {"TSM", "UMC", "ASX", "CHT"}          # 台股公司在美國的 ADR（同一家公司，不當國外對照）
M2_ETF = {"semiconductors": "SOXX", "communication-equipment": "IGN"}   # 其他 Yahoo 產業沒有對應的產業 ETF
UNIVERSE = os.path.join(C.ROOT, "research", "universe")


def market_of(t):
    for suf, m in ((".T", "日本"), (".KS", "韓國"), (".KQ", "韓國"), (".SS", "上海"), (".SZ", "深圳"), (".AS", "荷蘭"),
                   (".PA", "法國"), (".VI", "奧地利"), (".HK", "香港")):
        if t.endswith(suf):
            return m
    return "美國"


def freeze_expert(tw):
    """把現在的人工名單原樣凍結（不改內容）。已存在就不覆寫。"""
    p = os.path.join(UNIVERSE, "universe_v1_2026-11.csv")
    if os.path.exists(p):
        return pd.read_csv(p, dtype=str)
    with open(os.path.join(C.ROOT, "config", "themes.yaml"), encoding="utf-8") as f:
        tcfg = yaml.safe_load(f)
    last = pd.read_csv(os.path.join(C.ROOT, "history", "daily", f"{tw.dates[-1]}.csv.gz"), dtype={"code": str}).set_index("code")
    rows = []
    for th, v in tcfg["themes"].items():
        for s in v["stocks"]:
            code = str(s["code"])
            rows.append({"version": "v1", "theme": th, "side": "台股", "ticker": code, "name": s["name"],
                         "market": last["market"].get(code, ""), "inclusion_date": "2026-10-25", "exclusion_date": "",
                         "selection_reason": "使用者定稿的主題名單（config/themes.yaml 第二版）；前 3 大每天依市值重排",
                         "source": "config/themes.yaml", "weight": "主要版本：主題內全部等權"})
    for th, ts in theme_peers.load(C.ROOT).items():
        for t in ts:
            rows.append({"version": "v1", "theme": th, "side": "國外", "ticker": t, "name": "", "market": market_of(t),
                         "inclusion_date": "2026-10-27", "exclusion_date": "",
                         "selection_reason": "AI 助手依產品相似度列（theme_peers.yaml 註解），列的時候已知 2023～2026 漲幅，有後見之明",
                         "source": "config/theme_peers.yaml", "weight": "中位數（等於不加權）"})
    df = pd.DataFrame(rows)
    os.makedirs(UNIVERSE, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8")
    return df


def yahoo_tw_industry(tw):
    """台股主題成員在 Yahoo 的 industryKey（快取在 data/m1_tw_industry.json）。"""
    p = os.path.join(C.DATA, "m1_tw_industry.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))["industry"]
    import yfinance as yf
    last = pd.read_csv(os.path.join(C.ROOT, "history", "daily", f"{tw.dates[-1]}.csv.gz"), dtype={"code": str}).set_index("code")
    out = {}
    for code in sorted({c for v in tw.members.values() for c in v}):
        sym = code + (".TWO" if last["market"].get(code) == "TPEx" else ".TW")
        try:
            out[code] = yf.Ticker(sym).info.get("industryKey")
        except Exception:  # noqa: BLE001
            out[code] = None
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": pd.Timestamp.now(tz="UTC").isoformat(), "industry": out}, f, ensure_ascii=False)
    return out


def yahoo_top_companies(keys):
    p = os.path.join(C.DATA, "m1_top_companies.json")
    cache = json.load(open(p, encoding="utf-8"))["top"] if os.path.exists(p) else {}
    import yfinance as yf
    for k in keys:
        if k in cache:
            continue
        try:
            tc = yf.Industry(k).top_companies
            cache[k] = [[str(s), float(w)] for s, w in tc["market weight"].items()] if tc is not None else []
        except Exception:  # noqa: BLE001
            cache[k] = []
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": pd.Timestamp.now(tz="UTC").isoformat(), "top": cache}, f, ensure_ascii=False)
    return cache


def build_m1(tw):
    ind = yahoo_tw_industry(tw)
    theme_key = {}
    for th, cs in tw.members.items():
        votes = Counter(ind.get(c) for c in cs if ind.get(c))
        if votes:
            best = max(votes.values())
            theme_key[th] = sorted(k for k, v in votes.items() if v == best)[0]
    top = yahoo_top_companies(sorted(set(theme_key.values())))
    peers = {}
    for th, k in theme_key.items():
        cand = [s for s, _ in sorted(top.get(k, []), key=lambda x: -x[1])
                if not s.endswith((".TW", ".TWO")) and s not in TW_ADR]
        if cand:
            peers[th] = cand[:K]
    rows = [{"version": "m1", "theme": th, "side": "國外", "ticker": t, "name": "", "market": market_of(t),
             "inclusion_date": "2026-11-13", "exclusion_date": "", "selection_reason": f"Yahoo 產業 {theme_key[th]} 前 {K} 大（market weight）",
             "source": "Yahoo yf.Industry（現在的名單）", "weight": "中位數"} for th, ts in peers.items() for t in ts]
    pd.DataFrame(rows).to_csv(os.path.join(UNIVERSE, "universe_m1_yahoo_2026-11.csv"), index=False, encoding="utf-8")
    return peers, theme_key, ind


def build_m2(theme_key):
    m2 = {th: M2_ETF[k] for th, k in theme_key.items() if k in M2_ETF}
    rows = [{"version": "m2", "theme": th, "side": "國外", "ticker": t, "market": "美國", "inclusion_date": "2026-11-13",
             "selection_reason": f"Yahoo 產業 {theme_key[th]} 對應的產業 ETF", "source": "plan.md §6 M2 對照表"} for th, t in m2.items()]
    pd.DataFrame(rows).to_csv(os.path.join(UNIVERSE, "universe_m2_etf_2026-11.csv"), index=False, encoding="utf-8")
    return m2


def evaluate(tw, peers, closes, name):
    fx = C.foreign_daily(closes, tw.dates)
    panel = C.h1_panel(tw.daily, fx, tw.members, peers, C.bounds_weekly(tw.dates))
    ic = C.ic_series(panel)
    fm = C.fm_series(panel) if len(panel) else pd.DataFrame(columns=["fp_r"])
    labels = C.h2_labels(closes, peers, tw.dates)
    rec = C.h2_records(tw, labels, {th: tw.theme_index(th, "all_ew") for th in labels}, skip=1)
    diff = C.h2_diff(rec)
    r1, r2, rf = C.nw(ic), C.nw(diff), C.nw(fm["fp_r"]) if len(fm) else {"mean": None, "t": None}
    return [{"universe": name, "hypothesis": "H1 IC", "n": r1["n"], "mean": r1["mean"], "t_nw": r1["t"], "p": r1["p"]},
            {"universe": name, "hypothesis": "H1 控制台灣後國外係數", "n": r1["n"], "mean": rf["mean"], "t_nw": rf["t"], "p": rf.get("p")},
            {"universe": name, "hypothesis": "H2 國外在動 − 平常", "n": r2["n"], "mean": r2["mean"], "t_nw": r2["t"], "p": r2["p"],
             "label_days_on": int(sum((s == "國外在動").sum() for s in labels.values()))}]


def main():
    tw = C.TW()
    freeze_expert(tw)
    expert = theme_peers.load(C.ROOT)
    closes_e = C.cached_yahoo("peers_4y", [t for v in expert.values() for t in v])
    m1, theme_key, ind = build_m1(tw)
    m2 = build_m2(theme_key)
    closes_m1 = C.cached_yahoo("m1_4y", [t for v in m1.values() for t in v])
    rows = evaluate(tw, expert, closes_e, "Expert v1") + evaluate(tw, m1, closes_m1, "M1 Yahoo 產業")
    distinct_m2 = sorted(set(m2.values()))
    rows.append({"universe": "M2 產業 ETF", "hypothesis": "H1、H2", "n": len(m2), "mean": None, "t_nw": None, "p": None,
                 "note": f"只有 {len(distinct_m2)} 檔不同 ETF（{', '.join(distinct_m2) or '無'}），少於 5 檔 → Insufficient Data，不跑"})
    df = pd.DataFrame(rows)
    C.save_out(df, "mechanical.csv")
    groups = Counter(tuple(v) for v in m1.values())
    meta = {"theme_industry": theme_key, "m1_peers": m1, "m1_distinct_peer_groups": len(groups),
            "m1_themes": len(m1), "tw_members_without_industry": sorted(c for c, k in ind.items() if not k),
            "m2": m2, "m2_distinct_etf": distinct_m2,
            "m1_vs_expert_overlap": {th: sorted(set(m1.get(th, [])) & set(expert.get(th, []))) for th in expert}}
    e = df[(df.universe == "Expert v1") & (df.hypothesis == "H2 國外在動 − 平常")].iloc[0]
    m = df[(df.universe == "M1 Yahoo 產業")]
    meta["rule4"] = {h: bool((m[m.hypothesis == h]["mean"].iloc[0] or 0) > 0 and (m[m.hypothesis == h]["t_nw"].iloc[0] or 0) >= 1.5)
                     for h in ("H1 IC", "H2 國外在動 − 平常")}
    with open(os.path.join(C.OUT, "mechanical_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1, default=float)
    pd.set_option("display.width", 250)
    print(df.to_string())
    print(json.dumps({k: meta[k] for k in ("theme_industry", "m1_distinct_peer_groups", "m1_peers", "rule4", "m2_distinct_epf" if False else "m2_distinct_etf")},
                     ensure_ascii=False, indent=1))
    _ = e


if __name__ == "__main__":
    main()
