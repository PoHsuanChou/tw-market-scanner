"""細分主題（config/themes.yaml）：每個主題只看市值前 N 大，用過半規則判斷主題強不強。

主題強度（2026-10-25 使用者決定）：
  前 N 大成員裡，有 3 個以上訊號的股票數 ≥ 過半門檻，主題就算「強」。
  過半門檻 = 成員數 // 2 + 1 → 3 檔要 2 檔、2 檔要 2 檔、1 檔就看那 1 檔。
  前 N 大是「當天」的市值排名（收盤價 × 發行股數），每天重算，不是用 themes.yaml 裡的順序。

themes.yaml 的 reviewed 不是 true 時，每日流程不使用主題（回傳 None）；
回放可以用 allow_draft=True 先預覽，輸出會標 draft。
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
            members.append({"code": r["code"], "name": r["name"], "cap": r["close"] * sh / 1e8, "signals": sig})
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
