import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "research", "phase2"))
import common as C  # noqa: E402
from tests.conftest import make_dates  # noqa: E402


def test_t_distribution_p_matches_tables():
    assert abs(C.p_one_sided(2.0, 10) - 0.03669) < 1e-4
    assert abs(C.p_one_sided(2.5, 30) - 0.00906) < 1e-4
    assert abs(C.p_one_sided(1.0, 5) - 0.18161) < 1e-4
    assert abs(C.p_one_sided(-1.0, 5) - 0.81839) < 1e-4
    assert abs(C.p_one_sided(1.96, 1e7) - 0.025) < 1e-3


def test_bh_and_by():
    p = [0.01, 0.04, 0.03, 0.2, np.nan]
    q = C.bh(p)
    assert np.allclose(q[:4], [0.04, 0.0533333, 0.0533333, 0.2]) and np.isnan(q[4])
    assert all(C.bh(p, dependent=True)[:4] >= q[:4])


def test_foreign_daily_maps_everything_to_next_tw_day_after_fix():
    tw = make_dates(10)
    step = [[x, 110.0 if i >= 4 else 100.0] for i, x in enumerate(tw)]
    fixed = C.foreign_daily({"6981.T": step, "US": step}, tw)
    orig = C.foreign_daily({"6981.T": step, "US": step}, tw, asia_same_day=True)
    assert fixed["6981.T"].iloc[5] > 0.09 and fixed["6981.T"].iloc[4] == 0     # 修正後：亞股 d 日的漲跌算在 d+1
    assert orig["6981.T"].iloc[4] > 0.09                                       # 原本：同一天
    assert fixed["US"].iloc[5] > 0.09 and orig["US"].iloc[5] > 0.09


def test_h1_panel_uses_only_past_for_features_and_future_for_label():
    d = make_dates(60)
    rng = np.random.default_rng(0)
    tw = pd.DataFrame(rng.normal(0, 0.01, (60, 6)), index=d, columns=list("abcdef"))
    fx = pd.DataFrame(rng.normal(0, 0.01, (60, 6)), index=d, columns=list("ABCDEF"))
    members = {f"T{i}": [c] for i, c in enumerate("abcdef")}
    peers = {f"T{i}": [c] for i, c in enumerate("ABCDEF")}
    bounds = [9, 19, 29, 39]
    p = C.h1_panel(tw, fx, members, peers, bounds, min_themes=1)
    row = p[(p.date == d[19]) & (p.theme == "T0")].iloc[0]
    assert np.isclose(row.fp, fx["A"].iloc[10:20].sum()) and np.isclose(row.tp, tw["a"].iloc[10:20].sum())
    assert np.isclose(row.tn, tw["a"].iloc[20:30].sum())
    p1 = C.h1_panel(tw, fx, members, peers, bounds, skip=1, min_themes=1)
    row1 = p1[(p1.date == d[19]) & (p1.theme == "T0")].iloc[0]
    assert np.isclose(row1.tn, tw["a"].iloc[21:31].sum())                      # d+1 進場
    p2 = C.h1_panel(tw, fx, members, peers, bounds, lag=2, min_themes=1)
    row2 = p2[(p2.date == d[29]) & (p2.theme == "T0")].iloc[0]
    assert np.isclose(row2.fp, fx["A"].iloc[10:20].sum())                      # 再往前一段


def test_bounds_weekly_offsets_and_holiday_weeks():
    d = [x for x in make_dates(80) if not ("2026-02-16" <= x <= "2026-02-20")]   # 整週休市
    b0, b1 = C.bounds_weekly(d), C.bounds_weekly(d, offset=1)
    assert b0[0] == 19 and pd.Timestamp(d[b0[0]]).day_of_week == 4               # 第 4 週的週五
    assert b1[0] > b0[0] and all(y > x for x, y in zip(b0, b0[1:]))
    assert len(C.bounds_month(make_dates(70))) == 4


def test_nw_and_boot_detect_real_mean_and_not_noise():
    rng = np.random.default_rng(1)
    sig = rng.normal(0.5, 1, 60)
    noise = rng.normal(0, 1, 60)
    assert C.nw(sig)["p"] < 0.01 and C.nw(noise)["p"] > 0.05
    assert C.boot_p_mean(sig, reps=2000) < 0.02 and C.boot_p_mean(noise, reps=2000) > 0.05


def test_reality_check_penalises_picking_the_best_of_many():
    rng = np.random.default_rng(2)
    X = rng.normal(0, 1, (40, 30))                                              # 30 個都沒用的策略
    best_t = (X.mean(0) / (X.std(0, ddof=1) / np.sqrt(40))).max()
    rc = C.reality_check(lambda i: X[i].mean(0), 40, reps=500)
    assert C.p_one_sided(best_t, 39) < 0.1 and rc["p"] > 0.1                   # 單看最好的那個很漂亮，校正後不是
    Y = X.copy()
    Y[:, 0] += 1.0
    assert C.reality_check(lambda i: Y[i].mean(0), 40, reps=500)["p"] < 0.05


def test_clark_west_favours_model_with_real_signal():
    rng = np.random.default_rng(3)
    x = rng.normal(0, 1, 500)
    y = 0.5 * x + rng.normal(0, 1, 500)
    f = C.clark_west(y, np.zeros(500), 0.5 * x)
    assert C.nw(f)["p"] < 0.01


def test_h2_diff_and_regimes():
    rec = pd.DataFrame([("d1", "A", "國外在動", 5.0, 1.0), ("d1", "B", "平常", 1.0, 1.0), ("d1", "C", "國外抗跌", 9.0, 1.0),
                        ("d2", "A", "平常", 2.0, 0.0)], columns=["date", "theme", "label", "exc", "mkt"])
    s = C.h2_diff(rec)
    assert list(s.index) == ["d1"] and s["d1"] == 4.0
    assert C.h2_diff(rec, exclude=("A",)).empty
    tw = pd.Series(np.r_[np.linspace(100, 200, 200), np.linspace(200, 100, 100)], index=make_dates(300))
    r = C.regimes(tw)
    assert r.iloc[0] == "NA" and r.iloc[199] == "Bull" and r.iloc[-1] == "Bear"
