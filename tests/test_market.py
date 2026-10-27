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


def _days(end_iso, n):
    """以 end_iso 為最後一天往前 n 個日曆天的日期字串。"""
    from datetime import date, timedelta
    e = date.fromisoformat(end_iso)
    return [(e - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def test_mode_asof_never_uses_data_from_the_day_itself_or_later():
    """台股 d 日 17:30 掃描時，美股 d 日還沒開盤。只能用日期 < d 的美股資料，補跑、重跑結果才一致。"""
    ds = _days("2026-09-28", 40)
    calm = {"vix": [[d, 15.0] for d in ds], "sox": [[d, 100.0 + i] for i, d in enumerate(ds)],
            "ixic": [[d, 100.0 + i] for i, d in enumerate(ds)]}
    mode, info, asof = market.mode_asof(calm, "2026-09-20")
    assert asof == "2026-09-19" and mode == "riskon"
    crash = {k: v[:] for k, v in calm.items()}
    crash["vix"] = crash["vix"][:-1] + [["2026-09-28", 45.0]]            # 之後才發生的恐慌
    assert market.mode_asof(crash, "2026-09-20")[0] == "riskon"           # 看不到未來
    assert market.mode_asof(crash, "2026-09-29")[0] == "panic"


def test_record_mode_is_append_only_first_record_wins_and_flags_stale(root):
    import shutil
    from scanner import ledger
    dates = write_history(root, n_days=30)
    ds = _days(dates[-1], 40)                                             # 美股資料到台股日期的前一天
    s = {"vix": [[d, 15.0] for d in ds[:-1]], "sox": [[d, 100.0 + i] for i, d in enumerate(ds[:-1])],
         "ixic": [[d, 100.0 + i] for i, d in enumerate(ds[:-1])]}
    rec = market.record_mode(root, s, "v1")
    assert rec["tw_data_date"] == dates[-1] and rec["mode"] == "riskon" and rec["stale"] is False
    s["vix"] = [[d, 40.0] for d, _ in s["vix"]]                            # 重跑時資料變了，也不改已記錄的
    market.record_mode(root, s, "v1")
    rows = ledger.read(root, "market_modes")
    assert len(rows) == 1 and rows[0]["mode"] == "riskon"
    shutil.rmtree(os.path.join(root, "ledger"))
    old = _days("2025-06-30", 40)                                          # 最近一筆是半年前
    stale = market.record_mode(root, {k: [[d, 15.0 + i] for i, d in enumerate(old)] for k in s}, "v1")
    assert stale["stale"] is True                                           # 太舊的美股資料標 stale，分析時不採用
