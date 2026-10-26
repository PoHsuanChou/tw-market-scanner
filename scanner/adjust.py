"""股價還原：除權息、減資、分割等公司行動會讓成交價出現「假的」跳空，算報酬前必須還原。

history/daily 存的是當天實際成交價（沒還原）。沒還原的話：
  - 除息 5 元會被當成「虧損 5 元」；
  - 減資、分割（1 股變 4 股）會被當成 -75% 的崩跌或暴漲，報酬完全錯誤。

還原方式（向後還原，以最新價格為基準）：對每個事件，ex_date 之前的所有收盤價 × factor。
  factor = 事件後的參考價 ÷ 事件前的收盤價
  - kind=div        除權息：證交所 TWT49U、櫃買 exDailyQ（官方計算結果表）
  - kind=reduction  減資：證交所 TWTAUU（恢復買賣參考價 ÷ 停止買賣前收盤價）
  - kind=jump       偵測到的未知跳空：還原所有已知事件後，單日漲跌仍超過 ±10.5%（漲跌幅限制是 10%）
                    就視為公司行動（例如上櫃減資、ETF 分割），factor = 當日收盤 ÷ 前一日收盤。
                    這會把那一天的真實漲跌一併吃掉（最多 10%），但比 -75% 的假崩跌好很多。
                    新上市前 5 個交易日沒有漲跌幅限制，不偵測。

輸出 history/adjust.csv（code, ex_date, factor, kind），只增不改（同 code、ex_date、kind 去重）。
每日掃描（scanner.fetch）會把當天的事件補進來；歷史回放前用
  python -m scanner.adjust build --from 2023-08-01 --to 2026-10-22
一次補齊。
"""
import os
import re
import logging
import argparse
from datetime import date, timedelta

import pandas as pd

from scanner import normalize as N
from scanner.http import get_json, FetchError

log = logging.getLogger("adjust")

TWSE_EX = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
TWSE_RED = "https://www.twse.com.tw/rwd/zh/reducation/TWTAUU"
TPEX_EX = "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ"
JUMP = 0.105
IPO_QUIET_DAYS = 5
FACTOR_RANGE = (0.2, 6.0)          # 超出這個範圍的因子幾乎一定是資料錯誤，丟掉並記 log


def _iso(s):
    """'114年08月01日' / '114/02/12' → '2025-08-01'。"""
    m = re.findall(r"\d+", str(s))
    if len(m) < 3:
        return None
    y = int(m[0]) + 1911 if int(m[0]) < 1911 else int(m[0])
    return f"{y}-{int(m[1]):02d}-{int(m[2]):02d}"


def _event(code, ex_date, prev, ref, kind):
    prev, ref = N.to_num(prev), N.to_num(ref)
    if not code or not ex_date or not prev or not ref or prev <= 0 or ref <= 0:
        return None
    f = ref / prev
    if not (FACTOR_RANGE[0] <= f <= FACTOR_RANGE[1]):
        log.warning(f"{code} {ex_date} 因子 {f:.3f} 超出合理範圍，略過")
        return None
    return {"code": str(code).strip(), "ex_date": ex_date, "factor": round(f, 6), "kind": kind}


def _months(start, end):
    d = date.fromisoformat(start).replace(day=1)
    stop = date.fromisoformat(end)
    while d <= stop:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield d, min(nxt - timedelta(days=1), stop)
        d = nxt


def fetch_events(start, end, get=get_json, sleep=2.0):
    """向證交所、櫃買抓 start～end 之間的除權息與減資事件（以月為單位查詢）。"""
    out = []
    for a, b in _months(start, end):
        sa, sb = a.strftime("%Y%m%d"), b.strftime("%Y%m%d")
        try:
            p = get(TWSE_EX, params={"startDate": sa, "endDate": sb, "response": "json"}, retries=3, sleep=sleep)
            for r in (p or {}).get("data") or []:
                e = _event(r[1], _iso(r[0]), r[3], r[4], "div")
                e and out.append(e)
            p = get(TPEX_EX, params={"startDate": a.strftime("%Y/%m/%d"), "endDate": b.strftime("%Y/%m/%d"),
                                     "response": "json"}, retries=3, sleep=sleep)
            for t in (p or {}).get("tables") or []:
                for r in t.get("data") or []:
                    e = _event(r[1], _iso(r[0]), r[3], r[4], "div")
                    e and out.append(e)
        except FetchError as e:
            log.error(f"{a:%Y-%m} 除權息抓取失敗：{e}")
    for y in range(date.fromisoformat(start).year, date.fromisoformat(end).year + 1):
        try:
            p = get(TWSE_RED, params={"startDate": f"{y}0101", "endDate": f"{y}1231", "response": "json"}, retries=3, sleep=sleep)
            for r in (p or {}).get("data") or []:
                e = _event(r[1], _iso(r[0]), r[3], r[4], "reduction")
                e and out.append(e)
        except FetchError as e:
            log.error(f"{y} 減資抓取失敗：{e}")
    return out


def path(root):
    return os.path.join(root, "history", "adjust.csv")


def load(root):
    p = path(root)
    if not os.path.exists(p):
        return pd.DataFrame(columns=["code", "ex_date", "factor", "kind"])
    return pd.read_csv(p, dtype={"code": str})


def save(root, events):
    """合併並去重寫回（同 code、ex_date、kind 只留一筆）。回傳新增筆數。"""
    old = load(root)
    new = pd.DataFrame(events, columns=["code", "ex_date", "factor", "kind"])
    allx = pd.concat([old, new], ignore_index=True).drop_duplicates(["code", "ex_date", "kind"], keep="first")
    allx = allx.sort_values(["ex_date", "code", "kind"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(path(root)), exist_ok=True)
    allx.to_csv(path(root), index=False)
    return len(allx) - len(old)


def apply(close, events):
    """close：寬表（index=日期字串、columns=代號）。回傳還原後的新寬表，不改動原表。"""
    adj = close.copy()
    if events is None or len(events) == 0:
        return adj
    import numpy as np
    ev = events[events["code"].isin(adj.columns)]
    idx = np.array(list(adj.index), dtype="U10")
    for code, g in ev.groupby("code"):
        g = g.sort_values("ex_date")
        exd = np.array(list(g["ex_date"]), dtype="U10")
        f = g["factor"].to_numpy(dtype=float)
        suffix = np.concatenate([np.cumprod(f[::-1])[::-1], [1.0]])      # 事件 k 以後全部因子的乘積
        k = np.searchsorted(exd, idx, side="right")                      # 日期 t 之後（ex_date > t）的第一個事件
        adj[code] = adj[code].to_numpy(dtype=float) * suffix[k]
    return adj


def detect_jumps(close, known=None):
    """還原已知事件後，找出仍然超過漲跌幅限制的單日跳空。回傳事件 list（kind=jump）。

    判斷規則（目的是抓公司行動，又不要把真實的大漲大跌當成還原）：
      - 前一個交易日有成交價（相鄰兩天）：單日漲跌超過 ±10.5% 就是公司行動，因為正常交易不可能超過 10%。
      - 前面有停牌或沒成交的空檔：復牌後本來就可能累積大幅漲跌，只有極端值（因子 < 0.5 或 > 2.5，
        例如 1 拆 10 的分割）才算公司行動。
      - 新上市前 5 個交易日沒有漲跌幅限制，不偵測。
      - 已知除權息／減資的當天不偵測：漲跌幅限制是以「參考價」算的，還原後的前收盤會讓合法的 10% 看起來超過 10.5%。
    """
    adj = apply(close, known) if known is not None else close
    skip = set(zip(known["code"], known["ex_date"])) if known is not None and len(known) else set()
    out = []
    idx = list(adj.index)
    first_i = {c: (adj[c].notna().values.argmax() if adj[c].notna().any() else None) for c in adj.columns}
    for code in adj.columns:
        vals = adj[code].values
        fi = first_i[code]
        prev_val, prev_i = None, None
        for i, v in enumerate(vals):
            if v != v:
                continue
            if prev_val is not None and prev_val > 0:
                r = v / prev_val
                adjacent = prev_i == i - 1
                new_listing = fi is not None and fi > 0 and (i - fi) < IPO_QUIET_DAYS
                hit = (abs(r - 1) > JUMP) if adjacent else (r < 0.5 or r > 2.5)
                if hit and not new_listing and (code, idx[i]) not in skip:
                    out.append({"code": code, "ex_date": idx[i], "factor": round(float(r), 6), "kind": "jump"})
            prev_val, prev_i = v, i
    return out


def known_events(root):
    ev = load(root)
    return ev[ev["kind"] != "jump"]


def adjusted(root, close):
    """給評分與對答案用：把寬表收盤價依 history/adjust.csv 全部還原。"""
    return apply(close, load(root))


def build(root, start, end, sleep=2.0, get=get_json):
    from scanner import score
    evs = fetch_events(start, end, get=get, sleep=sleep)
    added = save(root, evs)
    dates, hist = score.load_daily(root, trading_end(root), n=10**6)
    close = hist.pivot(index="date", columns="code", values="close").sort_index()
    close = close.loc[(close.index >= start) & (close.index <= end)]
    jumps = detect_jumps(close, known_events(root))
    added_j = save(root, jumps)
    log.info(f"除權息／減資事件新增 {added}；偵測到 {len(jumps)} 個未知跳空（新增 {added_j}）")
    return evs, jumps


def trading_end(root):
    from scanner import score
    return score.trading_dates(root)[-1]


def update_day(root, day, get=get_json):
    """每日掃描用：補當天的事件，並檢查當天相對前一個交易日有沒有未知跳空。失敗不影響主流程。"""
    from scanner import score
    evs = fetch_events(day, day, get=get, sleep=1.0)
    n = save(root, evs)
    dates = score.trading_dates(root, day)
    if len(dates) >= 2:
        _, hist = score.load_daily(root, day, n=3)
        close = hist.pivot(index="date", columns="code", values="close").sort_index()
        j = detect_jumps(close.tail(2), known_events(root))
        j = [x for x in j if x["ex_date"] == day]
        n += save(root, j)
    return n


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "report"])
    ap.add_argument("--root", default=".")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--sleep", type=float, default=2.0)
    a = ap.parse_args()
    if a.cmd == "build":
        build(a.root, a.start, a.end, a.sleep)
    else:
        ev = load(a.root)
        print(ev.groupby("kind").size().to_dict(), "\n", ev[ev["kind"] == "jump"].tail(30).to_string())


if __name__ == "__main__":
    main()
