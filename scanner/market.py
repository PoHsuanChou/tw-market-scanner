"""網站總覽頁用的市場資料：美股指數、台股指數、匯率、市場燈號，以及全部股票的最新收盤價。

為什麼放在公開 repo：這些全是公開的市場價格，不含任何人的持倉；放這裡每天由免費的 Actions 更新，
網站（私人 repo 的每日監控被額度卡住時也一樣）直接讀 out/market.json 與 out/prices.json。
卡片現價用 prices.json：這裡輸出「全部」股票的收盤價，網站自己查卡片上的那一檔，
所以不會洩漏使用者追蹤哪些股票。

資料來源：
  美股 SOX／NASDAQ／S&P500／VIX／美債 10 年、美元兌台幣：Yahoo（yfinance）。GitHub 雲端偶爾會被擋，
    抓不到的那一項保留上一次的資料並記在 failed，網站會顯示它的日期，不會把舊的當成最新。
  加權指數、台積電、0050：自己累積的證交所資料（history/），不用 Yahoo。
市場燈號沿用私人 repo 的規則（config/settings.yaml 的 market_mode）：
  恐慌：VIX ≥ 30 或 SOX 20 日跌 ≥ 15%；Risk Off：VIX ≥ 25 且 NASDAQ 或 SOX 20 日跌超過 5%；
  Risk On：VIX < 18 且 NASDAQ、SOX 5 日都上漲；其他＝中性。

用法：python -m scanner.market [--root .]
"""
import os
import json
import logging
import argparse
from datetime import datetime, timezone

import pandas as pd

log = logging.getLogger("market")

YAHOO = {"spx": "^GSPC", "ixic": "^IXIC", "sox": "^SOX", "vix": "^VIX", "tnx": "^TNX", "twd": "TWD=X"}
KEEP = 70                      # 每條序列最多保留幾個交易日（網站畫 60 日線圖）
MODE = {"panic_vix": 30, "panic_sox_20d": -15, "riskoff_vix": 25, "riskoff_20d": -5, "riskon_vix": 18}


def closed_only(series, now=None):
    """去掉「還沒收盤的今天」。Yahoo 在美股盤中會回當天的即時價，網站寫的是「前一晚收盤」，
    所以美東時間 16:30 以前，不採用美東當天日期的那一筆（FX 也一併處理，簡單且保守）。"""
    from zoneinfo import ZoneInfo
    et = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    if et.hour * 60 + et.minute >= 16 * 60 + 30:
        return series
    return [p for p in series if p[0] < et.strftime("%Y-%m-%d")]


def fetch_yahoo(symbols=YAHOO, period="4mo"):
    """回傳 ({key: [[日期, 收盤], ...]}, [失敗的 key])。單一項目失敗不影響其他。"""
    out, failed = {}, []
    try:
        import yfinance as yf
    except Exception as e:  # noqa: BLE001
        log.error(f"沒有 yfinance：{e}")
        return {}, list(symbols)
    for key, sym in symbols.items():
        try:
            h = yf.Ticker(sym).history(period=period)["Close"].dropna()
            ser = closed_only([[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in h.items()])
            if len(ser) < 6:
                raise ValueError(f"只有 {len(ser)} 筆")
            out[key] = ser[-KEEP:]
        except Exception as e:  # noqa: BLE001
            log.warning(f"{sym} 抓不到：{e}")
            failed.append(key)
    return out, failed


def local_series(root, codes, n=KEEP):
    """從 history/daily 取指定代號最近 n 個交易日的收盤價；加權指數另從 history/index/twii.csv。"""
    d = os.path.join(root, "history", "daily")
    files = sorted(f for f in os.listdir(d) if f.endswith(".csv.gz"))[-n:] if os.path.isdir(d) else []
    out = {c: [] for c in codes}
    for f in files:
        df = pd.read_csv(os.path.join(d, f), usecols=["code", "close"], dtype={"code": str})
        day = f[: -len(".csv.gz")]
        for c, close in zip(df["code"], df["close"]):
            if c in out and close == close:
                out[c].append([day, round(float(close), 2)])
    p = os.path.join(root, "history", "index", "twii.csv")
    twii = []
    if os.path.exists(p):
        t = pd.read_csv(p, dtype={"date": str}).dropna().sort_values("date").tail(n)
        twii = [[a, round(float(b), 2)] for a, b in zip(t["date"], t["close"])]
    return {"twii": twii, "tsmc": out.get("2330", []), "0050": out.get("0050", [])}


def _pct(series, n):
    return (series[-1][1] / series[-1 - n][1] - 1) * 100 if len(series) > n else None


def market_mode(us):
    """依 us（{key: series}）判定市場燈號。資料不足回 None，不亂猜。回傳 (代碼, 判定用的數字)。"""
    need = ("vix", "sox", "ixic")
    if any(len(us.get(k) or []) < 21 for k in need):
        return None, {}
    vix = us["vix"][-1][1]
    sox5, sox20 = _pct(us["sox"], 5), _pct(us["sox"], 20)
    nas5, nas20 = _pct(us["ixic"], 5), _pct(us["ixic"], 20)
    info = {"vix": round(vix, 2), "sox5": round(sox5, 2), "sox20": round(sox20, 2),
            "nas5": round(nas5, 2), "nas20": round(nas20, 2)}
    if vix >= MODE["panic_vix"] or sox20 <= MODE["panic_sox_20d"]:
        return "panic", info
    if vix >= MODE["riskoff_vix"] and (nas20 < MODE["riskoff_20d"] or sox20 < MODE["riskoff_20d"]):
        return "riskoff", info
    if vix < MODE["riskon_vix"] and nas5 > 0 and sox5 > 0:
        return "riskon", info
    return "neutral", info


def mode_asof(us, tw_date):
    """台股 tw_date 收盤後（17:30 掃描時）能知道的市場燈號。
    那個時間美股還沒開盤，最近一次收盤是「前一個美股交易日」，所以只用日期 < tw_date 的美股資料。
    這樣燈號只取決於 (tw_date, 美股序列)，不管什麼時候重跑、補跑都會得到同一個結果，不會偷看之後的資料。"""
    cut = {k: [p for p in v if p[0] < tw_date] for k, v in us.items()}
    mode, info = market_mode(cut)
    asof = max((cut[k][-1][0] for k in ("vix", "sox", "ixic") if cut.get(k)), default=None)
    return mode, info, asof


def record_mode(root, series, rules_version):
    """把「最新一個台股交易日」的市場燈號寫進 ledger/market_modes.jsonl（只增不改，同一天只記第一筆）。
    美股資料太舊（最近一筆比台股日期早超過 5 天，例如 Yahoo 連續抓不到）就標 stale，分析時不採用。"""
    from datetime import date
    from scanner import ledger, score
    dates = score.trading_dates(root)
    if not dates:
        return None
    d = dates[-1]
    mode, info, asof = mode_asof(series, d)
    if mode is None:
        return None
    stale = asof is None or (date.fromisoformat(d) - date.fromisoformat(asof)).days > 5
    rec = {"tw_data_date": d, "mode": mode, "inputs": info, "us_asof": asof, "stale": stale,
           "rules_version": rules_version, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ledger.append(root, "market_modes", [rec], key=("tw_data_date",))
    return rec


def prices(root):
    """全部股票最新一天的收盤價 {代號: 收盤}，沒有收盤價（沒成交）的不放。"""
    d = os.path.join(root, "history", "daily")
    files = sorted(f for f in os.listdir(d) if f.endswith(".csv.gz")) if os.path.isdir(d) else []
    if not files:
        return None
    df = pd.read_csv(os.path.join(d, files[-1]), usecols=["code", "close"], dtype={"code": str}).dropna()
    return {"date": files[-1][: -len(".csv.gz")], "p": {c: round(float(v), 2) for c, v in zip(df["code"], df["close"])}}


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def build(root=".", fetch=fetch_yahoo):
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    mpath = os.path.join(root, "out", "market.json")
    prev = _load(mpath)
    fresh, failed = fetch()
    stale = []
    merged = {}
    for k in YAHOO:
        if k in fresh:
            merged[k] = fresh[k]
        elif (prev.get("series") or {}).get(k):
            merged[k] = prev["series"][k]       # 抓不到就沿用上一次，並標示
            stale.append(k)
    series = {**merged, **{k: v for k, v in local_series(root, ["2330", "0050"]).items() if v}}
    mode, info = market_mode(series)
    if mode is None and prev.get("mode"):
        mode, info = prev["mode"], prev.get("mode_inputs") or {}
        stale.append("mode")
    out = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "series": series, "mode": mode, "mode_inputs": info,
           "failed": failed, "stale": sorted(set(stale))}
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    try:
        from scanner import score
        record_mode(root, series, score.load_rules(root)["version"])
    except Exception as e:  # noqa: BLE001
        log.warning(f"記錄市場燈號失敗（不影響其他輸出）：{e}")
    px = prices(root)
    if px:
        with open(os.path.join(root, "out", "prices.json"), "w", encoding="utf-8") as f:
            json.dump(px, f, ensure_ascii=False, separators=(",", ":"))
    log.info(f"市場資料：{len(series)} 條序列，燈號 {mode}，抓不到 {failed or '無'}，股價 {len((px or {}).get('p', {}))} 檔")
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    build(ap.parse_args().root)


if __name__ == "__main__":
    main()
