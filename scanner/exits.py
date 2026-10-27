"""出場規則與盈虧比：同一批推薦，換不同的「什麼時候賣」，看結果差多少。

為什麼要算：對答案（grade.py）是「買了固定放 N 天」，但做波段的人中途會停損、會在主題轉弱時賣，
而且只看勝率不夠——勝率 50% 也能賺錢，只要賺的時候賺得比賠的時候多。

出場規則（事先定好、只有這 4 種，不看結果再加減，避免挑到碰巧好看的規則）：
  hold      固定持有 MAX_DAYS 個交易日（對照）
  stop      收盤比進場價跌超過 STOP_PCT 就在當天收盤賣，否則持有到 MAX_DAYS
  weak      推薦時的主題全部變成「不強」的第一天收盤賣（波段版看 swing_strong、單日版看 strong）；
            沒有主題的版本（base、momentum）不適用
  stop_weak 上面兩個條件先碰到哪個就賣
另一組「賠少、賺的抱住」（LONG_RULES，最多持有 LONG_DAYS 天）：
  trail      收盤比買進後的最高收盤回落 TRAIL_PCT 才賣（一開始最高點就是進場價，所以也等於 -20% 停損）
  stop_trail 跌 STOP_PCT 停損，或從最高點回落 TRAIL_PCT，先碰到哪個就賣

每一筆算：報酬（還原價、未扣手續費與稅）、同期間 focus 電子股等權中位數的超額、持有天數、
持有期間最大跌幅（最低收盤相對進場價）。彙總：中位數、勝率、平均賺、平均賠、盈虧比、平均報酬。

用法：python -m scanner.exits --base ledger_replay [--out docs/replay-exits.md]
限制：每週一批推薦、每筆持有最多 3 個月，期間彼此重疊，獨立樣本比 n 少很多；主題名單是事後挑的（見 replay-report.md）。
"""
import argparse

import pandas as pd

from scanner import ledger, score, grade

MAX_DAYS = 60
STOP_PCT = 10.0
TRAIL_PCT = 20.0
RULES = {"hold": "固定放 3 個月", "stop": f"跌 {STOP_PCT:.0f}% 停損", "weak": "主題轉弱就賣", "stop_weak": "停損＋主題轉弱"}
# 「賠少、賺的抱住」：持有最多 6 個月，從買進後最高收盤回落 TRAIL_PCT 才賣（移動停利）
LONG_DAYS = 120
LONG_RULES = {"hold": "固定放 6 個月", "stop": f"跌 {STOP_PCT:.0f}% 停損，其餘放 6 個月",
              "trail": f"從最高點回落 {TRAIL_PCT:.0f}% 才賣", "stop_trail": f"跌 {STOP_PCT:.0f}% 停損＋從最高點回落 {TRAIL_PCT:.0f}% 才賣"}
VARIANTS = {"theme_swing_top3": ("波段主題前 3 大（擋已漲，舊寄出版）", "swing_strong"),
            "theme_swing_uncapped": ("波段主題前 3 大、不擋已漲（寄出）", "swing_strong"), "theme_top3": ("單日主題前 3 大", "strong"),
            "base": ("原規則（對照）", None), "momentum": ("追漲版", None)}
MIN_N = 30


def strong_days(theme_scores, field):
    """{(data_date, theme): bool}"""
    return {(t["data_date"], t["theme"]): t.get(field) for t in theme_scores}


def exit_index(path, d_idx, dates, themes, strong, rule):
    """path：進場日起的還原收盤（index 0 = 進場日）。回傳出場位置（相對進場日的天數）。"""
    last = len(path) - 1
    p0 = peak = path[0]
    for k in range(1, last + 1):
        p = path[k]
        if rule in ("stop", "stop_weak", "stop_trail") and p == p and (p / p0 - 1) * 100 <= -STOP_PCT:
            return k
        if rule in ("trail", "stop_trail") and p == p:
            peak = max(peak, p)
            if (p / peak - 1) * 100 <= -TRAIL_PCT:
                return k
        if rule in ("weak", "stop_weak") and themes:
            d = dates[d_idx + k]
            flags = [strong.get((d, t)) for t in themes]
            if all(f is False for f in flags):
                return k
    return last


def simulate(picks, close, dates, bench, strong_by_field, rules=RULES, max_days=MAX_DAYS, top=3):
    """回傳每筆（variant, rule）的結果 list。bench(d0, d1) → 同期間基準報酬 % 或 None。"""
    idx = {d: i for i, d in enumerate(dates)}
    out = []
    for p in picks:
        v = p.get("variant", "base")
        if v not in VARIANTS or (p.get("rank") or 99) > top:
            continue
        d0, code = p["data_date"], p["code"]
        if d0 not in idx or code not in close.columns or idx[d0] + max_days >= len(dates):
            continue
        i0 = idx[d0]
        path = close[code].iloc[i0:i0 + max_days + 1].ffill().to_numpy(dtype=float)
        if path[0] != path[0] or path[0] <= 0:
            continue
        field = VARIANTS[v][1]
        themes = p.get("themes") or []
        for rule in rules:
            if rule in ("weak", "stop_weak") and not field:
                continue
            k = exit_index(path, i0, dates, themes, strong_by_field.get(field, {}), rule)
            ret = (path[k] / path[0] - 1) * 100
            b = bench(d0, dates[i0 + k])
            out.append({"variant": v, "rule": rule, "year": d0[:4], "start_date": d0, "code": code,
                        "days": k, "ret": round(ret, 3), "excess": None if b is None else round(ret - b, 3),
                        "mdd": round((min(path[:k + 1]) / path[0] - 1) * 100, 3)})
    return out


def summarize(rows, key="excess"):
    x = pd.Series([r[key] for r in rows if r.get(key) is not None], dtype=float)
    if x.empty:
        return {"n": 0}
    win, loss = x[x > 0], x[x <= 0]
    avg_w = float(win.mean()) if len(win) else 0.0
    avg_l = float(loss.mean()) if len(loss) else 0.0
    return {"n": int(len(x)), "median": float(x.median()), "mean": float(x.mean()), "win": float((x > 0).mean()),
            "avg_win": avg_w, "avg_loss": avg_l, "ratio": (avg_w / -avg_l) if avg_l < 0 else None,
            "days": float(pd.Series([r["days"] for r in rows]).mean()),
            "mdd": float(pd.Series([r["mdd"] for r in rows]).median())}


def _row(label, s):
    if not s.get("n"):
        return f"| {label} | — | — | — | — | — | — | — | — |"
    ratio = "—" if s["ratio"] is None else f"{s['ratio']:.2f}"
    warn = " ⚠" if s["n"] < MIN_N else ""
    return (f"| {label} | {s['n']}{warn} | {s['win']:.0%} | {s['median']:+.2f}% | {s['avg_win']:+.2f}% | "
            f"{s['avg_loss']:+.2f}% | {ratio} | {s['mean']:+.2f}% | {s['days']:.0f} 天／{s['mdd']:+.1f}% |")


HEAD = ("| 出場規則 | n | 勝率 | 中位數 | 平均賺 | 平均賠 | 盈虧比 | 平均（期望值） | 平均持有／最大跌幅中位數 |\n"
        "|---|---|---|---|---|---|---|---|---|")


def report(rows, rules=RULES, max_days=MAX_DAYS, level="###"):
    parts = [f"數字是**超額**（報酬 − 同期間 focus 電子股的等權中位數），未扣手續費與稅（來回約 0.6%）。"
             f"最多持有 {max_days} 個交易日。盈虧比 = 平均賺 ÷ 平均賠；⚠ 表示 n < {MIN_N}。"]
    for v, (name, _) in VARIANTS.items():
        vr = [r for r in rows if r["variant"] == v]
        if not vr:
            continue
        lines = [f"{level} {name}", HEAD]
        for rule, label in rules.items():
            rr = [r for r in vr if r["rule"] == rule]
            if rr:
                lines.append(_row(label, summarize(rr)))
        years = sorted({r["year"] for r in vr})
        lines += ["", "分年度（平均超額／勝率／盈虧比）：", "",
                  "| 出場規則 | " + " | ".join(years) + " |", "|---|" + "---|" * len(years)]
        for rule, label in rules.items():
            cells = []
            for y in years:
                s = summarize([r for r in vr if r["rule"] == rule and r["year"] == y])
                cells.append("—" if not s.get("n") else
                             f"{s['mean']:+.2f}%／{s['win']:.0%}／{'—' if s['ratio'] is None else format(s['ratio'], '.2f')}"
                             f"（n={s['n']}{' ⚠' if s['n'] < MIN_N else ''}）")
            if any(c != "—" for c in cells):
                lines.append(f"| {label} | " + " | ".join(cells) + " |")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def run(root, base="ledger_replay", rules=RULES, max_days=MAX_DAYS):
    cfg = score.load_rules(root)
    picks = ledger.read(root, "picks", base)
    if not picks:
        return []
    dates, close, common = grade._closes(root, score.trading_dates(root)[-1])
    turnover = common.pivot(index="date", columns="code", values="turnover").sort_index()
    ts = ledger.read(root, "theme_scores", base)
    strong = {"swing_strong": strong_days(ts, "swing_strong"), "strong": strong_days(ts, "strong")}
    cache, focus = {}, {}

    def bench(d0, d1):
        if (d0, d1) not in cache:
            if d0 not in focus:
                focus[d0] = grade.focus_codes(common, turnover, d0, cfg)
            r = grade._window_rets(close, focus[d0], d0, d1)
            cache[(d0, d1)] = float(r.median()) if len(r) >= cfg["grade"].get("min_bench_stocks", 30) else None
        return cache[(d0, d1)]

    return simulate(picks, close, dates, bench, strong, rules=rules, max_days=max_days)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--base", default="ledger_replay")
    ap.add_argument("--out")
    a = ap.parse_args()
    md = ("## 持有最多 3 個月：停損與主題轉弱\n\n" + report(run(a.root, a.base))
          + "\n\n## 持有最多 6 個月：賠少、賺的抱住（移動停利）\n\n"
          + report(run(a.root, a.base, LONG_RULES, LONG_DAYS), LONG_RULES, LONG_DAYS))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
