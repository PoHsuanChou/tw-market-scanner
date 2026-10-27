import os

import pandas as pd
import yaml

from scanner import themes, picks, score
from tests.test_picks_grade import _row, INDS


def _df(spec):
    """spec: {代號: (收盤價, 成立訊號數)}；訊號從 A 開始依序成立，D 一定成立才會通過推薦。"""
    rows = []
    for code, (close, n) in spec.items():
        flags = {k: (i < n) for i, k in enumerate("DABC")}
        rows.append(_row(code, close=close, **flags))
    return pd.DataFrame(rows)


def _cfg(**themes_):
    return {"top_n": 3, "themes": {t: {"stocks": [{"code": c} for c in codes]} for t, codes in themes_.items()}}


SHARES = {c: 1e9 for c in ("1001", "1002", "1003", "1004", "2001", "2002", "3001")}


def test_need_is_majority():
    assert [themes.need_for(n) for n in (1, 2, 3)] == [1, 2, 2]


def test_top3_by_market_cap_and_majority_rule(root):
    rules = score.load_rules(root)
    # 1004 市值最小 → 不在前 3；前 3 是 1001、1002、1003，其中 2 檔有 ≥3 訊號 → 強
    df = _df({"1001": (300, 3), "1002": (200, 4), "1003": (100, 1), "1004": (10, 4)})
    rows, strong = themes.score(df, _cfg(散熱=["1001", "1002", "1003", "1004"]), SHARES, rules)
    t = rows[0]
    assert [m["code"] for m in t["top"]] == ["1001", "1002", "1003"]
    assert (t["n"], t["need"], t["strong_members"], t["strong"]) == (3, 2, 2, True)
    assert set(strong) == {"1001", "1002", "1003"} and "1004" not in strong


def test_two_member_theme_needs_both_and_one_member_theme_is_itself(root):
    rules = score.load_rules(root)
    df = _df({"2001": (100, 3), "2002": (50, 2), "3001": (80, 3)})
    rows, strong = themes.score(df, _cfg(DRAM=["2001", "2002"], BBU=["3001"]), SHARES, rules)
    by = {r["theme"]: r for r in rows}
    assert (by["DRAM"]["need"], by["DRAM"]["strong"]) == (2, False)   # 2 檔只有 1 檔強 → 不強
    assert (by["BBU"]["need"], by["BBU"]["strong"]) == (1, True)       # 1 檔就看它自己
    assert set(strong) == {"3001"}


def test_theme_without_data_is_none_not_false(root):
    rows, _ = themes.score(_df({}), _cfg(空=["9999"]), SHARES, score.load_rules(root))
    assert rows[0]["strong"] is None and rows[0]["n"] == 0


def test_unreviewed_themes_are_not_used_unless_draft_allowed(root):
    with open(os.path.join(root, "config", "themes.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({"reviewed": False, "top_n": 3, "themes": {}}, f, allow_unicode=True)
    assert themes.load(root) is None
    assert themes.load(root, allow_draft=True)["draft"] is True


def test_theme_variant_only_picks_strong_theme_top_members(root):
    rules = score.load_rules(root)
    df = pd.DataFrame([_row("1001"), _row("1002"), _row("5555")])
    picked, _ = picks.select(df, INDS, rules, "2026-10-23", {"strong_theme_top": {"1001": ["散熱"], "1002": ["散熱"]}})
    t = [p for p in picked if p["variant"] == "theme_top3"]
    assert {p["code"] for p in t} == {"1001", "1002"} and not any(p["sent"] for p in t)
    assert t[0]["pick_id"].startswith("2026-W43-T") and t[0]["themes"] == ["散熱"]
    assert "5555" in {p["code"] for p in picked if p["variant"] == "momentum"}    # 沒設 universe 的版本不受主題限制


def test_theme_variant_skipped_when_themes_not_available(root):
    picked, _ = picks.select(pd.DataFrame([_row("1001")]), INDS, score.load_rules(root), "2026-10-23", None)
    assert not [p for p in picked if p["variant"] == "theme_top3"]


def _t(theme, strong, d="2026-10-23"):
    return {"data_date": d, "theme": theme, "strong": strong, "strong_members": 2 if strong else 0, "n": 3,
            "top": [{"code": "1001"}]}


def test_event_only_when_false_to_true():
    today = [_t("散熱", True), _t("ABF", True), _t("CCL", True), _t("新主題", True)]
    prev = [_t("散熱", False, "2026-10-22"), _t("ABF", True, "2026-10-22"), _t("CCL", None, "2026-10-22")]
    ev = themes.events(today, prev, "2026-10-23")
    assert [e["theme"] for e in ev] == ["散熱"]     # 已經強、前一天沒資料、前一天沒紀錄，都不算


def test_events_from_ledger_and_history_prefers_real_over_replay():
    replay = [_t("散熱", False, "2026-10-20"), _t("散熱", True, "2026-10-21"), _t("散熱", True, "2026-10-22")]
    real = [_t("散熱", False, "2026-10-22")]
    ev = themes.events_from_ledger(replay)
    assert [(e["data_date"], e["theme"]) for e in ev] == [("2026-10-21", "散熱")]
    h = themes.history(real, replay, [], ev)
    assert h["dates"] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert h["source"] == ["replay", "replay", "real"]
    assert h["themes"]["散熱"]["strong"] == [False, True, False]      # 10/22 用真實紀錄
    assert h["events"][0]["source"] == "replay"



def _swing_rows():
    return [{"theme": "散熱", "n": 3, "strong_members": 2, "strong": True,
             "top": [{"code": "1001"}, {"code": "1002"}, {"code": "1003"}]}]


DAYS = [f"2026-10-{d:02d}" for d in range(10, 20)]


def _hist(spec):
    """spec：{代號: 每天的訊號數 list（對齊 DAYS）}"""
    return {(d, c): v for c, vals in spec.items() for d, v in zip(DAYS, vals) if v is not None}


def test_swing_needs_both_trend_and_stable_signals(root):
    rules = score.load_rules(root)
    df = pd.DataFrame([_row("1001", r20=20.0), _row("1002", r20=15.0), _row("1003", r20=1.0),
                       _row("9001", r20=0.0), _row("9002", r20=0.0)])
    # 1001、1002 十天裡有 6 天 ≥3 個訊號（穩定）；1003 只有 2 天
    stable, weak = [3, 3, 3, 3, 3, 3, 1, 1, 1, 1], [3, 3, 1, 1, 1, 1, 1, 1, 1, 1]
    rows = _swing_rows()
    top = themes.swing(rows, df, _hist({"1001": stable, "1002": stable, "1003": weak}), DAYS, rules)
    r = rows[0]
    assert (r["swing_members"], r["swing_need"], r["swing_strong"]) == (2, 2, True)
    assert r["rel20"] > 0 and set(top) == {"1001", "1002", "1003"}

    rows = _swing_rows()   # 股價趨勢輸大盤 → 訊號再穩也不算強
    df2 = df.assign(r20=[1.0, 1.0, 1.0, 20.0, 20.0])
    themes.swing(rows, df2, _hist({"1001": stable, "1002": stable, "1003": stable}), DAYS, rules)
    assert rows[0]["swing_strong"] is False


def test_swing_is_none_when_history_too_short(root):
    rules = score.load_rules(root)
    df = pd.DataFrame([_row("1001"), _row("1002"), _row("1003")])
    rows = _swing_rows()
    short = [3, 3, 3, None, None, None, None, None, None, None]      # 只有 3 天資料 < 5
    themes.swing(rows, df, _hist({"1001": short, "1002": short, "1003": short}), DAYS, rules)
    assert rows[0]["swing_strong"] is None


def test_swing_events_use_swing_field_and_kind():
    today = [{**_t("散熱", False), "swing_strong": True}]
    prev = [{**_t("散熱", False, "2026-10-22"), "swing_strong": False}]
    assert themes.events(today, prev, "2026-10-23") == []                       # 單日版沒有轉強
    ev = themes.events(today, prev, "2026-10-23", "swing_strong", "swing")
    assert len(ev) == 1 and ev[0]["kind"] == "swing"


def test_swing_variant_uses_swing_universe(root):
    rules = score.load_rules(root)
    df = pd.DataFrame([_row("1001"), _row("1002")])
    picked, _ = picks.select(df, INDS, rules, "2026-10-23", {"strong_theme_top": {}, "strong_theme_swing_top": {"1002": ["散熱"]}})
    s = [p for p in picked if p["variant"] == "theme_swing_top3"]
    assert [p["code"] for p in s] == ["1002"] and s[0]["pick_id"].startswith("2026-W43-S")
