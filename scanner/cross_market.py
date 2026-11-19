"""跨市場對照：每個主題把台股成員和國外同類公司（config/theme_peers.yaml）放在一起，
列出價格位置與成交量。只顯示、記錄，不判斷誰領先，不產生任何訊號，也不影響推薦。

每檔股票列出（都和「自己」比，跨市場才比得起來）：
  r20       近 20 個交易日漲跌（%）
  rel20     r20 − 自己市場大盤的 r20（台股用加權指數；國外用各自的大盤，見 INDEX）
  pos60     收盤在近 60 個交易日高低區間的位置（0＝最低、100＝最高）
  off_high  距離近 60 日最高收盤還差幾 %（0＝今天就是新高）
  since_high 近 60 日最高收盤是幾個交易日前（0＝今天）
  range60   近 60 日高低差 ÷ 最低（%），越小代表整理得越窄
  vol_ratio 近 5 日平均成交量 ÷ 近 60 日平均成交量（> 1 代表最近量比較大）
  spark     近 60 日收盤（給網頁畫小走勢）

台股用還原收盤（scanner.adjust）；成交量用原始股數（遇到分割的那段期間量比會失真）。
國外用 Yahoo（auto_adjust），每檔標出自己的資料日期 asof；國外和台股的日期可能差一天（時差）。

輸出：
  out/cross_market.json         網站「波段 › 跨市場」用
  ledger/cross_market.jsonl     每個台股交易日 × 股票一筆（只增不改），之後要驗證看法時才有不偷看的紀錄

用法：python -m scanner.cross_market [--root .]
"""
import os
import json
import logging
import argparse
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yaml

from scanner import adjust, ledger, score, theme_peers

log = logging.getLogger("cross_market")

WIN = 60          # 區間長度（交易日）
R = 20            # 漲跌天數
VOL_SHORT = 5     # 近期量

# 國外股票依代號結尾對到自己的大盤（Yahoo 代號）；沒有結尾的視為美股
INDEX = {".T": "^N225", ".KS": "^KS11", ".KQ": "^KQ11", ".SS": "000001.SS", ".SZ": "399001.SZ",
         ".HK": "^HSI", ".AS": "^AEX", ".PA": "^FCHI", ".VI": "^ATX", ".DE": "^GDAXI", ".L": "^FTSE",
         "": "^GSPC"}
MARKET = {".T": "日", ".KS": "韓", ".KQ": "韓", ".SS": "中", ".SZ": "中", ".HK": "港", ".AS": "歐",
          ".PA": "歐", ".VI": "歐", ".DE": "歐", ".L": "歐", "": "美"}

# 網頁顯示用的公司名（沒列的就顯示代號）
NAMES = {
    "GFS": "格芯", "TSEM": "高塔半導體", "AVGO": "博通", "MRVL": "邁威爾", "6526.T": "Socionext",
    "MCHP": "微芯", "NXPI": "恩智浦", "STM": "意法半導體", "6723.T": "瑞薩", "MU": "美光",
    "000660.KS": "SK 海力士", "005930.KS": "三星電子", "WDC": "威騰", "SNDK": "SanDisk", "285A.T": "鎧俠",
    "SIMO": "慧榮", "AMKR": "艾克爾", "BESI.AS": "BESI", "6146.T": "迪思科", "6315.T": "TOWA",
    "FORM": "FormFactor", "6855.T": "日本電子材料", "COHU": "Cohu", "3436.T": "SUMCO", "4063.T": "信越化學",
    "SOI.PA": "Soitec", "QRVO": "Qorvo", "SWKS": "思佳訊", "4062.T": "揖斐電", "ATS.VI": "AT&S",
    "4004.T": "Resonac", "4182.T": "三菱瓦斯化學", "600183.SS": "生益科技", "TTMI": "TTM",
    "002463.SZ": "滬電股份", "300476.SZ": "勝宏科技", "VRT": "維諦", "MOD": "Modine", "6594.T": "尼得科",
    "6981.T": "村田", "6976.T": "太陽誘電", "6762.T": "TDK", "VSH": "威世", "ETN": "伊頓", "ENS": "EnerSys",
    "SMCI": "美超微", "DELL": "戴爾", "CLS": "Celestica", "COHR": "Coherent", "LITE": "Lumentum",
    "AAOI": "應用光電", "5803.T": "藤倉", "5801.T": "古河電工", "GLW": "康寧", "FN": "Fabrinet",
    "300308.SZ": "中際旭創", "300502.SZ": "新易盛", "APH": "安費諾", "CRDO": "Credo", "TEL": "泰科電子",
    "ANET": "Arista", "CSCO": "思科",
}


def suffix(ticker):
    i = ticker.rfind(".")
    return ticker[i:] if i > 0 else ""


def metrics(close, volume, bench):
    """close／volume／bench：依日期排好的 Series。資料不夠回傳 None。"""
    close = close.dropna()
    if len(close) < R + 1:
        return None
    w = close.iloc[-WIN:]
    last, hi, lo = float(w.iloc[-1]), float(w.max()), float(w.min())
    r20 = (last / float(close.iloc[-R - 1]) - 1) * 100
    br20 = None
    if bench is not None:
        b = bench.dropna()
        b = b[b.index <= close.index[-1]]
        if len(b) > R:
            br20 = (float(b.iloc[-1]) / float(b.iloc[-R - 1]) - 1) * 100
    vol_ratio = None
    v = volume.reindex(close.index).dropna() if volume is not None else None
    if v is not None:
        v = v[v > 0]
    if v is not None and len(v) >= WIN // 2:
        vw = v.iloc[-WIN:]
        vol_ratio = float(vw.iloc[-VOL_SHORT:].mean() / vw.mean())
    rnd = lambda x, n=1: None if x is None or x != x else round(x, n)
    return {
        "asof": str(close.index[-1])[:10],
        "close": rnd(last, 2),
        "r20": rnd(r20),
        "rel20": rnd(r20 - br20) if br20 is not None else None,
        "pos60": rnd((last - lo) / (hi - lo) * 100 if hi > lo else 100.0, 0),
        "off_high": rnd((last / hi - 1) * 100),
        "since_high": int(len(w) - 1 - int(np.argmax(w.values))),
        "range60": rnd((hi / lo - 1) * 100 if lo > 0 else None),
        "vol_ratio": rnd(vol_ratio, 2),
        "n_days": int(len(w)),
        "spark": [rnd(float(x), 2) for x in w.values],
    }


def tw_frames(root, codes, n_days):
    dates = score.trading_dates(root)[-n_days:]
    frames = [pd.read_csv(os.path.join(root, "history", "daily", f"{d}.csv.gz"), dtype={"code": str},
                          usecols=["date", "code", "name", "close", "volume"]) for d in dates]
    h = pd.concat(frames, ignore_index=True)
    h = h[h["code"].isin(codes)]
    close = h.pivot(index="date", columns="code", values="close").sort_index()
    close = adjust.adjusted(root, close)
    vol = h.pivot(index="date", columns="code", values="volume").sort_index()
    names = h.drop_duplicates("code", keep="last").set_index("code")["name"].to_dict()
    return close, vol, names


def twii(root):
    p = os.path.join(root, "history", "index", "twii.csv")
    s = pd.read_csv(p).set_index("date")["close"]
    return s.sort_index()


def fetch_yahoo(tickers, period="6mo"):
    """回傳 (close 寬表, volume 寬表, 失敗清單)。index 是日期字串。"""
    try:
        import yfinance as yf
        df = yf.download(list(tickers), period=period, auto_adjust=True, progress=False)
    except Exception as e:  # noqa: BLE001
        log.error(f"Yahoo 下載失敗：{e}")
        return pd.DataFrame(), pd.DataFrame(), list(tickers)
    close, vol = df["Close"], df["Volume"]
    close.index = close.index.strftime("%Y-%m-%d")
    vol.index = vol.index.strftime("%Y-%m-%d")
    failed = [t for t in tickers if t not in close.columns or close[t].dropna().shape[0] < R + 1]
    return close, vol, failed


def build(root=".", fetch=fetch_yahoo):
    peers = theme_peers.load(root)
    with open(os.path.join(root, "config", "themes.yaml"), encoding="utf-8") as f:
        tcfg = yaml.safe_load(f).get("themes") or {}
    names_order = [t for t in tcfg if t in peers]
    if not names_order:
        log.info("沒有同時出現在 themes.yaml 和 theme_peers.yaml 的主題，略過")
        return None
    rules = score.load_rules(root)
    tw_date = score.trading_dates(root)[-1]

    codes = sorted({str(s["code"]) for t in names_order for s in tcfg[t].get("stocks") or []})
    close, vol, names = tw_frames(root, codes, WIN + R + 5)
    bench_tw = twii(root)

    tickers = sorted({p for t in names_order for p in peers[t]})
    idx = sorted({INDEX[suffix(p)] for p in tickers if suffix(p) in INDEX})
    fclose, fvol, failed = fetch(tickers + idx)
    failed = [t for t in failed if t in tickers]

    themes_out, rows_ledger = [], []
    for t in names_order:
        members = []
        for s in tcfg[t].get("stocks") or []:
            c = str(s["code"])
            if c not in close.columns:
                continue
            m = metrics(close[c], vol[c] if c in vol.columns else None, bench_tw)
            if m:
                members.append({"id": c, "name": names.get(c) or s.get("name"), "mkt": "台", **m})
        for p in peers[t]:
            if p not in fclose.columns:
                continue
            ix = INDEX.get(suffix(p))
            m = metrics(fclose[p], fvol[p] if p in fvol.columns else None,
                        fclose[ix] if ix in fclose.columns else None)
            if m:
                members.append({"id": p, "name": NAMES.get(p, p), "mkt": MARKET.get(suffix(p), "?"), **m})
        themes_out.append({"theme": t, "members": members})
        for m in members:
            rows_ledger.append({"tw_data_date": tw_date, "theme": t, "id": m["id"], "mkt": m["mkt"],
                                **{k: m[k] for k in ("asof", "close", "r20", "rel20", "pos60", "off_high",
                                                     "since_high", "range60", "vol_ratio")}})

    usable = sum(1 for th in themes_out for m in th["members"] if m["mkt"] != "台")
    out = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tw_data_date": tw_date,
           "rules_version": rules["version"], "window": WIN, "r_days": R, "vol_short": VOL_SHORT,
           "failed": failed, "foreign_ok": usable,
           "note": "只顯示、不判斷誰領先、不影響推薦；國外名單是事後列的",
           "themes": themes_out}
    path = os.path.join(root, "out", "cross_market.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    n = ledger.append(root, "cross_market", rows_ledger, key=("tw_data_date", "theme", "id")) if usable else 0
    log.info(f"跨市場對照：{tw_date}，{len(themes_out)} 個主題，國外 {usable} 檔、抓不到 {len(failed)} 檔，ledger 新增 {n}")
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    build(ap.parse_args().root)


if __name__ == "__main__":
    main()
