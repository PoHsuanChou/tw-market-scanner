"""每週推薦：逐關過濾，每一關都記錄誰被刷掉。

關卡 id：P.industry → P.signals → P.not_priced → P.no_trap
（P.liquidity 已經在 score_stocks 的 U.liquidity 做掉了）
通過的排序：訊號數多→少，20 日報酬少→多（優先還沒漲的），成交金額多→少。
前 weekly_max 檔 sent=True；到 record_max 檔都寫進 ledger（含沒寄出的，這是回測樣本）。
沒有任何股票通過就是 0 檔，不湊數。
"""
from datetime import date

import pandas as pd

LETTER_NAMES = {"A": "價量", "B": "法人", "C": "大戶", "D": "基本面"}


def week_id(data_date):
    y, w, _ = date.fromisoformat(data_date).isocalendar()
    return f"{y}-W{w:02d}"


def is_week_close(data_date):
    """週五才產生推薦。遇到週五休市的那週會跳過（資料日期是週四），這是已知限制。"""
    return date.fromisoformat(data_date).weekday() == 4


def _count(row):
    return sum(1 for k in "ABCD" if row[k] is True)


def reason_text(r, ind):
    def yn(k, ok_text, no_text):
        v = r[k]
        return ok_text if v is True else (no_text if v is False else f"{LETTER_NAMES[k]}資料不足")
    r20 = "—" if r["r20"] is None else f"{r['r20']:+.1f}%"
    bench = "—" if r["bench_r20"] is None else f"{r['bench_r20']:+.1f}%"
    f5 = "—" if r["foreign_5d"] is None else f"{r['foreign_5d'] / 1000:+,.0f}"
    t5 = "—" if r["trust_5d"] is None else f"{r['trust_5d'] / 1000:+,.0f}"
    c = ""
    if r.get("c_basis") == "tdcc":
        c = f"（400 張以上 {r['c_big400_old']:.1f}% → {r['c_big400_now']:.1f}%）"
    elif r.get("c_basis") == "margin_proxy":
        c = "（用融資變化代理，集保歷史不足）"
    d = ""
    if r.get("d_yoy") == r.get("d_yoy") and r.get("d_yoy") is not None:
        d = f"（{r['d_ym']} 營收 YoY {r['d_yoy']:+.1f}%，比較基準 {r['d_ref_yoy']:+.1f}%）"
    return "\n".join([
        f"{r['name']} {r['code']}｜{r['industry']} 產業 {ind['score']}/4",
        yn("A", f"A 價量成立：收在月線上，20 日 {r20}（基準 {bench}）", "A 價量未成立"),
        yn("B", f"B 法人成立：外資 5 日 {f5} 張、投信 5 日 {t5} 張", "B 法人未成立"),
        yn("C", f"C 大戶成立{c}", "C 大戶未成立") + (c if r["C"] is False else ""),
        yn("D", f"D 基本面成立{d}", "D 基本面未成立") + (d if r["D"] is False else ""),
        "看錯的條件：月營收 YoY 掉回門檻以下，或投信轉為 5 日累計賣超",
    ])


def select(df, industries, rules, data_date):
    """回傳 (picks, rejects)。picks 是要寫進 ledger 的紀錄 list（已排序、含 rank 與 sent）。"""
    p = rules["picks"]
    ind = {i["industry"]: i for i in industries}
    passed, rejects = [], []
    for _, r in df.iterrows():
        r = r.to_dict()
        i = ind.get(r["industry"], {"score": 0, "delta_4w": None})
        sig = _count(r)
        fail = None
        if not (i["score"] >= p["min_industry_score"] or (i["delta_4w"] is not None and i["delta_4w"] >= p["min_industry_delta"])):
            fail = "P.industry"
        elif sig < p["min_signals"] or r["D"] is not True:
            fail = "P.signals"
        elif r["r20"] is not None and r["r20"] >= p["max_r20_pct"]:
            fail = "P.not_priced"
        elif (r["flag"] in ("notice", "disposal")
              or (r["margin_growth_5d"] is not None and r["margin_growth_5d"] > p["max_margin_growth_5d_pct"]
                  and r["r20"] is not None and r["r20"] > 0)):
            fail = "P.no_trap"
        base = {"data_date": data_date, "week": week_id(data_date), "code": r["code"], "name": r["name"],
                "industry": r["industry"], "price_at_pick": r["close"], "signals": sig,
                "A": r["A"], "B": r["B"], "C": r["C"], "D": r["D"]}
        if fail:
            if sig >= p["reject_record_min_signals"]:
                rejects.append({**base, "failed_at": fail})
        else:
            passed.append((r, i, base))
    passed.sort(key=lambda x: (-x[2]["signals"], x[0]["r20"] if x[0]["r20"] is not None else 0, -x[0]["avg_turnover"]))
    picks = []
    for n, (r, i, base) in enumerate(passed[: p["record_max"]], start=1):
        picks.append({**base, "pick_id": f"{base['week']}-{n:02d}", "rank": n, "sent": n <= p["weekly_max"],
                      "reason_text": reason_text(r, i), "failed_at": None})
    return picks, rejects
