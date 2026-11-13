"""國外強度：每個台股主題的國外同類公司（config/theme_peers.yaml）最近有沒有在漲。只顯示、記錄，不影響推薦。

國外強度 F = 該主題對照股 window 日報酬中位數 − 全部對照股 window 日報酬中位數（個百分點）。
  F ≥ strong_pp 記成 strong（回測用的就是這個條件）。網頁標籤再看對照股本身 window 日報酬中位數（abs）：
  abs > 0 標「國外在動」；abs ≤ 0 標「國外抗跌」（大跌時只是跌得比較少，不是在漲）。
  另外附上台股那邊的 rel20（theme_scores：前 3 大 20 日報酬中位數 − 基準），
  「國外在動」且 rel20 ≤ 0 標成 lead（國外在動、台灣還沒動），只是給網頁顯示的標籤。
  抓得到的對照股少於 min_peers 檔標 few_peers（一檔股票就能左右整個主題）。
回測（2023-10～2026-10，名單事後列、偏樂觀）：F ≥ 5 的主題之後 1 個月超額中位數多約 3 個百分點；
「台灣還沒動」這個條件本身沒有多加分。

不偷看：台股 d 日 17:30 跑的時候，只用日期 < d 的國外收盤（和 market.mode_asof 一樣），補跑、重跑結果一致。
Yahoo 抓不到時：out/theme_peers.json 沿用上一次並標 stale，不寫 ledger（不把舊資料當成當天的判斷）。

輸出：
  out/theme_peers.json        網站主題頁用（含每個主題的「誰先動」，見 leadlag()；誰先動不寫 ledger）
  ledger/theme_peers.jsonl    每個台股交易日 × 主題一筆（只增不改）

用法：python -m scanner.theme_peers [--root .]
"""
import os
import json
import logging
import argparse
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import yaml

from scanner import adjust, history, ledger, score, themes

log = logging.getLogger("theme_peers")


def load(root):
    p = os.path.join(root, "config", "theme_peers.yaml")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return {k: [str(t) for t in v] for k, v in (yaml.safe_load(f).get("peers") or {}).items()}


def fetch_yahoo(tickers, period="4mo"):
    """回傳 ({ticker: [[日期, 收盤], ...]}, [失敗的 ticker])。"""
    try:
        import yfinance as yf
    except Exception as e:  # noqa: BLE001
        log.error(f"沒有 yfinance：{e}")
        return {}, list(tickers)
    out, failed = {}, []
    try:
        df = yf.download(list(tickers), period=period, auto_adjust=True, progress=False)["Close"]
    except Exception as e:  # noqa: BLE001
        log.error(f"Yahoo 下載失敗：{e}")
        return {}, list(tickers)
    for t in tickers:
        s = df[t].dropna() if t in df.columns else []
        if len(s) < 6:
            failed.append(t)
            continue
        out[t] = [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in s.items()]
    return out, failed


def _median(v):
    v = sorted(v)
    n = len(v)
    return None if not n else (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2)


def ticker_returns(closes, tw_date, window, max_age_days):
    """{ticker: (window 日報酬 %, 最後一筆日期)}，只用日期 < tw_date 的收盤；太舊或不足 window+1 筆的不放。"""
    out = {}
    for t, ser in closes.items():
        s = [p for p in ser if p[0] < tw_date]
        if len(s) <= window:
            continue
        last = s[-1][0]
        if (date.fromisoformat(tw_date) - date.fromisoformat(last)).days > max_age_days:
            continue
        out[t] = ((s[-1][1] / s[-1 - window][1] - 1) * 100, last)
    return out


def compute(closes, peers, tw_date, tw_rel20, cfg):
    """回傳 (每個主題一列, 國外資料日期)。tw_rel20：{主題: rel20 或 None}。"""
    rets = ticker_returns(closes, tw_date, cfg["window"], cfg["max_age_days"])
    allmed = _median([r for r, _ in rets.values()])
    rows = []
    for theme, tickers in peers.items():
        got = [(t, *rets[t]) for t in tickers if t in rets]
        med = None if not got else _median([r for _, r, _ in got])
        f = None if med is None or allmed is None else round(med - allmed, 2)
        t_rel = tw_rel20.get(theme)
        strong = None if f is None else f >= cfg["strong_pp"]
        # strong 只是「比其他國外對照股強」：大跌時跌得少也算，所以另外看本身有沒有漲
        label = None if not strong else ("國外在動" if med > 0 else "國外抗跌")
        rows.append({"theme": theme, "foreign": f, "abs": round(med, 2) if med is not None else None,
                     "tw_rel20": t_rel, "strong": strong, "label": label,
                     "lead": None if strong is None or t_rel is None else bool(label == "國外在動" and t_rel <= 0),
                     "few_peers": len(got) < cfg["min_peers"],
                     "n_peers": len(got), "n_listed": len(tickers),
                     "peers": [{"ticker": t, "r": round(r, 2), "asof": d} for t, r, d in got],
                     "missing": [t for t in tickers if t not in rets]})
    asof = max((d for _, d in rets.values()), default=None)
    return rows, asof


# ---------------- 誰先動（領先落後） ----------------
# 國外強度只看同一天，看不出誰先動。這裡用過去約 3 年的「每週相對報酬」算相關係數：
#   台股主題週報酬 T（前 N 大、還原價，減掉全部主題成員的中位數）
#   國外主題週報酬 F（對照股，減掉全部對照股的中位數）
#   corr(F 往前 k 週, T)：k > 0 代表國外上 k 週的相對漲跌，跟台灣這週的相對漲跌有關 → 國外先動。
# 時差：美、歐股 f 日的漲跌要到台股下一個交易日才反映，所以歸到「f 之後第一個台股交易日」
#   （美股週五算在台股下週一那一週）；日、韓、中股和台股幾乎同時交易，歸到「f 當天或之後第一個台股交易日」。
#   不這樣分的話，亞洲同一天的共同消息會被誤判成「台灣先動」。同一週（k = 0）不算領先。
#   這裡亞股最後約一小時在台股收盤後，只用來描述，不影響每天的國外強度（那邊仍只用 < 台股日期的收盤）。
# 只是描述過去的關係、只給網頁顯示；週數有限、主題和落後期數多，門檻（min_t）故意拉高。

def _weekly(daily_log):
    """日 log 報酬寬表（index=台股日期字串）→ 週加總（週五結束），整週沒資料的留空。"""
    wk = pd.to_datetime(daily_log.index).to_period("W-FRI")
    return daily_log.groupby(wk).sum(min_count=1)


def tw_weekly(root, themes_cfg, shares, upto=None):
    """{主題: 週相對報酬 Series}。前 N 大用最新市值（還原價 × 現在股數）選，整段期間固定。"""
    dates = score.trading_dates(root, upto)
    top_n = themes_cfg.get("top_n", 3)
    members = {t: [str(s["code"]) for s in v.get("stocks") or []] for t, v in (themes_cfg.get("themes") or {}).items()}
    codes = sorted({c for v in members.values() for c in v})
    frames = [pd.read_csv(os.path.join(root, "history", "daily", f"{d}.csv.gz"), dtype={"code": str},
                          usecols=["date", "code", "close"]) for d in dates]
    if not frames:
        return {}
    hist = pd.concat(frames, ignore_index=True)
    close = hist[hist["code"].isin(codes)].pivot(index="date", columns="code", values="close").sort_index()
    close = adjust.adjusted(root, close)
    wk = _weekly(np.log(close / close.shift(1)))
    last = close.ffill().iloc[-1]
    univ = wk.median(axis=1)
    out = {}
    for t, cs in members.items():
        cs = [c for c in cs if c in wk.columns and shares.get(c) and last.get(c) == last.get(c)]
        top = sorted(cs, key=lambda c: -last[c] * shares[c])[:top_n]
        if top:
            out[t] = wk[top].median(axis=1) - univ
    return out


ASIA = (".T", ".KS", ".KQ", ".SS", ".SZ", ".HK")


def foreign_weekly(closes, peers, tw_dates):
    """{主題: 週相對報酬 Series}。國外日報酬先依時差歸到台股交易日（見上面說明）。"""
    tw = np.array(tw_dates, dtype="U10")
    cols = {}
    for t, ser in closes.items():
        if len(ser) < 2:
            continue
        d = np.array([p[0] for p in ser], dtype="U10")
        v = np.log(np.array([p[1] for p in ser], dtype=float))
        k = np.searchsorted(tw, d[1:], side="left" if t.endswith(ASIA) else "right")
        ok = k < len(tw)
        s = pd.Series(np.diff(v)[ok]).groupby(tw[k[ok]]).sum()
        # 上市前留空；上市後國外休市、台股有開的日子是 0（沒有新消息）
        cols[t] = s.reindex([x for x in tw_dates if x >= s.index.min()]).fillna(0.0)
    if not cols:
        return {}
    wk = _weekly(pd.DataFrame(cols).reindex(tw_dates))
    univ = wk.median(axis=1)
    out = {}
    for th, ts in peers.items():
        ts = [x for x in ts if x in wk.columns]
        if ts:
            out[th] = wk[ts].median(axis=1) - univ
    return out


def leadlag(f, t, cfg):
    """f、t：同一個主題的國外、台股週相對報酬。回傳 {weeks, corr: {k: r}, same_week, verdict, lag_weeks}。"""
    K = cfg["max_lag_weeks"]
    df = pd.DataFrame({"f": f, "t": t}).dropna()
    n = len(df)
    if n < cfg["min_weeks"]:
        return {"weeks": n, "corr": {}, "same_week": None, "verdict": None, "lag_weeks": None}
    weeks = pd.period_range(min(f.index.min(), t.index.min()), max(f.index.max(), t.index.max()), freq="W-FRI")
    f_all, t_all = f.reindex(weeks), t.reindex(weeks)   # 補齊整週休市（例如春節），shift 才是真的差 k 週
    corr = {}
    for k in range(-K, K + 1):
        j = pd.DataFrame({"f": f_all.shift(k), "t": t_all}).dropna()
        corr[k] = round(float(j["f"].corr(j["t"])), 3) if len(j) > 2 else None
    thr = cfg["min_t"] / np.sqrt(n)
    best = lambda ks: max(((corr[k], k) for k in ks if corr[k] is not None), default=(None, None))
    pos, kp = best(range(1, K + 1))
    neg, kn = best(range(-K, 0))
    if pos is not None and pos >= thr and (neg is None or pos >= neg):
        verdict, lag = "國外先動", kp
    elif neg is not None and neg >= thr:
        verdict, lag = "台灣先動", -kn
    elif corr[0] is not None and corr[0] >= thr:
        verdict, lag = "同步", 0
    else:
        verdict, lag = "看不出關係", None
    return {"weeks": n, "corr": {str(k): v for k, v in corr.items()}, "same_week": corr[0],
            "verdict": verdict, "lag_weeks": lag}


def leadlag_all(root, closes, peers, cfg):
    tcfg = themes.load(root)
    if not tcfg:
        return {}
    tw = tw_weekly(root, tcfg, history.load_shares(root))
    fw = foreign_weekly(closes, peers, score.trading_dates(root))
    return {th: leadlag(fw[th], tw[th], cfg) for th in peers if th in fw and th in tw}


def _tw_rel20(root, tw_date):
    """當天 theme_scores 的 rel20（同一天有多個規則版本時用最後一筆）。"""
    out = {}
    for r in ledger.read(root, "theme_scores"):
        if r["data_date"] == tw_date:
            out[r["theme"]] = r.get("rel20")
    return out


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def build(root=".", fetch=fetch_yahoo):
    peers = load(root)
    if not peers:
        log.info("沒有 config/theme_peers.yaml，略過國外強度")
        return None
    rules = score.load_rules(root)
    cfg = rules["theme_peers"]
    dates = score.trading_dates(root)
    if not dates:
        return None
    tw_date = dates[-1]
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    path = os.path.join(root, "out", "theme_peers.json")
    tickers = sorted({t for v in peers.values() for t in v})
    closes, failed = fetch(tickers, period=cfg["leadlag"]["period"])
    rows, asof = compute(closes, peers, tw_date, _tw_rel20(root, tw_date), cfg)
    try:
        ll = leadlag_all(root, closes, peers, cfg["leadlag"])
    except Exception as e:  # noqa: BLE001  誰先動只是附加資訊，算不出來不影響國外強度
        log.warning(f"誰先動算不出來：{e}")
        ll = {}
    for r in rows:
        r["leadlag"] = ll.get(r["theme"])
    usable = sum(1 for r in rows if r["foreign"] is not None)
    stale = usable < len(rows) / 2               # 一半以上主題算不出來 → 視為抓取失敗
    if stale:
        prev = _load(path)
        if prev.get("themes"):
            prev.update(stale=True, failed=failed, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(prev, f, ensure_ascii=False, separators=(",", ":"))
            log.warning(f"國外強度：只算出 {usable}/{len(rows)} 個主題，沿用上一次（{prev.get('tw_data_date')}）並標 stale")
            return prev
    out = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tw_data_date": tw_date,
           "foreign_asof": asof, "rules_version": rules["version"], "window": cfg["window"],
           "strong_pp": cfg["strong_pp"], "min_peers": cfg["min_peers"],
           "leadlag_cfg": {k: cfg["leadlag"][k] for k in ("max_lag_weeks", "min_t")},
           "stale": stale, "failed": failed,
           "note": "只顯示、不影響推薦；名單事後列，回測偏樂觀",
           "themes": sorted(rows, key=lambda r: (r["foreign"] is None, -(r["foreign"] or 0)))}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    n = 0
    if not stale:
        n = ledger.append(root, "theme_peers",
                          [{"tw_data_date": tw_date, "rules_version": rules["version"], "foreign_asof": asof,
                            **{k: r[k] for k in ("theme", "foreign", "abs", "tw_rel20", "strong", "label", "lead",
                                                 "n_peers")},
                            "peers": {p["ticker"]: p["r"] for p in r["peers"]}} for r in rows],
                          key=("tw_data_date", "theme"))
    log.info(f"國外強度：{tw_date}（國外到 {asof}），{usable}/{len(rows)} 個主題，"
             f"國外在動 {sum(1 for r in rows if r['label'] == '國外在動')} 個、"
             f"抗跌 {sum(1 for r in rows if r['label'] == '國外抗跌')} 個，抓不到 {len(failed)} 檔，ledger 新增 {n}")
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    build(ap.parse_args().root)


if __name__ == "__main__":
    main()
