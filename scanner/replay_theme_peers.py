"""回測：國外強度的標籤（國外在動／國外抗跌／平常）之後，台股主題有沒有贏全市場。只是研究，不進每日流程。

10-27 的國外強度回測程式當時沒有存進 repo，這支是重做的，並多分出「國外抗跌」。
標籤和每天的算法一樣（scanner.theme_peers.compute：只用日期 < 台股日期的國外收盤）。
台股主題 = themes.yaml 每個主題用最新市值選前 N 大、整段期間固定（和 theme_peers.tw_weekly 一樣），等權。
結果的算法、區塊與事件的定義都和 scanner/replay_us_sectors.py 一樣（共用它的函式）。

用法：python -m scanner.replay_theme_peers [--root .]
輸出：replay/theme_peers_states.json（摘要）、docs/replay-theme-peers.md（表格）
"""
import os
import json
import logging
import argparse
from datetime import datetime, timezone

import pandas as pd

from scanner import history, score, theme_peers, themes
from scanner import replay_us_sectors as R

log = logging.getLogger("replay_theme_peers")
STATES = ("國外在動", "國外抗跌", "平常")


def label_states(closes, peers, dates, cfg, start):
    """{主題: 狀態 Series（index=台股日期）}。start 之前不算（台股族群門檻資料還不夠）。"""
    rows = {t: [None] * len(dates) for t in peers}
    for i, d in enumerate(dates):
        if i < start:
            continue
        for r in theme_peers.compute(closes, peers, d, {}, cfg)[0]:
            rows[r["theme"]][i] = None if r["strong"] is None else (r["label"] or "平常")
    return {t: pd.Series(v, index=dates, dtype=object) for t, v in rows.items()}


def top_members(close, shares, tcfg):
    last = close.ffill().iloc[-1]
    out = {}
    for t, v in (tcfg.get("themes") or {}).items():
        cs = [str(s["code"]) for s in v.get("stocks") or []]
        cs = [c for c in cs if c in close.columns and shares.get(c) and last.get(c) == last.get(c)]
        out[t] = sorted(cs, key=lambda c: -last[c] * shares[c])[:tcfg.get("top_n", 3)]
    return out


def run(root=".", closes=None):
    rules = score.load_rules(root)
    cfg, ucfg = rules["theme_peers"], R.load(root)
    peers = theme_peers.load(root)
    close, avg_turn, _ = R.tw_panel(root, ucfg)
    dates = list(close.index)
    if closes is None:
        closes, failed = theme_peers.fetch_yahoo(sorted({t for v in peers.values() for t in v}), period="4y")
        log.info(f"Yahoo 抓不到：{failed}")
    start = ucfg["signal"]["window"]
    states = label_states(closes, peers, dates, cfg, start)
    liquid = avg_turn >= ucfg["min_avg_turnover_billion"] * 1e8
    bench = R.basket_index(close, liquid & close.notna())
    top = top_members(close, history.load_shares(root), themes.load(root) or {})
    cost = ucfg["cost_pct"]
    res = []
    for th, st in states.items():
        codes = top.get(th) or []
        if not codes:
            continue
        idx = R.basket_index(close[codes], close[codes].notna())
        row = {"name": th, "members": codes, "days": {s: int((st == s).sum()) for s in STATES},
               "blocks": {}, "events": {}, "by_year": {}}
        for h in ucfg["hold"]:
            exc = R.fwd(idx, h) - R.fwd(bench, h)
            b = R.blocks(st, exc, h, start)
            row["blocks"][str(h)] = {s: R.stats([e for _, x, e in b if x == s], cost) for s in STATES}
            row["blocks"][str(h)]["全部"] = R.stats([e for _, _, e in b], cost)
            row["events"][str(h)] = {s: R.stats([e for _, _, e in R.events(st, exc, h, s)], cost)
                                     for s in ("國外在動", "國外抗跌")}
            if h == 20:
                row["_blocks20"] = b
                for y in sorted({d[:4] for d, _, _ in b}):
                    row["by_year"][y] = {s: R.stats([e for d, x, e in b if x == s and d[:4] == y], cost)
                                         for s in STATES}
        res.append(row)
    pooled = pooled_test(res)
    for r in res:
        r.pop("_blocks20", None)
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "period": [dates[start], dates[-1]], "trading_days": len(dates) - start,
            "rules_version": rules["version"], "hold": ucfg["hold"], "themes": res, "pooled": pooled}


def pooled_test(res):
    """同 replay_us_sectors.pooled_test，狀態名稱換成這裡的。"""
    renamed = [{"_blocks20": [(d, {"國外在動": "在漲", "國外抗跌": "抗跌"}.get(s, s), e) for d, s, e in r["_blocks20"]]}
               for r in res]
    p = R.pooled_test(renamed)
    return {"國外在動": p["在漲"], "國外抗跌": p["抗跌"]}


def report(out):
    hz = [str(h) for h in out["hold"]]
    L = [f"回測期間 {out['period'][0]} 到 {out['period'][1]}（{out['trading_days']} 個交易日），規則版本 {out['rules_version']}。"
         f"產生：{out['generated_at'][:10]}。", "",
         "數字＝之後 h 個交易日，主題前 3 大（等權）比全市場平均多賺的中位數｜勝率｜扣 0.4% 成本後的勝率｜樣本數。"
         "⚠ 表示樣本 < 10。國外對照名單與台股主題名單都是 2026-10 才列的，偏樂觀。", "",
         "## 全部主題放一起（20 日區塊，以日期為單位）", "",
         "| 標籤 | 區塊數 | 比同一天「平常」的主題多賺（平均） | t | 多賺的區塊比例 |", "|---|---|---|---|---|"]
    for k, v in out["pooled"].items():
        L.append(f"| {k} | {v['blocks']} | {v['mean_diff']:+.2f} 個百分點 | {v['t']} | {v['positive_share']:.0%} |"
                 if v["blocks"] else f"| {k} | 0 | — | — | — |")
    # 全部主題合起來的區塊（每個主題每個區塊一筆；同一天的主題不獨立，只看大概）
    L += ["", "## 全部主題合起來（每個主題 × 每個區塊一筆）", "", "| 持有 | 國外在動 | 國外抗跌 | 平常 |", "|---|---|---|---|"]
    for h in hz:
        agg = {s: {"n": 0} for s in STATES}
        for s in STATES:
            vals = []
            for r in out["themes"]:
                st = r["blocks"][h][s]
                if st["n"]:
                    vals.append(st)
            n = sum(v["n"] for v in vals)
            agg[s] = {"n": n, "median": (sum(v["median"] * v["n"] for v in vals) / n) if n else None,
                      "win": (sum(v["win"] * v["n"] for v in vals) / n) if n else None,
                      "win_after_cost": (sum(v["win_after_cost"] * v["n"] for v in vals) / n) if n else None}
        L.append(f"| {h} 日 | " + " | ".join(R._cell(agg[s]).replace("+", "+") for s in STATES) + " |")
    L.append("")
    L.append("（上表的「中位數」是各主題中位數依樣本數加權平均，只看大概。）")
    for h in hz:
        L += ["", f"## 各主題（每 {h} 個交易日看一次，不重疊）：持有 {h} 日", "",
              "| 主題 | 國外在動 | 國外抗跌 | 平常 | 全部 |", "|---|---|---|---|---|"]
        for r in out["themes"]:
            b = r["blocks"][h]
            L.append(f"| {r['name']} | {R._cell(b['國外在動'])} | {R._cell(b['國外抗跌'])} | {R._cell(b['平常'])} | {R._cell(b['全部'])} |")
    return "\n".join(L) + "\n"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    root = ap.parse_args().root
    out = run(root)
    os.makedirs(os.path.join(root, "replay"), exist_ok=True)
    with open(os.path.join(root, "replay", "theme_peers_states.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(root, "docs", "replay-theme-peers.md"), "w", encoding="utf-8") as f:
        f.write("# 回測：國外在動／國外抗跌之後，台股主題的表現\n\n" + report(out))
    log.info(f"完成：{len(out['themes'])} 個主題，全部放一起 {out['pooled']}")


if __name__ == "__main__":
    main()
