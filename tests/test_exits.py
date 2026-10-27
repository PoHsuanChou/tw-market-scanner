import pandas as pd

from scanner import exits

DATES = [f"2026-01-{d:02d}" for d in range(1, 11)]


def _close(values):
    return pd.DataFrame({"1111": values}, index=DATES)


def _pick(variant="theme_swing_top3", themes=("T",)):
    return {"variant": variant, "rank": 1, "data_date": DATES[0], "code": "1111", "themes": list(themes)}


def _run(values, strong=None, variant="theme_swing_top3", max_days=5):
    strong = strong or {}
    return {r["rule"]: r for r in exits.simulate([_pick(variant)], _close(values), DATES, lambda a, b: 0.0,
                                                  {"swing_strong": strong, "strong": strong}, max_days=max_days)}


def test_hold_keeps_full_window():
    r = _run([100, 95, 85, 90, 110, 120, 0, 0, 0, 0])
    assert r["hold"]["days"] == 5 and r["hold"]["ret"] == 20.0
    assert r["hold"]["mdd"] == -15.0


def test_stop_sells_on_first_close_below_limit():
    r = _run([100, 95, 89, 90, 110, 120, 0, 0, 0, 0])
    assert r["stop"]["days"] == 2 and r["stop"]["ret"] == -11.0


def test_weak_sells_when_all_themes_turn_weak():
    strong = {(DATES[1], "T"): True, (DATES[2], "T"): None, (DATES[3], "T"): False}
    r = _run([100, 101, 102, 103, 104, 105, 0, 0, 0, 0], strong)
    assert r["weak"]["days"] == 3                 # None（資料不足）不算轉弱
    assert r["stop_weak"]["days"] == 3


def test_weak_rule_skipped_for_variants_without_themes():
    r = _run([100] * 10, variant="base")
    assert set(r) == {"hold", "stop"}


def test_trail_sells_after_drop_from_peak():
    r = {x["rule"]: x for x in exits.simulate([_pick()], _close([100, 120, 150, 125, 119, 160, 0, 0, 0, 0]), DATES,
                                              lambda a, b: 0.0, {}, rules=exits.LONG_RULES, max_days=6)}
    assert r["trail"]["days"] == 4 and r["trail"]["ret"] == 19.0     # 150 → 119 回落超過 20%
    assert r["hold"]["days"] == 6


def test_summary_ratio():
    rows = [{"excess": e, "days": 1, "mdd": 0} for e in (10, 10, -5, -5)]
    s = exits.summarize(rows)
    assert s["win"] == 0.5 and s["ratio"] == 2.0 and s["mean"] == 2.5
