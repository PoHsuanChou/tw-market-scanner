import numpy as np
import pandas as pd

from scanner import replay_us_sectors as R
from tests.conftest import make_dates

CFG = {"signal": {"window": 20, "strong_pp": 5, "rate_up": 0.25}, "universe": ["UP", "DN", "DN2", "MILD"],
       "pairs": [{"name": "漲", "us": "UP"}, {"name": "抗跌", "us": "MILD"}, {"name": "利率", "us": "^TNX", "kind": "rate"}]}


def _ser(dates, f):
    return pd.Series([f(i) for i in range(len(dates))], index=dates, dtype=float)


def test_us_states_labels_and_uses_only_earlier_closes():
    d = make_dates(60)
    us = {"UP": _ser(d, lambda i: 100 * 1.01 ** i), "DN": _ser(d, lambda i: 100 * 0.98 ** i),
          "DN2": _ser(d, lambda i: 100 * 0.98 ** i), "MILD": _ser(d, lambda i: 100 * 0.999 ** i), "^TNX": _ser(d, lambda i: 4 + 0.02 * i)}
    st = R.us_states(us, CFG, d)
    assert st[("UP", "etf")].iloc[-1] == "在漲"
    assert st[("MILD", "etf")].iloc[-1] == "抗跌"            # 本身在跌，但比中位數（DN 和 MILD 之間）強 5 以上
    assert st[("^TNX", "rate")].iloc[-1] == "在漲"           # 20 日上升 0.4 個百分點
    assert st[("UP", "etf")].iloc[20] is None                # 台股 d[20] 只看得到 d[19]，還不夠 20 日
    assert st[("UP", "etf")].iloc[21] == "在漲"


def test_us_state_on_tw_date_ignores_same_day_us_close():
    d = make_dates(40)
    up = _ser(d, lambda i: 100.0)
    up.iloc[-1] = 200.0                                       # 台股最後一天當天美股暴漲，台股那天看不到
    us = {"UP": up, "DN": _ser(d, lambda i: 100), "DN2": _ser(d, lambda i: 100), "MILD": _ser(d, lambda i: 100)}
    assert R.us_states(us, CFG, d)[("UP", "etf")].iloc[-1] == "平常"


def test_events_do_not_overlap_and_blocks_step_by_h():
    d = make_dates(30)
    st = pd.Series(["平常", "在漲", "平常", "在漲", "在漲", "平常", "平常", "在漲"] + ["平常"] * 22, index=d)
    exc = pd.Series(np.arange(30, dtype=float), index=d)
    ev = R.events(st, exc, 5, "在漲")
    assert [x[0] for x in ev] == [d[1], d[7]]                 # d[3] 還在 d[1] 的 5 天持有期內
    b = R.blocks(st, exc, 10, 0)
    assert [x[0] for x in b] == [d[0], d[10]]                 # 之後不足 10 天的 d[20] 不算


def test_basket_index_is_equal_weight_and_skips_bad_data():
    d = make_dates(4)
    close = pd.DataFrame({"A": [10, 11, 11, 11], "B": [10, 10, 10, 50], "C": [10, 10, 12, 12]}, index=d, dtype=float)
    ok = close.notna()
    ok.loc[:, "C"] = [False, False, True, True]                # C 從第 3 天才進籃子，它第 3 天的漲幅不算
    idx = R.basket_index(close, ok)
    assert round(idx.iloc[1], 4) == 1.05 and round(idx.iloc[2], 4) == 1.05
    assert round(idx.iloc[3], 4) == 1.05                       # B 一天 +400% 視為資料錯誤


def test_pooled_compares_within_same_date():
    res = [{"_blocks20": [("2024-01-02", "在漲", 5.0), ("2024-02-01", "在漲", 3.0)]},
           {"_blocks20": [("2024-01-02", "平常", 1.0), ("2024-02-01", "平常", 2.0), ("2024-03-01", "在漲", 9.0)]}]
    p = R.pooled_test(res)
    assert p["在漲"]["blocks"] == 2 and p["在漲"]["mean_diff"] == 2.5 and p["抗跌"]["blocks"] == 0
