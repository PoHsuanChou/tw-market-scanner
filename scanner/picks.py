"""每週推薦：逐關過濾，每一關都記錄誰被刷掉。

關卡 id：P.industry（可關閉）→ P.signals → P.not_priced（依版本）→ P.no_trap
（P.liquidity 已經在 score_stocks 的 U.liquidity 做掉了）

兩個版本並行（rules.yaml 的 picks.variants），用同一份評分：
  base      排除 20 日已漲 ≥ 25% 的；排序 訊號數多→少、20 日報酬少→多（優先還沒漲的）。2026-10-26 起不寄，2026-10-27 起也不記推薦。
  momentum  不排除已經漲的；排序 訊號數多→少、20 日報酬多→少。只記錄不寄，用來和 base 比較。
這樣做的原因：回放顯示這一年「已經漲的」之後表現較好，但只有一年、一種行情，
不能因此推翻 SOP「不追高」的原則；兩版同時記錄，用之後的真實資料再比。
（2026-10-27 起 base 設 record: false：不寄也不記推薦，只保留關卡給對照組。
 另加 theme_swing_uncapped：波段主題版但不擋已漲 ≥ 25%，只記錄不寄。）

對照組：每週把 focus 範圍內「全部」股票寫進 pick_universe（通過的 failed_at=None，被刷掉的記關卡），
含 0、1 個訊號。只記被刷掉的會漏掉通過的股票，讓高訊號那幾列偏向「漲太多被刷掉」的股票。
每版前 weekly_max 檔 rank 1..N；只有 send=true 的版本 sent=True。
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
        f"{r['name']} {r['code']}｜{r['industry']}",
        yn("A", f"A 價量成立：收在月線上，20 日 {r20}（基準 {bench}）", "A 價量未成立"),
        yn("B", f"B 法人成立：外資 5 日 {f5} 張、投信 5 日 {t5} 張", "B 法人未成立"),
        yn("C", f"C 大戶成立{c}", "C 大戶未成立") + (c if r["C"] is False else ""),
        yn("D", f"D 基本面成立{d}", "D 基本面未成立") + (d if r["D"] is False else ""),
        "看錯的條件：月營收 YoY 掉回門檻以下，或投信轉為 5 日累計賣超",
    ])


def _gate(r, i, p, max_r20):
    """依序檢查關卡，回傳第一個沒過的關卡 id，全過回 None。max_r20=None 代表不檢查 P.not_priced。"""
    sig = _count(r)
    if p.get("industry_gate", True) and not (
            i["score"] >= p["min_industry_score"]
            or (i["delta_4w"] is not None and i["delta_4w"] >= p["min_industry_delta"])):
        return "P.industry"
    if sig < p["min_signals"] or r["D"] is not True:
        return "P.signals"
    if max_r20 is not None and r["r20"] is not None and r["r20"] >= max_r20:
        return "P.not_priced"
    if (r["flag"] in ("notice", "disposal")
            or (r["margin_growth_5d"] is not None and r["margin_growth_5d"] > p["max_margin_growth_5d_pct"]
                and r["r20"] is not None and r["r20"] > 0)):
        return "P.no_trap"
    return None


def select(df, industries, rules, data_date, universes=None):
    """回傳 (picks, universe)。
    picks：各版本的推薦紀錄（含 variant、rank、sent）。
    universe：對照組。用 base 版的關卡記錄 focus 範圍「全部」股票各自卡在哪一關（通過為 None），
      和哪一版寄出無關（寄出的若是主題版，它只看主題前 3 大，拿來當對照組會太窄）。
    universes：{"strong_theme_top": {代號: [主題]}, "strong_theme_swing_top": {...}}，
    只有設定了 universe 的版本會用到；對應的資料是 None 或沒給時，那個版本整個略過，不產生推薦。"""
    p = rules["picks"]
    variants = p["variants"]
    control = p.get("control_variant", "base")
    ind = {i["industry"]: i for i in industries}
    rows = []
    for _, r in df.iterrows():
        r = r.to_dict()
        i = ind.get(r["industry"], {"score": 0, "delta_4w": None})
        base = {"data_date": data_date, "week": week_id(data_date), "code": r["code"], "name": r["name"],
                "industry": r["industry"], "price_at_pick": r["close"], "r20": r["r20"], "signals": _count(r),
                "A": r["A"], "B": r["B"], "C": r["C"], "D": r["D"]}
        rows.append((r, i, base))

    picks, universe = [], []
    for vname, v in variants.items():
        theme_only = bool(v.get("universe"))
        theme_codes = (universes or {}).get(v.get("universe")) if theme_only else None
        if theme_only and theme_codes is None:
            continue
        passed = []
        for r, i, base in rows:
            if theme_only and r["code"] not in theme_codes:
                continue
            fail = _gate(r, i, p, v.get("max_r20_pct"))
            if fail is None:
                passed.append((r, i, base))
            if vname == control:
                universe.append({**base, "failed_at": fail})
        if v.get("record") is False:     # 只當對照組的關卡用，不產生推薦紀錄
            continue
        sign = -1 if v.get("sort_r20") == "desc" else 1
        none_r20 = float("-inf") if sign == -1 else float("inf")
        passed.sort(key=lambda x: (-x[2]["signals"],
                                   sign * (x[0]["r20"] if x[0]["r20"] is not None else none_r20 * sign),
                                   -x[0]["avg_turnover"]))
        tag = v.get("id_tag", "")          # 編號只跟版本有關，換寄出版本也不會變（base 沒有前綴）
        for n, (r, i, base) in enumerate(passed[: p["record_max"]], start=1):
            extra = {"themes": theme_codes[r["code"]]} if theme_only else {}
            picks.append({**base, **extra, "variant": vname, "pick_id": f"{base['week']}-{tag}{n:02d}", "rank": n,
                          "sent": bool(v.get("send")) and n <= p["weekly_max"],
                          "reason_text": reason_text(r, i), "failed_at": None})
    return picks, universe
