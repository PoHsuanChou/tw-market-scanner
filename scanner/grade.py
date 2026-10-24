"""對答案：判斷到期後，算實際報酬並寫進 ledger/outcomes.jsonl。

- horizon 是「交易日」，不是日曆天。到期日 = history 裡該判斷日之後第 N 個交易日；
  還沒有那麼多天的資料就還沒到期，下次再算。
- 基準 = 同期間全部普通股報酬的中位數（和 rules.yaml 的 benchmark 一致）。
- 停牌、下市：到期日沒有收盤價就用最後一個有收盤價的日子，標 delisted=true，
  不把它從樣本拿掉（只看活下來的股票會讓結果太好看）。
- 已經在 outcomes 裡的 (ref_id, horizon) 不會重算，也不會被改。
"""
import statistics

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


def _bench(close, d0, d1):
    r = (close.loc[d1] / close.loc[d0] - 1).dropna() * 100
    return float(statistics.median(r)) if len(r) > 200 else None


def run(root, rules, data_date):
    dates, close, common = _closes(root, data_date)
    idx = {d: i for i, d in enumerate(dates)}
    done = {(r["ref_id"], r["horizon"]) for r in ledger.read(root, "outcomes")}
    new = []

    def add(ref_type, ref_id, d0, h, ret, bench, delisted, extra=None):
        new.append({"ref_type": ref_type, "ref_id": ref_id, "horizon": h, "start_date": d0,
                    "end_date": dates[idx[d0] + h], "ret": None if ret is None else round(ret, 3),
                    "bench_ret": None if bench is None else round(bench, 3),
                    "excess": None if ret is None or bench is None else round(ret - bench, 3),
                    "delisted": delisted, **(extra or {})})

    for h in rules["grade"]["horizons"]:
        for p in ledger.read(root, "picks"):
            d0 = p["data_date"]
            if (p["pick_id"], h) in done or d0 not in idx or idx[d0] + h >= len(dates) or p["code"] not in close.columns:
                continue
            d1 = dates[idx[d0] + h]
            ret, dl = _ret(close, p["code"], d0, d1)
            add("pick", p["pick_id"], d0, h, ret, _bench(close, d0, d1), dl, {"sent": p["sent"]})
        for r in ledger.read(root, "industry_scores"):
            d0 = r["data_date"]
            ref = f"{d0}|{r['industry']}"
            if (ref, h) in done or d0 not in idx or idx[d0] + h >= len(dates):
                continue
            d1 = dates[idx[d0] + h]
            members = common[(common["date"] == d0) & (common["industry"].map(
                lambda x: rules["universe"]["industry_alias"].get(x, x)) == r["industry"])]["code"]
            rets = [_ret(close, c, d0, d1) for c in members if c in close.columns]
            rets = [x for x in rets if x[0] is not None]
            if not rets:
                continue
            mean = sum(x[0] for x in rets) / len(rets)
            add("industry", ref, d0, h, mean, _bench(close, d0, d1), any(x[1] for x in rets),
                {"score": r["score"], "n_members": len(rets)})
    return ledger.append(root, "outcomes", new, key=("ref_id", "horizon"))
