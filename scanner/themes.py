"""細分主題（config/themes.yaml）：每個主題只看市值前 N 大，用過半規則判斷主題強不強。

主題強度（2026-10-25 使用者決定）：
  前 N 大成員裡，有 3 個以上訊號的股票數 ≥ 過半門檻，主題就算「強」。
  過半門檻 = 成員數 // 2 + 1 → 3 檔要 2 檔、2 檔要 2 檔、1 檔就看那 1 檔。
  前 N 大是「當天」的市值排名（收盤價 × 發行股數），每天重算，不是用 themes.yaml 裡的順序。

themes.yaml 的 reviewed 不是 true 時，每日流程不使用主題（回傳 None）；
回放可以用 allow_draft=True 先預覽，輸出會標 draft。

波段版（2026-10-25 上線，網頁與通知預設用它）：單日版平均只強 3 天就翻掉，不適合波段。
  主題波段強 = 兩個條件都成立：
    1. 前 N 大成員 20 日報酬的中位數 > 當天 focus 股票 20 日報酬的中位數（股價趨勢）
    2. 前 N 大成員裡，過半的股票在最近 10 個交易日中，至少一半的日子有 ≥3 個訊號（訊號穩定）
  回放裡平均強 9 天、每週約 2 個轉強；這些數字是依「節奏」設計的，沒有用報酬挑門檻。
"""
import os

import yaml


def load(root, allow_draft=False):
    p = os.path.join(root, "config", "themes.yaml")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("reviewed") and not allow_draft:
        return None
    cfg["draft"] = not cfg.get("reviewed")
    return cfg


def need_for(n):
    """過半門檻：1→1、2→2、3→2。"""
    return n // 2 + 1


def score(df, cfg, shares, rules):
    """df：score_stocks 的輸出（當天每檔的 A/B/C/D 與收盤價）。
    回傳 (主題列, {代號: [它擔任前 N 大、且主題為強的主題名稱]})。
    沒有收盤價或發行股數的成員不參與排名；主題一檔有資料的成員都沒有時 strong=None。"""
    top_n = cfg.get("top_n", 3)
    min_sig = rules["themes"]["member_min_signals"]
    by_code = {r["code"]: r for r in df.to_dict("records")} if len(df) else {}
    rows, strong_top = [], {}
    for theme, v in (cfg.get("themes") or {}).items():
        members = []
        for s in v.get("stocks") or []:
            r = by_code.get(str(s["code"]))
            sh = shares.get(str(s["code"]))
            if r is None or not sh or r["close"] != r["close"]:
                continue
            sig = sum(1 for k in "ABCD" if r[k] is True)
            # 市值 = 還原後的收盤價 × 現在的股數：歷史價格如果沒還原，分割、減資前後的股數與價格對不上，排名會錯
            px = r.get("adj_close", r["close"])
            members.append({"code": r["code"], "name": r["name"], "cap": px * sh / 1e8, "signals": sig})
        members.sort(key=lambda m: -m["cap"])
        top = members[:top_n]
        n = len(top)
        strong_n = sum(1 for m in top if m["signals"] >= min_sig)
        strong = (strong_n >= need_for(n)) if n else None
        rows.append({"theme": theme, "n": n, "need": need_for(n) if n else None, "strong_members": strong_n,
                     "strong": strong, "top": [{**m, "cap": round(m["cap"], 1)} for m in top]})
        if strong:
            for m in top:
                strong_top.setdefault(m["code"], []).append(theme)
    rows.sort(key=lambda r: (not r["strong"], -(r["strong_members"] or 0), r["theme"]))
    return rows, strong_top


# ---------------- 波段版 ----------------

def swing(rows, df, sig_hist, window_dates, rules):
    """在 rows（score() 的輸出）每列加上 rel20、swing_members、swing_need、swing_strong。
    sig_hist：{(日期, 代號): 訊號數}；window_dates：含今天在內最近 lookback 個交易日。
    回傳 {代號: [它擔任前 N 大、且主題波段強的主題名稱]}。資料不足一律 None，不當成 False。"""
    cfg = rules["themes"]["swing"]
    min_sig = rules["themes"]["member_min_signals"]
    r20 = {r["code"]: r["r20"] for r in df.to_dict("records")} if len(df) else {}
    bench_vals = [v for v in r20.values() if v is not None and v == v]
    bench = sorted(bench_vals)[len(bench_vals) // 2] if bench_vals else None
    out = {}
    for row in rows:
        codes = [m["code"] for m in row["top"]]
        row.update(rel20=None, swing_members=None, swing_need=None, swing_strong=None)
        if not codes or bench is None:
            continue
        vals = [r20.get(c) for c in codes]
        if any(v is None or v != v for v in vals):
            continue
        vals.sort()
        med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
        member_ok = []
        for c in codes:
            hist = [sig_hist.get((d, c)) for d in window_dates]
            hist = [h for h in hist if h is not None]
            if len(hist) < cfg["min_known_days"]:
                member_ok = None
                break
            member_ok.append(sum(1 for h in hist if h >= min_sig) / len(hist) >= cfg["min_days_ratio"])
        if member_ok is None:
            continue
        n = len(codes)
        row["rel20"] = round(med - bench, 2)
        row["swing_members"] = sum(member_ok)
        row["swing_need"] = need_for(n)
        row["swing_strong"] = bool(med > bench and sum(member_ok) >= need_for(n))
        if row["swing_strong"]:
            for c in codes:
                out.setdefault(c, []).append(row["theme"])
    return out


# ---------------- 主題轉強事件 ----------------

def events(today_rows, prev_rows, data_date, field="strong", kind="daily"):
    """前一個交易日「不強」（False）、今天「強」→ 一筆轉強事件。
    前一天沒有該主題的紀錄（例如第一天、主題剛加入）不算轉強，避免第一天所有強主題都被當成事件。
    前一天是 None（沒資料）也不算，只有明確從 False 變 True 才算。"""
    prev = {r["theme"]: r.get(field) for r in prev_rows}
    out = []
    for r in today_rows:
        if r.get(field) is True and prev.get(r["theme"]) is False:
            out.append({"data_date": data_date, "theme": r["theme"], "kind": kind,
                        "strong_members": r["strong_members"], "swing_members": r.get("swing_members"),
                        "rel20": r.get("rel20"), "n": r["n"], "top": r["top"]})
    return out


def events_from_ledger(rows, field="strong", kind="daily"):
    """用一串 theme_scores 紀錄（多天）重建所有轉強事件，回放與補算用。"""
    by_date = {}
    for r in rows:
        by_date.setdefault(r["data_date"], []).append(r)
    dates = sorted(by_date)
    out = []
    for d0, d1 in zip(dates, dates[1:]):
        out.extend(events(by_date[d1], by_date[d0], d1, field, kind))
    return out


def history(real_rows, replay_rows, real_events, replay_events, days=120):
    """給網頁畫圖：最近 days 個交易日，每個主題每天的強成員數、成員數、強不強。
    同一天有真實紀錄就用真實的，沒有才用回放的；每天標 source（real / replay）。
    同一天同一主題有多筆（規則改版後另記的），用最後一筆。"""
    by_date = {}
    for src, rows in (("replay", replay_rows), ("real", real_rows)):
        for r in rows:
            by_date.setdefault(r["data_date"], {})[src] = by_date.get(r["data_date"], {}).get(src, []) + [r]
    dates = sorted(by_date)[-days:]
    themes_ = {}
    sources = []
    for d in dates:
        src = "real" if "real" in by_date[d] else "replay"
        sources.append(src)
        for r in by_date[d][src]:
            themes_.setdefault(r["theme"], {})[d] = r
    out = {"dates": dates, "source": sources, "themes": {}}
    for t, per in sorted(themes_.items()):
        out["themes"][t] = {
            "strong_members": [per.get(d, {}).get("strong_members") for d in dates],
            "n": [per.get(d, {}).get("n") for d in dates],
            "strong": [per.get(d, {}).get("strong") for d in dates],
            "swing_strong": [per.get(d, {}).get("swing_strong") for d in dates],
            "swing_members": [per.get(d, {}).get("swing_members") for d in dates],
            "rel20": [per.get(d, {}).get("rel20") for d in dates],
        }
    lo = dates[0] if dates else ""
    evs = [{**e, "source": "real"} for e in real_events if e["data_date"] >= lo]
    real_dates = {r["data_date"] for r in real_rows}
    evs += [{**e, "source": "replay"} for e in replay_events if e["data_date"] >= lo and e["data_date"] not in real_dates]
    out["events"] = sorted(evs, key=lambda e: (e["data_date"], e["theme"]), reverse=True)
    return out
