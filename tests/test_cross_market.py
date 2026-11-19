import pandas as pd

from scanner import cross_market as cm


def _s(vals, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(vals)).strftime("%Y-%m-%d")
    return pd.Series(vals, index=idx, dtype=float)


def test_metrics_new_high_and_volume():
    close = _s([100] * 59 + [110])            # 最後一天創 60 日新高
    vol = _s([1000] * 55 + [3000] * 5)         # 最近 5 天量放大
    bench = _s([50] * 60)
    m = cm.metrics(close, vol, bench)
    assert m["pos60"] == 100 and m["off_high"] == 0 and m["since_high"] == 0
    assert m["r20"] == 10.0 and m["rel20"] == 10.0
    assert m["range60"] == 10.0
    assert m["vol_ratio"] == round(3000 / ((55 * 1000 + 5 * 3000) / 60), 2)
    assert len(m["spark"]) == 60


def test_metrics_not_enough_data():
    assert cm.metrics(_s([1] * 10), None, None) is None


def test_metrics_missing_bench_and_volume():
    m = cm.metrics(_s(list(range(1, 41))), None, None)
    assert m["rel20"] is None and m["vol_ratio"] is None and m["pos60"] == 100


def test_suffix_and_market():
    assert cm.suffix("6981.T") == ".T" and cm.MARKET[cm.suffix("6981.T")] == "日"
    assert cm.suffix("MU") == "" and cm.INDEX[cm.suffix("MU")] == "^GSPC"
    assert cm.suffix("BESI.AS") == ".AS"
