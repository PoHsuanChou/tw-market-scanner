import json
import os

from scanner import market
from tests.conftest import write_history


def _ser(start, step, n=30):
    return [[f"2026-09-{(i % 28) + 1:02d}", start + step * i] for i in range(n)]


def test_market_mode_rules_match_private_repo():
    calm = {"vix": _ser(15, 0), "sox": _ser(100, 1), "ixic": _ser(100, 1)}
    assert market.market_mode(calm)[0] == "riskon"
    mid = {"vix": _ser(20, 0), "sox": _ser(100, 1), "ixic": _ser(100, 1)}
    assert market.market_mode(mid)[0] == "neutral"                       # VIX 18–25：中性
    panic = {"vix": _ser(35, 0), "sox": _ser(100, 1), "ixic": _ser(100, 1)}
    assert market.market_mode(panic)[0] == "panic"                       # VIX ≥ 30
    crash = {"vix": _ser(20, 0), "sox": _ser(100, -1.5), "ixic": _ser(100, 1)}
    assert market.market_mode(crash)[0] == "panic"                       # SOX 20 日跌超過 15%
    off = {"vix": _ser(26, 0), "sox": _ser(100, 0), "ixic": _ser(100, -0.5)}
    assert market.market_mode(off)[0] == "riskoff"                       # VIX ≥ 25 且 NASDAQ 20 日跌超過 5%
    assert market.market_mode({"vix": _ser(15, 0)})[0] is None            # 資料不足不亂猜


def test_build_keeps_previous_series_when_yahoo_fails_and_marks_stale(root):
    write_history(root, n_days=30)
    prev = {"series": {"spx": [["2026-10-01", 5000.0]] * 8}, "mode": "neutral", "mode_inputs": {"vix": 20}}
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    json.dump(prev, open(os.path.join(root, "out", "market.json"), "w"))
    out = market.build(root, fetch=lambda: ({}, ["spx", "vix"]))
    assert out["series"]["spx"] == prev["series"]["spx"]                 # 沿用上一次
    assert "spx" in out["stale"] and out["failed"] == ["spx", "vix"]
    assert out["mode"] == "neutral" and "mode" in out["stale"]           # 資料不足時沿用上一次的燈號並標示


def test_build_writes_prices_for_every_stock_and_local_series(root):
    dates = write_history(root, n_days=30)
    out = market.build(root, fetch=lambda: ({}, list(market.YAHOO)))
    px = json.load(open(os.path.join(root, "out", "prices.json")))
    assert px["date"] == dates[-1] and "1000" in px["p"] and len(px["p"]) == 230
    assert out["series"].get("tsmc") is None                              # 合成資料沒有 2330，不會硬湊


def test_closed_only_drops_todays_intraday_point_until_us_close():
    from datetime import datetime, timezone
    ser = [["2026-10-23", 1.0], ["2026-10-26", 2.0]]
    during = datetime(2026, 10, 26, 13, 43, tzinfo=timezone.utc)        # 美東 09:43，盤中
    after = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)          # 美東 17:00，已收盤
    morning_tw = datetime(2026, 10, 27, 9, 30, tzinfo=timezone.utc)     # 台灣晚上 17:30，美股還沒開
    assert market.closed_only(ser, during) == [["2026-10-23", 1.0]]
    assert market.closed_only(ser, after) == ser
    assert market.closed_only(ser, morning_tw) == ser                  # 前一天（10/26）已收盤，保留
