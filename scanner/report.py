"""從 ledger 產生回測表格（Markdown）。數字全部由程式算，報告裡不手抄數字。

看什麼：
  - 中位數超額與勝率（超額 > 0 的比例），不看平均：少數大漲股會把平均拉高。
  - 每格都附樣本數 n；n < 30 的格子標「樣本不足」。
  - 5／20／60 日的窗口彼此重疊，n 看起來大，獨立樣本其實少很多。

用法：
  python -m scanner.report --base ledger_replay [--out docs/replay-tables.md]
  python -m scanner.report --base ledger
"""
import os
import argparse

import pandas as pd

from scanner import ledger

MIN_N = 30


def _df(root, name, base):
    return pd.DataFrame(ledger.read(root, name, base))


def _stats(g):
    x = g["excess"].dropna()
    return pd.Series({"n": len(x), "median": x.median() if len(x) else None,
                      "win": (x > 0).mean() if len(x) else None})


def _fmt(tbl, index_name):
    lines = [f"| {index_name} | " + " | ".join(f"{h} 日" for h in tbl.columns.levels[0]) + " |",
             "|---|" + "---|" * len(tbl.columns.levels[0])]
    for idx, row in tbl.iterrows():
        cells = []
        for h in tbl.columns.levels[0]:
            n, med, win = row[(h, "n")], row[(h, "median")], row[(h, "win")]
            if n != n or n == 0:
                cells.append("—")
                continue
            c = f"{med:+.2f}%，勝率 {win:.0%}（n={int(n)}）"
            cells.append(c + (" 樣本不足" if n < MIN_N else ""))
        lines.append(f"| {idx} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _pivot(df, by):
    t = df.groupby([by, "horizon"]).apply(_stats, include_groups=False).unstack("horizon")
    return t.swaplevel(axis=1).sort_index(axis=1)


def build(root=".", base="ledger"):
    out = _df(root, "outcomes", base)
    picks = _df(root, "picks", base)
    if out.empty:
        return f"`{base}/outcomes.jsonl` 還沒有到期的紀錄。"
    parts = []

    # 1. 推薦數量
    if not picks.empty:
        picks["variant"] = picks.get("variant", pd.Series(["base"] * len(picks))).fillna("base")
        weeks = picks.groupby("variant")["week"].nunique()
        sent = picks[picks["sent"] == True].groupby("week").size()  # noqa: E712
        parts.append("### 推薦數量\n" + "\n".join(
            f"- {v}：{int(weeks[v])} 週有推薦，共 {int((picks['variant'] == v).sum())} 筆記錄" for v in weeks.index)
            + f"\n- 實際寄出：{len(sent)} 週、{int(sent.sum()) if len(sent) else 0} 檔")

    # 2. 兩版推薦的表現（每版前 3 名，等同「如果寄出的是這版」）
    pk = out[out["ref_type"] == "pick"].copy()
    if not pk.empty:
        pk["variant"] = pk.get("variant", pd.Series(["base"] * len(pk))).fillna("base")
        top = pk[pk["rank"].fillna(99) <= 3]
        parts.append("### 兩個版本的前 3 名（相對同日 focus 股票等權中位數的超額）\n" + _fmt(_pivot(top, "variant"), "版本"))

    # 3. 對照組：依訊號數（被刷掉的 + base 推薦，同一批週五）
    rj = out[out["ref_type"] == "universe"].copy()
    if not rj.empty:
        parts.append("### 依成立訊號數（對照組：每週 focus 範圍全部股票）\n" + _fmt(_pivot(rj, "signals"), "訊號數"))
        parts.append("### 依卡在哪一關（base 版；通過 = 進入 base 候選）\n" + _fmt(_pivot(rj, "failed_at"), "關卡"))

    # 4. 主題強弱（前 N 大成員報酬中位數的超額）
    th = out[out["ref_type"] == "theme"].copy()
    if not th.empty:
        th["強弱"] = th["strong"].map({True: "強", False: "不強"}).fillna("無資料")
        parts.append("### 細分主題・單日版（前 3 大成員報酬中位數的超額）\n" + _fmt(_pivot(th, "強弱"), "主題"))
        if "swing_strong" in th:
            th["波段"] = th["swing_strong"].map({True: "波段強", False: "波段不強"}).fillna("無資料")
            parts.append("### 細分主題・波段版\n" + _fmt(_pivot(th, "波段"), "主題"))

    ev = out[out["ref_type"] == "theme_event"].copy()
    if not ev.empty:
        ev["事件"] = ev.get("kind", pd.Series(["daily"] * len(ev))).fillna("daily").map(
            {"daily": "單日版轉強當天", "swing": "波段版轉強當天"})
        parts.append("### 主題轉強事件（當天收盤後的前 3 大成員，報酬中位數的超額）\n" + _fmt(_pivot(ev, "事件"), "事件"))

    # 5. 產業分數（只顯示用，不再擋推薦）
    ind = out[out["ref_type"] == "industry"]
    if not ind.empty:
        parts.append("### 產業分數（產業內中位數報酬的超額）\n" + _fmt(_pivot(ind, "score"), "分數"))

    parts.append(f"格式：中位數超額，勝率（超額 > 0 的比例），n。n < {MIN_N} 標「樣本不足」。"
                 "5／20／60 日窗口互相重疊，獨立樣本比 n 少得多。")
    return "\n\n".join(parts)


def _cell(c):
    if not c or not c.get("n"):
        return "—"
    mark = " ⚠" if c["n"] < MIN_N else ""
    return f"{c['median']:+.2f}%｜勝率 {c['win']:.0%}｜n={c['n']}{mark}"


def by_year_md(summary):
    """replay/summary.json → 分年度 Markdown。重點是「強主題比不強主題多多少」是不是每一年都在。"""
    hz = [str(h) for h in summary["horizons"]]
    years = sorted(summary["by_year"])
    parts = [f"回放期間 {summary['period']['from']} 到 {summary['period']['to']}（{summary['period']['trading_days']} 個交易日），"
             f"規則版本 {summary['rules_version']}。⚠ 表示樣本數 < {MIN_N}。"]

    def gap_table(title, getter):
        rows = ["| 年度 | " + " | ".join(f"{h} 日" for h in hz) + " |", "|---|" + "---|" * len(hz)]
        for y in years + ["全部"]:
            src = summary["all"] if y == "全部" else summary["by_year"][y]
            cells = []
            for h in hz:
                a, b = getter(src, h)
                cells.append("—" if not a or not b or not a.get("n") or not b.get("n")
                             else f"{a['median'] - b['median']:+.2f} 個百分點（強 {a['median']:+.2f}、不強 {b['median']:+.2f}）")
            rows.append(f"| {y} | " + " | ".join(cells) + " |")
        return f"#### {title}\n" + "\n".join(rows)

    parts.append(gap_table("波段強的主題 − 不強的主題（中位數超額的差距；正數＝強的比較好）",
                           lambda s, h: ((s["themes"].get("強") or {}).get(h), (s["themes"].get("不強") or {}).get(h))))

    def level_table(title, getter):
        rows = ["| 年度 | " + " | ".join(f"{h} 日" for h in hz) + " |", "|---|" + "---|" * len(hz)]
        for y in years + ["全部"]:
            src = summary["all"] if y == "全部" else summary["by_year"][y]
            rows.append(f"| {y} | " + " | ".join(_cell(getter(src, h)) for h in hz) + " |")
        return f"#### {title}\n" + "\n".join(rows)

    parts.append(level_table("波段強的主題（絕對水準，偏樂觀）", lambda s, h: (s["themes"].get("強") or {}).get(h)))
    parts.append(level_table("不強的主題", lambda s, h: (s["themes"].get("不強") or {}).get(h)))
    parts.append(level_table("波段版轉強當天", lambda s, h: s["theme_events"].get(h)))
    for v, name in (("theme_swing_uncapped", "波段主題前 3 大、不擋已漲（現在寄出的版本）"),
                    ("theme_swing_top3", "波段主題前 3 大（擋已漲，舊寄出版）"), ("base", "原規則（不追高，2026-10-27 起不記錄）"),
                    ("momentum", "追漲版"), ("theme_top3", "單日主題前 3 大")):
        parts.append(level_table(f"推薦：{name}", lambda s, h, v=v: (s["picks"].get(v) or {}).get(h)))
    return "\n\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--base", default="ledger")
    ap.add_argument("--out")
    ap.add_argument("--by-year", action="store_true", help="讀 replay/summary.json，輸出分年度表格")
    a = ap.parse_args()
    if a.by_year:
        import json
        md = by_year_md(json.load(open(os.path.join(a.root, "replay", "summary.json"), encoding="utf-8")))
    else:
        md = build(a.root, a.base)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
