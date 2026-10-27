"""國外強度：每個台股主題的國外同類公司（config/theme_peers.yaml）最近有沒有在漲。只顯示、記錄，不影響推薦。

國外強度 F = 該主題對照股 window 日報酬中位數 − 全部對照股 window 日報酬中位數（個百分點）。
  F ≥ strong_pp：「國外在動」。另外附上台股那邊的 rel20（theme_scores：前 3 大 20 日報酬中位數 − 基準），
  F ≥ strong_pp 且 rel20 ≤ 0 標成 lead（國外在動、台灣還沒動），只是給網頁顯示的標籤。
回測（2023-10～2026-10，名單事後列、偏樂觀）：F ≥ 5 的主題之後 1 個月超額中位數多約 3 個百分點；
「台灣還沒動」這個條件本身沒有多加分。

不偷看：台股 d 日 17:30 跑的時候，只用日期 < d 的國外收盤（和 market.mode_asof 一樣），補跑、重跑結果一致。
Yahoo 抓不到時：out/theme_peers.json 沿用上一次並標 stale，不寫 ledger（不把舊資料當成當天的判斷）。

輸出：
  out/theme_peers.json        網站主題頁用
  ledger/theme_peers.jsonl    每個台股交易日 × 主題一筆（只增不改）

用法：python -m scanner.theme_peers [--root .]
"""
import os
import json
import logging
import argparse
from datetime import date, datetime, timezone

import yaml

from scanner import ledger, score

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
        f = None if not got or allmed is None else round(_median([r for _, r, _ in got]) - allmed, 2)
        t_rel = tw_rel20.get(theme)
        strong = None if f is None else f >= cfg["strong_pp"]
        rows.append({"theme": theme, "foreign": f, "tw_rel20": t_rel, "strong": strong,
                     "lead": None if strong is None or t_rel is None else bool(strong and t_rel <= 0),
                     "n_peers": len(got), "n_listed": len(tickers),
                     "peers": [{"ticker": t, "r": round(r, 2), "asof": d} for t, r, d in got],
                     "missing": [t for t in tickers if t not in rets]})
    asof = max((d for _, d in rets.values()), default=None)
    return rows, asof


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
    closes, failed = fetch(tickers)
    rows, asof = compute(closes, peers, tw_date, _tw_rel20(root, tw_date), cfg)
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
           "strong_pp": cfg["strong_pp"], "stale": stale, "failed": failed,
           "note": "只顯示、不影響推薦；名單事後列，回測偏樂觀",
           "themes": sorted(rows, key=lambda r: (r["foreign"] is None, -(r["foreign"] or 0)))}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    n = 0
    if not stale:
        n = ledger.append(root, "theme_peers",
                          [{"tw_data_date": tw_date, "rules_version": rules["version"], "foreign_asof": asof,
                            **{k: r[k] for k in ("theme", "foreign", "tw_rel20", "strong", "lead", "n_peers")},
                            "peers": {p["ticker"]: p["r"] for p in r["peers"]}} for r in rows],
                          key=("tw_data_date", "theme"))
    log.info(f"國外強度：{tw_date}（國外到 {asof}），{usable}/{len(rows)} 個主題，"
             f"國外在動 {sum(1 for r in rows if r['strong'])} 個，抓不到 {len(failed)} 檔，ledger 新增 {n}")
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    build(ap.parse_args().root)


if __name__ == "__main__":
    main()
