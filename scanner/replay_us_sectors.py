"""回測：美股某產業在漲（或抗跌）的時候，買台股對應族群，之後有沒有贏全市場。只是研究，不進每日流程。

配對與門檻在 config/us_sectors.yaml（先寫死再跑結果）。

訊號（每個台股交易日 d 都算）：只用日期 < d 的美股收盤（台股 d 日開盤前看得到的）。
  ETF：20 日報酬 − universe 全部 ETF 20 日報酬中位數 ≥ strong_pp → 本身 > 0「在漲」、≤ 0「抗跌」，其他「平常」。
  利率（kind: rate）：^TNX 20 日上升 ≥ rate_up 個百分點 →「在漲」。
報酬：d 日台股收盤進場，持有 h 個交易日。族群 = 成員等權（每天重新等權、流動性門檻用前 20 日），
  基準 = 全部流動性過門檻的普通股等權（「全市場平均」）。超額 = 族群 − 基準。價格都已還原除權息。

為什麼不用每天的結果直接算勝率：今天和明天之後 20 天幾乎是同一段，每天算會把同一件事算 20 次。所以分兩種：
  區塊：每 h 個交易日取一天（不重疊），看那天的狀態和之後 h 天的超額。
  事件：狀態剛變成「在漲」／「抗跌」的那天進場；同一組配對前一筆還沒持有完就不再算新的（不重疊）。
「平常」＝同一組配對所有區塊，用來扣掉「這段期間這個族群本來就很會漲」。

用法：python -m scanner.replay_us_sectors [--root .]
輸出：replay/us_sectors.json（摘要）、docs/replay-us-sectors.md（表格）
"""
import os
import json
import logging
import argparse
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yaml

from scanner import adjust, score, themes

log = logging.getLogger("replay_us_sectors")
STATES = ("在漲", "抗跌", "平常")


def load(root):
    with open(os.path.join(root, "config", "us_sectors.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_yahoo(tickers, start):
    import yfinance as yf
    df = yf.download(list(tickers), start=start, auto_adjust=True, progress=False)["Close"]
    out = {}
    for t in tickers:
        if t in df.columns and df[t].notna().sum() > 30:
            v = df[t].dropna()
            out[t] = pd.Series(v.to_numpy(dtype=float), index=v.index.strftime("%Y-%m-%d"))
    return out


# ---------------- 訊號 ----------------

def us_states(us, cfg, tw_dates):
    """us：{ticker: 收盤 Series（index=日期字串）}。回傳 {(ticker, kind): 狀態 Series（index=台股日期）}。"""
    w, sig = cfg["signal"]["window"], cfg["signal"]
    r20 = {}
    for t, s in us.items():
        r = (s / s.shift(w) - 1) * 100 if t != "^TNX" else s - s.shift(w)
        # 台股 d 日看得到的 = 日期 < d 的最後一筆
        idx = np.searchsorted(np.array(s.index, dtype="U10"), np.array(tw_dates, dtype="U10"), side="left") - 1
        vals = np.where(idx >= 0, r.to_numpy()[np.clip(idx, 0, None)], np.nan)
        r20[t] = pd.Series(vals, index=tw_dates)
    univ = pd.DataFrame({t: r20[t] for t in cfg["universe"] if t in r20}).median(axis=1)
    out = {}
    for p in cfg["pairs"]:
        t, kind = p["us"], p.get("kind", "etf")
        if t not in r20:
            continue
        r = r20[t]
        if kind == "rate":
            st = np.where(r >= sig["rate_up"], "在漲", "平常")
        else:
            strong = (r - univ) >= sig["strong_pp"]
            st = np.where(strong & (r > 0), "在漲", np.where(strong, "抗跌", "平常"))
        # dtype=object：不然 pandas 會把 None 轉成 NaN，下面用 None 判斷「資料不足」會失效
        out[(t, kind)] = pd.Series(np.where(r.isna(), None, st), index=tw_dates, dtype=object)
    return out


# ---------------- 台股族群報酬 ----------------

def tw_panel(root, cfg):
    """回傳 (還原收盤寬表, 前 20 日平均成交金額寬表, 每檔產業)。只放普通股。"""
    dates = score.trading_dates(root)
    frames = [pd.read_csv(os.path.join(root, "history", "daily", f"{d}.csv.gz"), dtype={"code": str},
                          usecols=["date", "code", "industry", "close", "turnover"]) for d in dates]
    h = pd.concat(frames, ignore_index=True)
    h = h[h["code"].str.fullmatch(score.COMMON)]
    close = adjust.adjusted(root, h.pivot(index="date", columns="code", values="close").sort_index())
    turn = h.pivot(index="date", columns="code", values="turnover").sort_index()
    avg_turn = turn.rolling(20, min_periods=5).mean().shift(1)       # 只用進場前的成交金額
    ind = h.drop_duplicates("code", keep="last").set_index("code")["industry"]
    return close, avg_turn, ind


def basket_index(close, ok):
    """等權族群指數（每天重新等權）。ok：同形狀的布林表，當天可以算進來的成員。"""
    r = close.pct_change()
    r = r.where(r.abs() <= 0.3)                                        # 超過 ±30% 視為資料錯誤（漲跌幅限制 10%）
    r = r.where(ok.shift(1, fill_value=False))                         # 前一天就在籃子裡才算它今天的報酬
    return (1 + r.mean(axis=1).fillna(0)).cumprod()


def members(p, ind, root):
    tw = p["tw"]
    if "stocks" in tw:
        return [str(c) for c in tw["stocks"]]
    if tw.get("themes") == "all":
        tc = themes.load(root) or {}
        return sorted({str(s["code"]) for v in (tc.get("themes") or {}).values() for s in v.get("stocks") or []})
    return list(ind[ind.isin(tw["industries"])].index)


# ---------------- 結果 ----------------

def fwd(idx, h):
    return (idx.shift(-h) / idx - 1) * 100


def stats(v, cost):
    v = np.asarray([x for x in v if x == x])
    if not len(v):
        return {"n": 0, "mean": None, "median": None, "win": None, "win_after_cost": None}
    return {"n": int(len(v)), "mean": round(float(v.mean()), 2), "median": round(float(np.median(v)), 2),
            "win": round(float((v > 0).mean()), 3), "win_after_cost": round(float((v > cost).mean()), 3)}


def blocks(state, exc, h, start):
    """每 h 天取一天（從 start 開始）：[(日期, 狀態, 之後 h 天超額)]，之後不足 h 天的不算。"""
    out = []
    for i in range(start, len(state) - h, h):
        s, e = state.iloc[i], exc.iloc[i]
        if s is not None and e == e:
            out.append((state.index[i], s, float(e)))
    return out


def events(state, exc, h, which):
    """狀態剛變成 which 的那天；前一筆還沒持有完（h 天內）就跳過。"""
    out, busy_until = [], -1
    st = state.tolist()
    for i in range(1, len(st) - h):
        if st[i] == which and st[i - 1] not in (which, None) and i > busy_until:
            e = exc.iloc[i]
            if e == e:
                out.append((state.index[i], which, float(e)))
                busy_until = i + h - 1
    return out


def run(root=".", us=None):
    cfg = load(root)
    close, avg_turn, ind = tw_panel(root, cfg)
    dates = list(close.index)
    if us is None:
        tickers = sorted(set(cfg["universe"]) | {p["us"] for p in cfg["pairs"]})
        us = fetch_yahoo(tickers, start=str(pd.Timestamp(dates[0]) - pd.Timedelta(days=60))[:10])
    states = us_states(us, cfg, dates)
    liquid = avg_turn >= cfg["min_avg_turnover_billion"] * 1e8
    bench = basket_index(close, liquid & close.notna())
    start = cfg["signal"]["window"]                                     # 前 20 天台股資料還不夠算族群門檻
    cost = cfg["cost_pct"]
    res = []
    for p in cfg["pairs"]:
        st = states.get((p["us"], p.get("kind", "etf")))
        if st is None:
            log.warning(f"{p['name']}：美股 {p['us']} 沒有資料，略過")
            continue
        codes = [c for c in members(p, ind, root) if c in close.columns]
        ok = close[codes].notna()
        if p["tw"].get("liquidity", True):
            ok &= liquid[codes]
        idx = basket_index(close[codes], ok)
        row = {"name": p["name"], "us": p["us"], "n_members": len(codes),
               "avg_members": round(float(ok.sum(axis=1).iloc[start:].mean()), 1),
               "days": {s: int((st == s).sum()) for s in STATES}, "blocks": {}, "events": {}, "by_year": {}}
        for h in cfg["hold"]:
            exc = fwd(idx, h) - fwd(bench, h)
            b = blocks(st, exc, h, start)
            row["blocks"][str(h)] = {s: stats([e for _, x, e in b if x == s], cost) for s in STATES}
            row["blocks"][str(h)]["全部"] = stats([e for _, _, e in b], cost)
            row["events"][str(h)] = {s: stats([e for _, _, e in events(st, exc, h, s)], cost) for s in ("在漲", "抗跌")}
            if h == 20:
                row["_blocks20"] = b
                for y in sorted({d[:4] for d, _, _ in b}):
                    row["by_year"][y] = {s: stats([e for d, x, e in b if x == s and d[:4] == y], cost)
                                         for s in STATES}
        res.append(row)
    pooled = pooled_test(res)
    for r in res:
        r.pop("_blocks20", None)
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "period": [dates[start], dates[-1]], "trading_days": len(dates) - start, "config": cfg,
            "pairs": res, "pooled": pooled}


def pooled_test(res):
    """全部配對放一起（20 日區塊）：每個區塊日期算「在漲的配對平均超額 − 同一天平常的配對平均超額」，
    再看這些差距的平均和 t（同一天的配對互相不獨立，所以以日期為單位）。抗跌同理。"""
    out = {}
    by_date = {}
    for r in res:
        for d, s, e in r.get("_blocks20", []):
            by_date.setdefault(d, {}).setdefault(s, []).append(e)
    for which in ("在漲", "抗跌"):
        diffs = [np.mean(v[which]) - np.mean(v["平常"]) for v in by_date.values() if which in v and "平常" in v]
        n = len(diffs)
        m = float(np.mean(diffs)) if n else None
        sd = float(np.std(diffs, ddof=1)) if n > 1 else None
        out[which] = {"blocks": n, "mean_diff": round(m, 2) if m is not None else None,
                      "t": round(m / sd * np.sqrt(n), 2) if sd else None,
                      "positive_share": round(float(np.mean([x > 0 for x in diffs])), 2) if n else None}
    return out


# ---------------- 報告 ----------------

def _cell(s):
    if not s or not s["n"]:
        return "—"
    warn = " ⚠" if s["n"] < 10 else ""
    return f"{s['median']:+.2f}%｜勝率 {s['win']:.0%}（扣成本 {s['win_after_cost']:.0%}）｜n={s['n']}{warn}"


def report(out):
    hz = [str(h) for h in out["config"]["hold"]]
    L = [f"回測期間 {out['period'][0]} 到 {out['period'][1]}（{out['trading_days']} 個交易日）。"
         f"配對在 config/us_sectors.yaml（先寫死再跑）。產生：{out['generated_at'][:10]}。",
         "", "數字＝之後 h 個交易日，族群比全市場平均多賺的中位數｜勝率（多賺 > 0 的比例）｜扣 0.4% 成本後的勝率｜樣本數。"
         "⚠ 表示樣本 < 10，幾乎不能看。", "",
         "## 全部配對放一起（20 日區塊，以日期為單位）", "",
         "| 狀態 | 區塊數 | 比同一天「平常」的配對多賺（平均） | t | 多賺的區塊比例 |", "|---|---|---|---|---|"]
    for k, v in out["pooled"].items():
        L.append(f"| {k} | {v['blocks']} | {v['mean_diff']:+.2f} 個百分點 | {v['t']} | {v['positive_share']:.0%} |"
                 if v["blocks"] else f"| {k} | 0 | — | — | — |")
    for h in hz:
        L += ["", f"## 區塊（每 {h} 個交易日看一次，不重疊）：持有 {h} 日", "",
              "| 配對 | 成員（平均） | 美股在漲 | 美股抗跌 | 平常 | 全部（這個族群本來的水準） |", "|---|---|---|---|---|---|"]
        for r in out["pairs"]:
            b = r["blocks"][h]
            L.append(f"| {r['name']}（{r['us']}） | {r['avg_members']} | {_cell(b['在漲'])} | {_cell(b['抗跌'])} | "
                     f"{_cell(b['平常'])} | {_cell(b['全部'])} |")
    for h in hz:
        L += ["", f"## 事件（剛轉成在漲／抗跌那天進場）：持有 {h} 日", "",
              "| 配對 | 剛轉在漲 | 剛轉抗跌 | 區塊「全部」（對照） |", "|---|---|---|---|"]
        for r in out["pairs"]:
            e = r["events"][h]
            L.append(f"| {r['name']} | {_cell(e['在漲'])} | {_cell(e['抗跌'])} | {_cell(r['blocks'][h]['全部'])} |")
    L += ["", "## 分年度（20 日區塊）：美股在漲 − 平常（中位數差，個百分點）", "",
          "| 配對 | " + " | ".join(sorted({y for r in out["pairs"] for y in r["by_year"]})) + " |",
          "|---|" + "---|" * len({y for r in out["pairs"] for y in r["by_year"]})]
    years = sorted({y for r in out["pairs"] for y in r["by_year"]})
    for r in out["pairs"]:
        cells = []
        for y in years:
            s = r["by_year"].get(y, {})
            a, b = s.get("在漲") or {}, s.get("平常") or {}
            cells.append(f"{a['median'] - b['median']:+.2f}（n={a['n']}）" if a.get("n") and b.get("n") else "—")
        L.append(f"| {r['name']} | " + " | ".join(cells) + " |")
    return "\n".join(L) + "\n"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    root = ap.parse_args().root
    out = run(root)
    os.makedirs(os.path.join(root, "replay"), exist_ok=True)
    with open(os.path.join(root, "replay", "us_sectors.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(root, "docs", "replay-us-sectors.md"), "w", encoding="utf-8") as f:
        f.write("# 回測：美股產業在漲／抗跌時買台股對應族群\n\n" + report(out))
    log.info(f"完成：{len(out['pairs'])} 組配對，全部放一起 {out['pooled']}")


if __name__ == "__main__":
    main()
