"""第二階段研究共用工具（只在 research/，不改 scanner/ 與 config/）。計畫：research/phase2/plan.md。

重用 scanner 的資料與函式，不另建資料管線：
  台股：scanner.replay_us_sectors.tw_panel（history/daily + adjust.csv 還原）、basket_index
  國外：scanner.theme_peers.fetch_yahoo / compute（國外強度標籤的原始定義）
Yahoo 資料抓一次存在 research/phase2/data/（附抓取時間），之後都讀這份，確保可重現。

這裡的修正（只用在新版本，原始結果不動）：
  B2：預測用途時，所有國外市場（含日韓中）都歸到「之後第一個台股交易日」，見 foreign_daily()
  B4：H2 可設 skip=1，等於 d+1 收盤進場（網站 d 日 17:30 才公布）
"""
import os
import sys
import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scanner import theme_peers, themes, score, history  # noqa: E402
from scanner import replay_us_sectors as RU  # noqa: E402
from scanner import replay_theme_peers as RT  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")
ASIA = theme_peers.ASIA
SINGLE_PEER = ("化合物半導體", "封測", "記憶體-控制器/模組", "BBU/備援電池")   # 國外只抓得到 1 檔（2026-11-13）


# ---------------- 資料 ----------------

def cached_yahoo(name, tickers, period="4y", fetch=None):
    """research/phase2/data/{name}.json；有就讀、沒有才抓。回傳 {ticker: [[日期, 收盤], ...]}。"""
    p = os.path.join(DATA, f"{name}.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)["closes"]
    closes, failed = (fetch or theme_peers.fetch_yahoo)(sorted(set(tickers)), period=period)
    os.makedirs(DATA, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "period": period,
                   "tickers": sorted(set(tickers)), "failed": failed, "closes": closes}, f, separators=(",", ":"))
    return closes


def save_out(df, name):
    os.makedirs(OUT, exist_ok=True)
    df.to_csv(os.path.join(OUT, name), index=False, encoding="utf-8")


class TW:
    """台股資料（一次載入）：還原收盤、前 20 日均成交、加權指數、全市場等權基準、主題成員。"""

    def __init__(self, root=ROOT):
        ucfg = RU.load(root)
        self.close, self.avg_turn, self.ind = RU.tw_panel(root, ucfg)
        self.dates = list(self.close.index)
        liquid = self.avg_turn >= ucfg["min_avg_turnover_billion"] * 1e8
        self.bench_ew = RU.basket_index(self.close, liquid & self.close.notna())
        tw = score.index_series(root)
        self.twii = (tw.reindex(self.dates) / tw.reindex(self.dates).iloc[0])
        tcfg = themes.load(root) or {}
        self.members = {t: [str(s["code"]) for s in v.get("stocks") or []] for t, v in (tcfg.get("themes") or {}).items()}
        self.top3 = RT.top_members(self.close, history.load_shares(root), tcfg)
        r = self.close.pct_change()
        self.daily = np.log1p(r.where(r.abs() <= 0.3))                 # 同 basket_index：超過 ±30% 當資料錯誤

    def theme_index(self, theme, weights="all_ew"):
        codes = [c for c in (self.members[theme] if weights == "all_ew" else self.top3.get(theme, [])) if c in self.close.columns]
        if not codes:
            return None
        return RU.basket_index(self.close[codes], self.close[codes].notna())


def foreign_daily(closes, tw_dates, asia_same_day=False):
    """國外日 log 報酬，歸到台股交易日（寬表）。asia_same_day=False 是 B2 修正後（全部歸到下一個台股日）；
    True 是原本 foreign_weekly 的歸法（日韓中同一天），只用來重現原始結果。上市前留空、之後休市日為 0。"""
    tw = np.array(tw_dates, dtype="U10")
    cols = {}
    for t, ser in closes.items():
        if len(ser) < 2:
            continue
        d = np.array([p[0] for p in ser], dtype="U10")
        v = np.log(np.array([p[1] for p in ser], dtype=float))
        side = "left" if (asia_same_day and t.endswith(ASIA)) else "right"
        k = np.searchsorted(tw, d[1:], side=side)
        ok = k < len(tw)
        s = pd.Series(np.diff(v)[ok]).groupby(tw[k[ok]]).sum()
        cols[t] = s.reindex([x for x in tw_dates if x >= s.index.min()]).fillna(0.0)
    return pd.DataFrame(cols).reindex(tw_dates)


# ---------------- H1：國外過去一段 → 台灣下一段（主題間排名） ----------------

def bounds_weekly(dates, every=4, offset=0):
    """每 4 週一個切點（週五為一週結束，切點 = 該週最後一個台股交易日的位置）。offset 錯開 0～3 週。"""
    wk = pd.to_datetime(pd.Index(dates)).to_period("W-FRI")
    last = pd.Series(range(len(dates)), index=wk).groupby(level=0).max()
    full = last.reindex(pd.period_range(wk.min(), wk.max(), freq="W-FRI"))   # 整週休市也算一週
    pos = list(full.to_numpy())
    out = [pos[i] for i in range(3 + offset, len(pos), every)]
    return [int(x) for x in out if x == x]


def bounds_month(dates):
    m = pd.to_datetime(pd.Index(dates)).to_period("M")
    return [int(x) for x in pd.Series(range(len(dates)), index=m).groupby(level=0).max().to_numpy()]


def _window(daily, i0, i1):
    """(i0, i1] 每欄的 log 報酬合計；那段每天都有資料的欄才算。"""
    if i1 >= len(daily) or i0 < 0:
        return pd.Series(dtype=float)
    seg = daily.iloc[i0 + 1:i1 + 1]
    s = seg.sum(min_count=1)
    return s[seg.notna().sum() == len(seg)]


def h1_panel(tw_daily, fx_daily, members, peers, bounds, skip=0, tw_agg="mean", lag=1, min_themes=5):
    """每個切點 b_k 一段：fp = 國外 (b_{k-lag}, b_{k-lag+1}]、tp = 台灣 (b_{k-1}, b_k]、tn = 台灣 (b_k+skip, b_{k+1}+skip]。
    主題值：台灣成員 tw_agg（mean＝等權）、國外對照股中位數（和國外強度一樣）。每段把主題排名（百分位 − 0.5）。"""
    rows = []
    for k in range(lag, len(bounds) - 1):
        i, i_next = bounds[k], bounds[k + 1]
        fx_w = _window(fx_daily, bounds[k - lag], bounds[k - lag + 1])
        tw_p = _window(tw_daily, bounds[k - 1], i)
        tw_n = _window(tw_daily, i + skip, i_next + skip)
        if tw_n.empty:
            continue
        for th, cs in members.items():
            ps = [p for p in peers.get(th, []) if p in fx_w.index]
            ms_p = [c for c in cs if c in tw_p.index]
            ms_n = [c for c in cs if c in tw_n.index]
            if not ps or not ms_p or not ms_n:
                continue
            agg = np.mean if tw_agg == "mean" else np.median
            rows.append((tw_daily.index[i], th, float(np.median(fx_w[ps])), float(agg(tw_p[ms_p])), float(agg(tw_n[ms_n]))))
    d = pd.DataFrame(rows, columns=["date", "theme", "fp", "tp", "tn"])
    if d.empty:
        return d
    d = d[d.groupby("date")["theme"].transform("count") >= min_themes].copy()
    for c in ("fp", "tp", "tn"):
        d[c + "_r"] = d.groupby("date")[c].rank(pct=True) - 0.5
    return d


def ic_series(panel, x="fp_r", y="tn_r"):
    return panel.groupby("date").apply(lambda g: g[x].corr(g[y]), include_groups=False).dropna()


def fm_series(panel, xs=("fp_r", "tp_r"), y="tn_r"):
    """每段截面迴歸 y ~ 1 + xs，回傳每段係數（DataFrame，index=date）。"""
    out = {}
    for d, g in panel.groupby("date"):
        X = np.c_[np.ones(len(g)), g[list(xs)].to_numpy()]
        if len(g) <= X.shape[1]:
            continue
        b, *_ = np.linalg.lstsq(X, g[y].to_numpy(), rcond=None)
        out[d] = b[1:]
    return pd.DataFrame.from_dict(out, orient="index", columns=list(xs))


def tercile_spread(panel, score_col="fp_r", y="tn", cost_pct=0.4):
    """每段：score 前 1/3 主題的 y 平均 − 後 1/3（log 報酬 ×100 ≈ %），扣多空兩邊成本。"""
    def one(g):
        q = g[score_col].rank(pct=True)
        return (g.loc[q > 2 / 3, y].mean() - g.loc[q <= 1 / 3, y].mean()) * 100 - 2 * cost_pct
    return panel.groupby("date").apply(one, include_groups=False).dropna()


# ---------------- H2：國外在動標籤 → 台灣主題之後 20 日 ----------------

def h2_labels(closes, peers, dates, start=20):
    cfg = score.load_rules(ROOT)["theme_peers"]
    return RT.label_states(closes, peers, dates, cfg, start)


def h2_records(tw, labels, theme_idx, h=20, offset=0, skip=0, start=20, bench="ew"):
    """每 h 個交易日一個決策日 d（從 start+offset 開始）：每個主題的標籤、d+skip 收盤進場持有 h 日的超額（%）、
    同期基準報酬（%）。bench：ew＝全市場流動性等權、twii＝加權指數。"""
    b = tw.bench_ew if bench == "ew" else tw.twii
    rows = []
    n = len(tw.dates)
    for i in range(start + offset, n - h - skip, h):
        e0, e1 = i + skip, i + skip + h
        br = (b.iloc[e1] / b.iloc[e0] - 1) * 100
        for th, st in labels.items():
            lab = st.iloc[i]
            idx = theme_idx.get(th)
            if lab is None or idx is None:
                continue
            r = (idx.iloc[e1] / idx.iloc[e0] - 1) * 100
            if r == r:
                rows.append((tw.dates[i], th, lab, float(r - br), float(br)))
    return pd.DataFrame(rows, columns=["date", "theme", "label", "exc", "mkt"])


def h2_diff(rec, on="國外在動", off="平常", agg="mean", exclude=()):
    """每個決策日：on 主題超額 agg − off 主題超額 agg（兩邊都有才算）。"""
    r = rec[~rec["theme"].isin(exclude)]
    f = (lambda v: v.mean()) if agg == "mean" else (lambda v: v.median())
    out = {}
    for d, g in r.groupby("date"):
        a, b = g.loc[g["label"] == on, "exc"], g.loc[g["label"] == off, "exc"]
        if len(a) and len(b):
            out[d] = f(a) - f(b)
    return pd.Series(out, dtype=float).sort_index()


# ---------------- 統計 ----------------

def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        de = d * c
        h *= de
        if abs(de - 1) < 1e-12:
            break
    return h


def _betainc(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    return bt * _betacf(a, b, x) / a if x < (a + 1) / (a + b + 2) else 1 - bt * _betacf(b, a, 1 - x) / b


def p_one_sided(t, df):
    """單尾 p：P(T ≥ t)，T ~ Student t(df)。df 無限大時是常態。"""
    if t != t or df is None or df < 1:
        return float("nan")
    if df > 1e6:
        return 0.5 * math.erfc(t / math.sqrt(2))
    tail = 0.5 * _betainc(df / 2, 0.5, df / (df + t * t))
    return tail if t > 0 else 1 - tail


def nw(series, lags=2):
    """平均、Newey-West t（Bartlett）、n、單尾 p（df = n − 1）。"""
    x = np.asarray(pd.Series(series).dropna(), dtype=float)
    n = len(x)
    if n < 3:
        return {"n": n, "mean": float(x.mean()) if n else None, "median": float(np.median(x)) if n else None,
                "t": None, "p": None}
    e = x - x.mean()
    g = [np.sum(e[k:] * e[:n - k]) / n for k in range(lags + 1)]
    v = g[0] + 2 * sum((1 - k / (lags + 1)) * g[k] for k in range(1, lags + 1))
    t = float(x.mean() / math.sqrt(v / n)) if v > 0 else float("nan")
    return {"n": n, "mean": float(x.mean()), "median": float(np.median(x)), "t": t, "p": p_one_sided(t, n - 1)}


def welch(a, b):
    """mean(a) − mean(b) 的 Welch t 與單尾 p（a 比較大）。任一邊少於 2 筆回 None。"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a[a == a], b[b == b]
    if len(a) < 2 or len(b) < 2:
        return {"diff": float(a.mean() - b.mean()) if len(a) and len(b) else None, "t": None, "p": None,
                "n_a": len(a), "n_b": len(b)}
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    if va + vb == 0:
        return {"diff": float(a.mean() - b.mean()), "t": None, "p": None, "n_a": len(a), "n_b": len(b)}
    t = (a.mean() - b.mean()) / math.sqrt(va + vb)
    df = (va + vb) ** 2 / (va ** 2 / (len(a) - 1) + vb ** 2 / (len(b) - 1))
    return {"diff": float(a.mean() - b.mean()), "t": float(t), "p": p_one_sided(t, df), "n_a": len(a), "n_b": len(b)}


def corr_p(r, n):
    if r is None or r != r or n is None or n < 4 or abs(r) >= 1:
        return float("nan")
    return p_one_sided(r * math.sqrt((n - 2) / (1 - r * r)), n - 2)


def bh(p, dependent=False):
    """Benjamini-Hochberg（dependent=True 時是 Benjamini-Yekutieli）。NaN 不參與、原樣回 NaN。"""
    p = np.asarray(p, float)
    q = np.full(len(p), np.nan)
    ok = np.where(p == p)[0]
    m = len(ok)
    if not m:
        return q
    c = sum(1 / i for i in range(1, m + 1)) if dependent else 1.0
    order = ok[np.argsort(p[ok])]
    ranked = p[order] * m * c / np.arange(1, m + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    q[order] = np.minimum(ranked, 1.0)
    return q


def stationary_idx(n, mean_block, rng):
    """Politis-Romano stationary bootstrap 的一組索引（環狀）。"""
    idx = np.empty(n, dtype=int)
    idx[0] = rng.integers(n)
    p = 1 / mean_block
    for i in range(1, n):
        idx[i] = rng.integers(n) if rng.random() < p else (idx[i - 1] + 1) % n
    return idx


def boot_p_mean(series, reps=10000, block=3, seed=0):
    """H0：平均 ≤ 0。把序列減去平均（讓 H0 成立）後重抽，p = 重抽平均 ≥ 觀察平均 的比例。"""
    x = np.asarray(pd.Series(series).dropna(), float)
    if len(x) < 5:
        return float("nan")
    rng = np.random.default_rng(seed)
    obs, c = x.mean(), x - x.mean()
    bm = np.array([c[stationary_idx(len(x), block, rng)].mean() for _ in range(reps)])
    return float((np.sum(bm >= obs) + 1) / (reps + 1))


def reality_check(stat_fn, n, reps=2000, block=3, seed=0):
    """White's Reality Check（studentized，以 bootstrap 標準差標準化）。stat_fn(idx) → K 個統計量（越大越好）。
    H0：K 個策略都沒有優勢。回傳 {p, best, T}：p = P(max_k T*_k ≥ max_k T_k)。"""
    rng = np.random.default_rng(seed)
    obs = np.asarray(stat_fn(np.arange(n)), float)
    boots = np.array([stat_fn(stationary_idx(n, block, rng)) for _ in range(reps)], float)
    sd = np.nanstd(boots, axis=0, ddof=1)
    ok = (sd > 0) & (obs == obs)
    T = np.where(ok, obs / np.where(sd > 0, sd, 1), np.nan)
    Tstar = (boots - obs) / np.where(sd > 0, sd, 1)
    mx = np.nanmax(Tstar[:, ok], axis=1)
    return {"p": float((np.sum(mx >= np.nanmax(T)) + 1) / (reps + 1)), "best": int(np.nanargmax(T)), "T": T.tolist(), "k": int(ok.sum())}


def clark_west(y, f_small, f_big):
    """巢狀模型比較的逐筆 f；平均 > 0 代表大模型（多國外）比較好。"""
    y, a, b = (np.asarray(v, float) for v in (y, f_small, f_big))
    return (y - a) ** 2 - ((y - b) ** 2 - (a - b) ** 2)


# ---------------- 行情 ----------------

def regimes(twii, ma=120, thr=0.05):
    """事先寫定（plan.md §4.5）：> ma 日均線且 60 日報酬 > thr → Bull；< 均線且 < −thr → Bear；其他 Sideways；均線不足 NA。"""
    m = twii.rolling(ma).mean()
    r60 = twii / twii.shift(60) - 1
    out = pd.Series("Sideways", index=twii.index, dtype=object)
    out[(twii > m) & (r60 > thr)] = "Bull"
    out[(twii < m) & (r60 < -thr)] = "Bear"
    out[m.isna() | r60.isna()] = "NA"
    return out
