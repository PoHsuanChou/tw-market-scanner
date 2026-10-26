import numpy as np
import pandas as pd

from scanner import adjust


def _close(cols):
    idx = [f"2026-01-{d:02d}" for d in range(5, 5 + len(next(iter(cols.values()))))]
    return pd.DataFrame(cols, index=idx)


def _ev(*rows):
    return pd.DataFrame([{"code": c, "ex_date": d, "factor": f, "kind": k} for c, d, f, k in rows])


def test_iso_handles_both_roc_formats():
    assert adjust._iso("114年08月01日") == "2025-08-01"
    assert adjust._iso("114/02/12") == "2025-02-12"
    assert adjust._iso("") is None


def test_event_factor_and_sanity_range():
    e = adjust._event("2402 ", "2025-08-01", "41.10", "39.12", "div")
    assert e["code"] == "2402" and abs(e["factor"] - 39.12 / 41.10) < 1e-6
    assert adjust._event("1111", "2025-08-01", "10", "100", "div") is None       # 因子 10 倍：資料錯誤，丟掉
    assert adjust._event("1111", "2025-08-01", "--", "10", "div") is None


def test_dividend_adjustment_removes_the_fake_drop_and_stacks_multiple_events():
    c = _close({"A": [100.0, 100.0, 95.0, 95.0, 90.0]})          # 第 3 天除息 5%，第 5 天又除息約 5.3%
    ev = _ev(("A", "2026-01-07", 0.95, "div"), ("A", "2026-01-09", 90 / 95, "div"))
    a = adjust.apply(c, ev)
    assert np.allclose(a["A"].values, [90.0, 90.0, 90.0, 90.0, 90.0])   # 還原後一條直線＝沒有假虧損
    assert c["A"].iloc[0] == 100.0                                       # 不動原表


def test_reduction_and_split_make_prices_comparable():
    c = _close({"B": [10.0, 10.0, 20.0, 21.0], "C": [400.0, 400.0, 100.0, 101.0]})   # B 減資 1 變 2；C 1 拆 4
    ev = _ev(("B", "2026-01-07", 2.0, "reduction"), ("C", "2026-01-07", 0.25, "jump"))
    a = adjust.apply(c, ev)
    assert np.allclose(a["B"].values, [20, 20, 20, 21]) and np.allclose(a["C"].values, [100, 100, 100, 101])


def test_detect_jumps_flags_corporate_actions_but_not_real_moves():
    c = _close({
        "REAL": [100, 109, 100, 108, 100.0],                   # 單日 ±9%：真實漲跌，不是公司行動
        "SPLIT": [400, 400, 100, 100, 101.0],                  # 相鄰兩天 -75%：1 拆 4
        "GAPOK": [100, np.nan, np.nan, 125, 126.0],            # 停牌復牌 +25%：可能是真實，不當成還原
        "GAPSPLIT": [1000, np.nan, 100, 100, 100.0],           # 停牌後 -90%：極端，視為分割
        "NEW": [np.nan, np.nan, 20, 40, 41.0],                 # 新上市初期沒有漲跌幅限制
        "EXDIV": [100, 100, 96, 105.5, 106.0],                 # 除息日當天漲 10%（以參考價算），不能誤判
    })
    known = _ev(("EXDIV", "2026-01-07", 0.9, "div"))
    got = {(j["code"], j["ex_date"]) for j in adjust.detect_jumps(c, known)}
    assert got == {("SPLIT", "2026-01-07"), ("GAPSPLIT", "2026-01-07")}


def test_save_dedupes_and_is_idempotent(root):
    e = [{"code": "2402", "ex_date": "2025-08-01", "factor": 0.95, "kind": "div"}]
    assert adjust.save(root, e) == 1
    assert adjust.save(root, e + [{"code": "2402", "ex_date": "2025-08-01", "factor": 0.5, "kind": "div"}]) == 0
    assert list(adjust.load(root)["factor"]) == [0.95]                   # 舊的不被改
