import pandas as pd

from scanner import breadth


def test_new_highs_counts_only_full_window(monkeypatch):
    monkeypatch.setattr(breadth, "HIGH_WIN", 5)
    monkeypatch.setattr(breadth, "SERIES_DAYS", 3)
    idx = [f"2026-01-{d:02d}" for d in range(1, 9)]
    close = pd.DataFrame({"1111": [1, 2, 3, 4, 5, 6, 7, 8],      # 每天創新高
                          "2222": [8, 7, 6, 5, 4, 3, 2, 1]}, index=idx)  # 一路跌
    out = breadth.new_highs(close)
    assert [r[0] for r in out] == idx[-3:]
    assert all(r[1] == 1 and r[2] == 2 for r in out)


def test_resilient_ranks_relative_to_index_and_filters_liquidity(monkeypatch):
    monkeypatch.setattr(breadth, "RES_WIN", 2)
    idx = ["d1", "d2", "d3"]
    close = pd.DataFrame({"1111": [100, 100, 95], "2222": [100, 100, 80], "3333": [100, 100, 120]}, index=idx)
    turnover = pd.DataFrame({"1111": [5e8] * 3, "2222": [5e8] * 3, "3333": [1e7] * 3}, index=idx)
    bench = pd.Series([100, 100, 90], index=idx)
    meta, rows = breadth.resilient(close, turnover, {}, {}, bench, {"1111": 12.0})
    assert meta["bench_r20"] == -10.0
    assert [r["code"] for r in rows] == ["1111", "2222"]      # 3333 成交金額太小被排除
    assert rows[0]["rel20"] == 5.0 and rows[0]["rev_yoy"] == 12.0


def test_theme_revenue_detects_turn_up():
    yms = [f"2025-{m:02d}" for m in range(1, 13)] + [f"2026-{m:02d}" for m in range(1, 13)]
    # 先高、一路下滑、最後幾個月急拉 → 3 月平均重新站上 12 月平均
    vals = [100] * 12 + [80, 70, 60, 55, 50, 50, 50, 50, 50, 50, 50, 140]
    frames = {ym: pd.DataFrame({"revenue": [v * 1e5], "yoy": [0.0]}, index=["1111"]) for ym, v in zip(yms, vals)}
    cfg = {"themes": {"測試": {"stocks": [{"code": "1111", "name": "x"}]}}}
    (t,) = breadth.theme_revenue(frames, cfg)
    assert t["state"] == "彎底"
    assert t["series"][-1][1] == 140.0


def test_theme_pe_compares_to_median():
    pe = {"1111": {"pe": 10.0, "pb": 1.0}, "2222": {"pe": 20.0, "pb": 2.0}, "3333": {"pe": 30.0, "pb": 3.0}}
    cfg = {"themes": {"t": {"stocks": [{"code": c} for c in ("1111", "2222", "3333", "4444")]}}}
    (t,) = breadth.theme_pe(pe, cfg, {})
    assert t["median_pe"] == 20.0
    assert [m["vs_med"] for m in t["members"]] == [-50, 0, 50, None]


def test_faded_finds_intraday_reversal():
    df = pd.DataFrame([
        {"date": "d1", "code": "1111", "close": 100, "high": 100, "turnover": 5e8, "industry": ""},
        {"date": "d2", "code": "1111", "close": 102, "high": 109, "turnover": 5e8, "industry": ""},   # +9% 攻過、收盤掉 6.4%
        {"date": "d1", "code": "2222", "close": 100, "high": 100, "turnover": 5e8, "industry": ""},
        {"date": "d2", "code": "2222", "close": 108, "high": 109, "turnover": 5e8, "industry": ""},   # 收在高點附近
    ])
    rows = breadth.faded(df, "d2", {})
    assert [r["code"] for r in rows] == ["1111"]
    assert rows[0]["high_pct"] == 9.0 and rows[0]["close_pct"] == 2.0
