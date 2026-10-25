"""對答案：判斷到期後，算實際報酬並寫進 ledger/outcomes.jsonl。

- horizon 是「交易日」，不是日曆天。到期日 = history 裡該判斷日之後第 N 個交易日；
  還沒有那麼多天的資料就還沒到期，下次再算。
- 主要基準（bench_ret / excess）= 同一天 focus 範圍普通股（電子相關、流動性過關）在同期間報酬的
  等權中位數（rules.yaml grade.benchmark = ew_median_focus）。加權指數報酬另記在 bench_twii，僅供參考。
- 產業的報酬 = 產業內成分股報酬的中位數（不是平均，平均會被少數大漲股拉高）。
- 停牌、下市：到期日沒有收盤價就用最後一個有收盤價的日子，標 delisted=true，
  不把它從樣本拿掉（只看活下來的股票會讓結果太好看）。
- 已經在 outcomes 裡的 (ref_id, horizon) 不會重算，也不會被改。
"""
import pandas as pd

from scanner import ledger, score


def _closes(root, dates_needed_upto):
    dates, hist = score.load_daily(root, dates_needed_upto, n=10**6)
    common = hist[hist["code"].str.fullmatch(score.COMMON)]
    close = common.pivot(index="date", columns="code", values="close").sort_index()
    return dates, close, common


def _ret(close, code, d0, d1):
    """回傳 (報酬 %, 是否下市/停牌)。"""
    s = close[code]
    p0 = s.loc[d0]
    if p0 != p0:
        return None, False
    end = s.loc[d1]
    if end == end:
        return float(end / p0 - 1) * 100, False
    last = s.loc[d0:d1].dropna()
    return float(last.iloc[-1] / p0 - 1) * 100, True


def _twii_ret(idx, d0, d1):
    if idx is not None and d0 in idx.index and d1 in idx.index:
        return float(idx.loc[d1] / idx.loc[d0] - 1) * 100
    return None


def _window_rets(close, codes, d0, d1):
    """一組股票在 d0→d1 的報酬（%）。中途下市的用最後一個有收盤價的日子（和 _ret 一致）。"""
    sub = close.loc[d0:d1, [c for c in codes if c in close.columns]].ffill()
    r = (sub.iloc[-1] / sub.iloc[0] - 1) * 100
    return r.dropna()


def focus_codes(common, turnover, d0, rules):
    """d0 當天的 focus 範圍：電子相關產業 + 近 20 日平均成交金額過門檻（和 score_stocks 的 U.focus、U.liquidity 一致）。"""
    alias = rules["universe"]["industry_alias"]
    focus = set(rules["universe"]["focus_industries"])
    today = common[common["date"] == d0]
    inds = today["industry"].map(lambda x: alias.get(x, x) if isinstance(x, str) else "")
    codes = today.loc[inds.isin(focus), "code"]
    avg = turnover.loc[:d0].iloc[-20:].mean()
    min_turn = rules["universe"]["min_avg_turnover_billion"] * 1e8
    return [c for c in codes if avg.get(c, 0) >= min_turn]


def run(root, rules, data_date, base="ledger"):
    dates, close, common = _closes(root, data_date)
    turnover = common.pivot(index="date", columns="code", values="turnover").sort_index()
    idx = {d: i for i, d in enumerate(dates)}
    twii = score.index_series(root, data_date)
    done = {(r["ref_id"], r["horizon"]) for r in ledger.read(root, "outcomes", base)}
    method = rules["grade"].get("benchmark", "ew_median_focus")
    new = []
    cache = {}

    def bench(d0, d1):
        if (d0, d1) not in cache:
            r = _window_rets(close, focus_codes(common, turnover, d0, rules), d0, d1)
            cache[(d0, d1)] = float(r.median()) if len(r) >= rules["grade"].get("min_bench_stocks", 30) else None
        return cache[(d0, d1)]

    def add(ref_type, ref_id, d0, h, ret, delisted, extra=None):
        d1 = dates[idx[d0] + h]
        b = bench(d0, d1)
        tw = _twii_ret(twii, d0, d1)
        new.append({"ref_type": ref_type, "ref_id": ref_id, "horizon": h, "start_date": d0, "end_date": d1,
                    "ret": None if ret is None else round(ret, 3),
                    "bench_ret": None if b is None else round(b, 3), "bench_method": method,
                    "excess": None if ret is None or b is None else round(ret - b, 3),
                    "bench_twii": None if tw is None else round(tw, 3),
                    "delisted": delisted, "rules_version": rules["version"], **(extra or {})})

    ind_cache = {}

    def industry_of(d0):
        if d0 not in ind_cache:
            alias = rules["universe"]["industry_alias"]
            t = common[common["date"] == d0]
            ind_cache[d0] = {c: alias.get(i, i) for c, i in zip(t["code"], t["industry"]) if isinstance(i, str)}
        return ind_cache[d0]

    def due(ref, d0, h):
        return (ref, h) not in done and d0 in idx and idx[d0] + h < len(dates)

    for h in rules["grade"]["horizons"]:
        for p in ledger.read(root, "picks", base):
            d0 = p["data_date"]
            if not due(p["pick_id"], d0, h) or p["code"] not in close.columns:
                continue
            ret, dl = _ret(close, p["code"], d0, dates[idx[d0] + h])
            add("pick", p["pick_id"], d0, h, ret, dl,
                {"sent": p["sent"], "variant": p.get("variant", "base"), "rank": p.get("rank")})
        for p in ledger.read(root, "pick_universe", base):
            ref = f"{p['week']}|{p['code']}"
            d0 = p["data_date"]
            if not due(ref, d0, h) or p["code"] not in close.columns:
                continue
            ret, dl = _ret(close, p["code"], d0, dates[idx[d0] + h])
            add("universe", ref, d0, h, ret, dl, {"failed_at": p["failed_at"] or "通過", "signals": p["signals"]})
        for e in ledger.read(root, "theme_events", base):
            d0 = e["data_date"]
            ref = f"{d0}|E|{e['theme']}"
            if not due(ref, d0, h):
                continue
            rets = _window_rets(close, [m["code"] for m in e["top"]], d0, dates[idx[d0] + h])
            if not rets.empty:
                add("theme_event", ref, d0, h, float(rets.median()), False,
                    {"theme": e["theme"], "n_members": int(len(rets))})
        for t in ledger.read(root, "theme_scores", base):
            d0 = t["data_date"]
            ref = f"{d0}|T|{t['theme']}"
            if not t.get("top") or not due(ref, d0, h):
                continue
            rets = _window_rets(close, [m["code"] for m in t["top"]], d0, dates[idx[d0] + h])
            if rets.empty:
                continue
            add("theme", ref, d0, h, float(rets.median()), False,
                {"theme": t["theme"], "strong": t["strong"], "n_members": int(len(rets))})
        for r in ledger.read(root, "industry_scores", base):
            d0 = r["data_date"]
            ref = f"{d0}|{r['industry']}"
            if not due(ref, d0, h):
                continue
            members = [c for c in focus_codes(common, turnover, d0, rules) if industry_of(d0).get(c) == r["industry"]]
            rets = _window_rets(close, members, d0, dates[idx[d0] + h])
            if rets.empty:
                continue
            add("industry", ref, d0, h, float(rets.median()), False,
                {"score": r["score"], "n_members": int(len(rets))})
    return ledger.append(root, "outcomes", new, key=("ref_id", "horizon"), base=base)
