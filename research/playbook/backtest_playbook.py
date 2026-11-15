"""回測：track_stock 的幾條規則與心法數字，事後看準不準。2026-11-14。

只用 history/ 裡「當時已經知道」的資料（價格當天收盤、營收用公布日），規則在跑之前寫死，不因結果調整。
輸出 research/reports/playbook_backtest.md。

測試一｜EPS 公式的營收假設（track_stock common/valuation_defaults.py「中立」情境）
  中立次年營收＝近 3 月營收×4×(1＋min(近 3 月平均年增÷4, 25%))，年增為負時成長取 0。
  對答案：之後 12 個月實際營收合計。誤差＝實際÷預估−1（正＝公式低估）。
  同時看「樂觀」版（÷2、上限 50%）與「不打折」版（年增全給、上限 100%），只描述，不改規則。
  樣本：月營收 ≥ 5 億的股票（跟全市場營收掃描同門檻），每個月一次。

測試二｜停損的「大盤拖累／個股轉弱」標記（track_stock monitor/daily_monitor.py stop_context）
  每 10 個交易日，對每檔 20 日平均成交金額 ≥ 3 億的普通股設停損＝收盤−2×ATR14（SOP 的波動停損）。
  之後 60 個交易日內第一次收盤 ≤ 停損＝跌破。跌破當天：
    加權指數 20 日 ≤ −5% 且 個股 20 日 − 指數 20 日 > −5 個百分點 → 大盤拖累；否則個股轉弱。
  看跌破後 20／60 個交易日「個股 − 加權指數」的超額報酬。
  想知道：大盤拖累組是不是比較常彈回來（如果是，未進場的卡遇到大盤拖累，改成重算比作廢合理）。

測試三｜大盤溫度（track_stock 網站「大盤溫度」的讀法）
  跌停家數：當天跌幅 ≤ −9.5% 的普通股數；5 日合計在「過去 250 天」的 95 分位以上＝恐慌。
  融資：Σ(個股融資餘額張數×收盤×1000) 估算金額；恐慌且 20 日減少 ≥ 5%＝恐慌＋融資出清。
  偏熱：融資在過去 250 天 90 分位以上，且漲停 5 日合計 80 分位以上。
  每個訊號只取每一波的第一天（相隔 < 20 個交易日視為同一波），看之後 20／60 天加權指數報酬，
  對照全部交易日的平均。樣本只有約 3 年，幾波而已，只能當描述。
"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from scanner import adjust, score  # noqa: E402

OUT = os.path.join(ROOT, "research", "reports", "playbook_backtest.md")


def is_common(c):
    return isinstance(c, str) and len(c) == 4 and c.isdigit() and not c.startswith("0")


def load_all():
    dates = score.trading_dates(ROOT)
    _, df = score.load_daily(ROOT, dates[-1], n=len(dates))
    df = df[df["code"].map(is_common)]
    wide = {c: df.pivot_table(index="date", columns="code", values=c, aggfunc="last").sort_index()
            for c in ("close", "high", "low", "turnover", "margin_bal")}
    adj = adjust.adjusted(ROOT, wide["close"])
    fac = adj / wide["close"]
    wide["adj"], wide["adj_high"], wide["adj_low"] = adj, wide["high"] * fac, wide["low"] * fac
    twii = score.index_series(ROOT).reindex(adj.index).ffill()
    return wide, twii


def fmt(x, pct=True):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x:+.1f}%" if pct else f"{x:.0f}"


def summarize(v):
    v = pd.Series(v).dropna()
    if not len(v):
        return {"n": 0}
    return {"n": len(v), "median": v.median(), "mean": v.mean(), "win": (v > 0).mean() * 100,
            "p25": v.quantile(.25), "p75": v.quantile(.75)}


def row(name, s, extra=""):
    if not s.get("n"):
        return f"| {name} | 0 | — | — | — | — |{extra}"
    return (f"| {name} | {s['n']} | {fmt(s['median'])} | {fmt(s['mean'])} | {s['win']:.0f}% | "
            f"{fmt(s['p25'])} ~ {fmt(s['p75'])} |{extra}")


# ---------------- 測試一 ----------------
def test_revenue():
    frames = {}
    for ym in score._files(ROOT, "revenue"):
        f = pd.read_csv(os.path.join(ROOT, "history", "revenue", f"{ym}.csv.gz"), dtype={"code": str})
        frames[ym] = f[f["code"].map(is_common)].set_index("code")
    yms = sorted(frames)
    rev = pd.DataFrame({ym: frames[ym]["revenue"] for ym in yms}) / 1e5   # 億
    yoy = pd.DataFrame({ym: frames[ym]["yoy"] for ym in yms})
    rec = []
    for i in range(2, len(yms) - 12):
        last3, fut = yms[i - 2:i + 1], yms[i + 1:i + 13]
        r3 = rev[last3]
        ok = r3.notna().all(axis=1) & rev[fut].notna().all(axis=1) & (rev[yms[i]] >= 5)
        y3 = yoy[last3].mean(axis=1)
        run = r3.sum(axis=1) * 4
        actual = rev[fut].sum(axis=1)
        for name, div, cap in (("中立", 4, 25), ("樂觀", 2, 50), ("不打折", 1, 100)):
            g = (y3 / div).clip(lower=0, upper=cap)
            fc = run * (1 + g / 100)
            err = (actual / fc - 1) * 100
            for code in ok[ok].index:
                rec.append({"ym": yms[i], "code": code, "ver": name, "yoy3": y3[code], "err": err[code]})
    d = pd.DataFrame(rec)
    L = ["## 測試一｜EPS 公式的營收假設", "",
         f"預估月份 {d['ym'].min()} ~ {d['ym'].max()}（之後要有 12 個月實際營收），每月營收 ≥ 5 億的普通股。",
         "誤差＝之後 12 個月實際營收 ÷ 公式預估 − 1。**正＝公式低估**（實際比預估多）。", "",
         "| 版本 | 樣本 | 中位數 | 平均 | 低估的比例 | 中間一半落在 |", "|---|---|---|---|---|---|"]
    for v in ("中立", "樂觀", "不打折"):
        L.append(row(v, summarize(d[d.ver == v]["err"])))
    L += ["", "依「近 3 月平均年增」分組（中立版）：", "",
          "| 近 3 月年增 | 樣本 | 中位數 | 平均 | 低估的比例 | 中間一半落在 |", "|---|---|---|---|---|---|"]
    base = d[d.ver == "中立"]
    for lo, hi, lab in ((-1e9, 0, "< 0%"), (0, 20, "0–20%"), (20, 50, "20–50%"), (50, 100, "50–100%"), (100, 1e9, "> 100%")):
        L.append(row(lab, summarize(base[(base.yoy3 >= lo) & (base.yoy3 < hi)]["err"])))
    L += ["", "依預估年份（中立版）：", "", "| 年份 | 樣本 | 中位數 | 平均 | 低估的比例 | 中間一半落在 |", "|---|---|---|---|---|---|"]
    for y, g in base.groupby(base.ym.str[:4]):
        L.append(row(y, summarize(g["err"])))
    return L, d


# ---------------- 測試二 ----------------
def atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = np.maximum(h - l, np.maximum((h - pc).abs(), (l - pc).abs()))
    return tr.rolling(n).mean()


def test_stops(w, twii):
    c, h, l, t = w["adj"], w["adj_high"], w["adj_low"], w["turnover"]
    A = pd.DataFrame({k: atr(h[k], l[k], c[k]) for k in c.columns})
    liq = t.rolling(20).mean()
    r20 = c / c.shift(20) - 1
    m20 = twii / twii.shift(20) - 1
    idx = list(c.index)
    rec = []
    for i in range(40, len(idx) - 21, 10):
        d0 = idx[i]
        stop = c.loc[d0] - 2 * A.loc[d0]
        ok = (liq.loc[d0] >= 3e8) & stop.notna() & (stop > 0)
        win = c.iloc[i + 1:i + 61]
        for code in ok[ok].index:
            hit = win[code] <= stop[code]
            if not hit.any():
                continue
            d = hit.idxmax()
            j = idx.index(d)
            s20, mk = r20.at[d, code], m20.loc[d]
            if np.isnan(s20) or np.isnan(mk):
                continue
            typ = "大盤拖累" if (mk <= -0.05 and (s20 - mk) > -0.05) else "個股轉弱"
            out = {"d0": d0, "date": d, "code": code, "type": typ, "mk20": mk * 100}
            for k in (20, 60):
                if j + k < len(idx):
                    out[f"ex{k}"] = ((c.iat[j + k, c.columns.get_loc(code)] / c.at[d, code])
                                     - (twii.iloc[j + k] / twii.loc[d])) * 100
            rec.append(out)
    d = pd.DataFrame(rec).drop_duplicates(["date", "code"])
    L = ["## 測試二｜停損的「大盤拖累／個股轉弱」標記", "",
         f"跌破日 {d['date'].min()} ~ {d['date'].max()}，20 日平均成交金額 ≥ 3 億的普通股，停損＝收盤 − 2×ATR14。",
         "數字是跌破後「個股 − 加權指數」的超額報酬。**正＝跌破後反而比大盤好**（停損賣在低點）。", ""]
    for k in (20, 60):
        L += [f"跌破後 {k} 個交易日：", "", "| 分組 | 樣本 | 中位數 | 平均 | 贏大盤的比例 | 中間一半落在 |", "|---|---|---|---|---|---|"]
        for typ in ("大盤拖累", "個股轉弱"):
            L.append(row(typ, summarize(d[d.type == typ][f"ex{k}"])))
        L.append("")
    dr = d[d.type == "大盤拖累"]
    eps = sorted(set(dr["date"].str[:7]))
    L += [f"大盤拖累的跌破集中在 {len(eps)} 個月份：{'、'.join(eps)}。同一波大跌裡的樣本彼此不獨立，"
          "真正獨立的只有幾波，下面分波看：", "",
          "| 月份 | 樣本 | 20 日超額中位數 | 60 日超額中位數 |", "|---|---|---|---|"]
    for m, g in dr.groupby(dr["date"].str[:7]):
        L.append(f"| {m} | {len(g)} | {fmt(g['ex20'].median())} | {fmt(g['ex60'].median())} |")
    return L, d


# ---------------- 測試三 ----------------
def pct_rank(s, win=250, minp=120):
    return s.rolling(win, min_periods=minp).apply(lambda x: (x <= x[-1]).mean() * 100, raw=True)


def episodes(mask, gap=20):
    days, last = [], None
    for i, (d, m) in enumerate(mask.items()):
        if m and (last is None or i - last >= gap):
            days.append(d)
        if m:
            last = i
    return days


def test_heat(w, twii):
    c = w["close"]
    ret = c / c.shift(1) - 1
    ld = (ret <= -0.095).sum(axis=1)
    lu = (ret >= 0.095).sum(axis=1)
    margin = (w["margin_bal"] * c * 1000).sum(axis=1, min_count=500)
    ld5, lu5 = ld.rolling(5).sum(), lu.rolling(5).sum()
    p_ld, p_lu, p_m = pct_rank(ld5), pct_rank(lu5), pct_rank(margin)
    m20 = margin / margin.shift(20) - 1
    fwd = {k: (twii.shift(-k) / twii - 1) * 100 for k in (20, 60)}
    sig = {"恐慌（跌停 5 日合計 ≥ 95 分位）": p_ld >= 95,
           "恐慌＋融資出清（再加融資 20 日 ≤ −5%）": (p_ld >= 95) & (m20 <= -0.05),
           "偏熱（融資 ≥ 90 分位且漲停 5 日 ≥ 80 分位）": (p_m >= 90) & (p_lu >= 80)}
    L = ["## 測試三｜大盤溫度的讀法", "",
         f"期間 {c.index[120]} ~ {c.index[-1]}（前 120 天用來算分位數）。每一波只取第一天，看之後加權指數報酬。", "",
         "| 訊號 | 波數 | 每一波第一天 | 之後 20 日（每波） | 之後 60 日（每波） |", "|---|---|---|---|---|"]
    for name, m in sig.items():
        ep = episodes(m.fillna(False).iloc[120:])
        f20 = "、".join(fmt(fwd[20].get(d)) for d in ep)
        f60 = "、".join(fmt(fwd[60].get(d)) for d in ep)
        L.append(f"| {name} | {len(ep)} | {'、'.join(ep) or '—'} | {f20 or '—'} | {f60 or '—'} |")
    base20, base60 = fwd[20].iloc[120:].mean(), fwd[60].iloc[120:].mean()
    L += ["", f"對照：同期間任何一天之後 20 日平均 {fmt(base20)}、60 日平均 {fmt(base60)}。",
          "（估算的融資金額含上櫃、用收盤價乘張數，跟證交所公布的上市融資金額不同，只看相對高低。）"]
    return L


def main():
    w, twii = load_all()
    L = ["# 回測：track_stock 規則與心法數字（2026-11-14）", "",
         "規則在跑之前寫死（見 research/playbook/backtest_playbook.py 開頭），沒有因為結果調整。"
         f"資料：tw-market-scanner history/（{w['adj'].index[0]} ~ {w['adj'].index[-1]}），價格除權息還原，營收只用已公布的。", ""]
    l1, _ = test_revenue()
    l2, _ = test_stops(w, twii)
    l3 = test_heat(w, twii)
    L += l1 + [""] + l2 + [""] + l3
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
