"""大盤寬度與族群營收趨勢 → out/breadth.json。只顯示，不影響推薦。2026-11-14 新增。

心法裡下班可以做、但原本沒有資料的三件事，全部用 history/ 已經有的資料算（不另外抓）：

1. 創新高家數：每天收盤價（除權息還原後）等於近 250 個交易日最高的股票數。
   「右上角股票」數量多＝大盤偏熱；修正時創新高家數縮到很少＝動能退潮。
2. 抗跌名單：近 20 個交易日比加權指數多漲（或少跌）最多的股票。
   心法：修正時找跌得比大盤少的；那些多半籌碼穩，大盤止跌後常先彈。
   只列有成交量的普通股（20 日平均成交金額 ≥ 3 億），附最新月營收年增率。
3. 族群營收趨勢：每個細分主題（themes.yaml 全部成員）月營收加總的 3／6／12 個月平均。
   心法（網通、工業電腦）：把族群營收加總畫線，平均線彎底往上的時候是好機會。
   「彎底」＝ 3 個月平均剛站上 12 個月平均，而且之前至少 3 個月在下面。
   只用「公布日 ≤ 資料日」的營收，不偷看。
4. 同業本益比：每個主題成員今天的本益比（證交所 BWIBBU_ALL、櫃買 peratio，免費），
   跟同主題中位數比。心法：同業本益比最好用。⚠️ 交易所給的是 TTM（過去四季），不是預估。
5. 曾經強攻、尾盤軟掉：當天最高比前一天收盤漲 ≥ 7%，收盤又比最高掉 ≥ 4%。
   心法：早上強、尾盤打開軟掉的，代表有人拿真金白銀攻過，隔天可以留意。只是名單。

用法：python -m scanner.breadth
"""
import json
import logging
import os

import pandas as pd

from scanner import adjust, score, themes
from scanner.http import get_json

log = logging.getLogger("breadth")

HIGH_WIN = 250          # 創新高看幾個交易日
SERIES_DAYS = 250       # 創新高家數輸出幾天
RES_WIN = 20            # 抗跌看幾天
RES_MIN_TURNOVER = 3e8  # 20 日平均成交金額門檻（元）；心法：技術面在中大型股比較有效
RES_TOP = 25
REV_MONTHS = 24
PE_TWSE = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
PE_TPEX = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
FADE_HIGH = 0.07        # 盤中最高比前一天收盤漲幾 %
FADE_DROP = 0.04        # 收盤比最高掉幾 %


def _is_common(code):
    return isinstance(code, str) and len(code) == 4 and code.isdigit() and not code.startswith("0")


def _wide(df, col):
    return df.pivot_table(index="date", columns="code", values=col, aggfunc="last").sort_index()


def new_highs(close):
    """close：還原後寬表。回傳 [(date, 創新高家數, 有效家數)]。"""
    roll = close.rolling(HIGH_WIN, min_periods=int(HIGH_WIN * 0.8)).max()
    hit = (close >= roll) & roll.notna()
    valid = roll.notna().sum(axis=1)
    out = []
    for d in close.index[-SERIES_DAYS:]:
        n = int(valid.loc[d])
        if n:
            out.append([d, int(hit.loc[d].sum()), n])
    return out


def resilient(close, turnover, names, industries, bench, rev_yoy):
    if len(close) <= RES_WIN or len(bench) <= RES_WIN:
        return {}, []
    d = close.index[-1]
    r = (close.iloc[-1] / close.iloc[-1 - RES_WIN] - 1) * 100
    b = float((bench.loc[:d].iloc[-1] / bench.loc[:d].iloc[-1 - RES_WIN] - 1) * 100)
    liq = turnover.iloc[-RES_WIN:].mean()
    ok = r.notna() & (liq >= RES_MIN_TURNOVER)
    ok &= pd.Series([_is_common(c) for c in r.index], index=r.index)
    rel = (r[ok] - b).sort_values(ascending=False)
    rows = []
    for code in rel.index[:RES_TOP]:
        rows.append({"code": code, "name": names.get(code, code), "industry": industries.get(code) or "",
                     "r20": round(float(r[code]), 1), "rel20": round(float(rel[code]), 1),
                     "turnover_yi": round(float(liq[code]) / 1e8, 1),
                     "rev_yoy": rev_yoy.get(code)})
    meta = {"date": d, "bench_r20": round(b, 1), "n_ranked": int(ok.sum())}
    return meta, rows


def latest_rev_yoy(rev_frames):
    out = {}
    for ym in sorted(rev_frames):
        for code, y in rev_frames[ym]["yoy"].items():
            if pd.notna(y):
                out[code] = round(float(y), 1)
    return out


def theme_revenue(rev_frames, cfg):
    """每個主題：月營收加總（只算每個月都有資料的成員，避免成員缺月造成假跳動）。"""
    yms = sorted(rev_frames)[-(REV_MONTHS + 12):]
    out = []
    for theme, t in (cfg.get("themes") or {}).items():
        codes = [str(s["code"]) for s in (t or {}).get("stocks") or []]
        have = [c for c in codes if all(c in rev_frames[ym].index for ym in yms[-15:])]
        if not have:
            continue
        tot = []
        for ym in yms:
            f = rev_frames[ym]
            v = f.loc[f.index.intersection(have), "revenue"]
            tot.append((ym, float(v.sum()) / 1e5 if len(v) == len(have) else None))  # 仟元 → 億
        s = pd.Series([v for _, v in tot], index=[ym for ym, _ in tot], dtype=float)
        m3, m6, m12 = s.rolling(3).mean(), s.rolling(6).mean(), s.rolling(12).mean()
        state, below = "資料不足", 0
        if m12.notna().sum() >= 4:
            above = (m3 > m12)
            for x in above.iloc[-5:-1][::-1]:
                if x:
                    break
                below += 1
            if above.iloc[-1] and below >= 3:
                state = "彎底"
            elif above.iloc[-1]:
                state = "向上" if m6.iloc[-1] >= m6.iloc[-2] else "高檔走平"
            else:
                state = "向下" if m6.iloc[-1] < m6.iloc[-2] else "低檔走平"
        r = lambda x: None if pd.isna(x) else round(float(x), 1)
        out.append({"theme": theme, "members": len(have), "of": len(codes), "state": state,
                    "last_ym": yms[-1], "yoy3": None if len(s) < 15 or not s.iloc[-15:-12].sum() else
                    round((s.iloc[-3:].sum() / s.iloc[-15:-12].sum() - 1) * 100, 1),
                    "series": [[ym, r(s[ym]), r(m3[ym]), r(m6[ym]), r(m12[ym])] for ym in s.index[-REV_MONTHS:]]})
    order = {"彎底": 0, "向上": 1, "高檔走平": 2, "低檔走平": 3, "向下": 4, "資料不足": 5}
    return sorted(out, key=lambda x: (order[x["state"]], x["theme"]))


def _f(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def fetch_pe(get=get_json):
    """{代號: {"pe":…, "pb":…}}。抓不到回空 dict（只影響顯示）。"""
    out = {}
    try:
        for r in get(PE_TWSE) or []:
            out[str(r.get("Code"))] = {"pe": _f(r.get("PEratio")), "pb": _f(r.get("PBratio"))}
    except Exception as e:  # noqa: BLE001
        log.warning(f"上市本益比抓不到：{e}")
    try:
        for r in get(PE_TPEX) or []:
            out[str(r.get("SecuritiesCompanyCode"))] = {"pe": _f(r.get("PriceEarningRatio")), "pb": _f(r.get("PriceBookRatio"))}
    except Exception as e:  # noqa: BLE001
        log.warning(f"上櫃本益比抓不到：{e}")
    return out


def theme_pe(pe, cfg, names):
    rows = []
    for theme, t in (cfg.get("themes") or {}).items():
        mem = []
        for s in (t or {}).get("stocks") or []:
            c = str(s["code"])
            v = pe.get(c) or {}
            mem.append({"code": c, "name": names.get(c) or s.get("name") or c, "pe": v.get("pe"), "pb": v.get("pb")})
        pes = sorted(m["pe"] for m in mem if m["pe"])
        med = pes[len(pes) // 2] if len(pes) % 2 else (sum(pes[len(pes) // 2 - 1:len(pes) // 2 + 1]) / 2 if pes else None)
        for m in mem:
            m["vs_med"] = round((m["pe"] / med - 1) * 100) if m["pe"] and med else None
        rows.append({"theme": theme, "median_pe": None if med is None else round(med, 1), "members": mem})
    return rows


def faded(df, data_date, names, turnover_min=RES_MIN_TURNOVER):
    """當天曾經大漲、收盤軟掉的股票。df：load_daily 的長表（含前一天）。"""
    days = sorted(df["date"].unique())
    if len(days) < 2:
        return []
    today = df[df["date"] == data_date].set_index("code")
    prev = df[df["date"] == days[-2]].set_index("code")["close"]
    t = today.join(prev.rename("prev"), how="inner")
    t = t[(t["prev"] > 0) & (t["high"] > 0) & (t["turnover"] >= turnover_min / 3)]
    up = t["high"] / t["prev"] - 1
    drop = t["close"] / t["high"] - 1
    hit = t[(up >= FADE_HIGH) & (drop <= -FADE_DROP)]
    rows = [{"code": c, "name": names.get(c, c), "industry": r.get("industry") or "",
             "high_pct": round(float(up[c]) * 100, 1), "close_pct": round(float(r["close"] / r["prev"] - 1) * 100, 1),
             "turnover_yi": round(float(r["turnover"]) / 1e8, 1)} for c, r in hit.iterrows()]
    return sorted(rows, key=lambda x: -x["turnover_yi"])[:30]


def build(root=".", get=get_json):
    dates = score.trading_dates(root)
    if not dates:
        raise SystemExit("history/daily 沒有資料")
    data_date = dates[-1]
    _, df = score.load_daily(root, data_date, n=HIGH_WIN + SERIES_DAYS)
    df = df[df["code"].map(_is_common)]
    close = adjust.adjusted(root, _wide(df, "close"))
    turnover = _wide(df, "turnover")
    last = df[df["date"] == data_date]
    names = dict(zip(last["code"], last["name"]))
    inds = dict(zip(last["code"], last["industry"].fillna("")))
    rev_frames = score._revenue_frames(root, data_date)
    meta, res = resilient(close, turnover, names, inds, score.index_series(root, data_date), latest_rev_yoy(rev_frames))
    cfg = themes.load(root) or {}
    out = {"data_date": data_date,
           "new_highs": {"window": HIGH_WIN, "series": new_highs(close)},
           "resilient": {**meta, "window": RES_WIN, "min_turnover_yi": RES_MIN_TURNOVER / 1e8, "rows": res},
           "theme_revenue": theme_revenue(rev_frames, cfg),
           "theme_pe": theme_pe(fetch_pe(get), cfg, names),
           "faded": {"rule": f"盤中最高比前一天收盤 ≥ +{FADE_HIGH * 100:.0f}%，收盤比最高掉 ≥ {FADE_DROP * 100:.0f}%，成交金額 ≥ 1 億",
                     "rows": faded(df, data_date, names)},
           "note": "只顯示，不影響推薦。價格已除權息還原；營收只用公布日 ≤ 資料日的資料。"}
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    with open(os.path.join(root, "out", "breadth.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    nh = out["new_highs"]["series"][-1] if out["new_highs"]["series"] else None
    print(f"{data_date}｜創新高 {nh}｜抗跌 {len(res)} 檔｜族群營收 {len(out['theme_revenue'])} 個主題")
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build(".")


if __name__ == "__main__":
    main()
