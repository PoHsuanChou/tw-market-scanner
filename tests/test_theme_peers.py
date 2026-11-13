import json
import os

import numpy as np
import pandas as pd
import yaml

from scanner import theme_peers, ledger
from tests.conftest import make_dates, write_history

CFG = {"window": 20, "strong_pp": 5, "max_age_days": 7, "min_peers": 2}
LL = {"max_lag_weeks": 2, "min_weeks": 52, "min_t": 2.5}


def _ser(dates, start, step):
    return [[d, start * (1 + step) ** i] for i, d in enumerate(dates)]


def _peers(root, peers):
    with open(os.path.join(root, "config", "theme_peers.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({"peers": peers}, f, allow_unicode=True)


def test_foreign_strength_is_relative_to_all_peers_and_flags_lead():
    d = make_dates(30)
    closes = {"UP1": _ser(d, 100, 0.01), "UP2": _ser(d, 50, 0.012), "FLAT": _ser(d, 10, 0.0), "DN": _ser(d, 10, -0.005)}
    peers = {"熱": ["UP1", "UP2"], "冷": ["FLAT", "DN"], "沒資料": ["XXX"]}
    tw = make_dates(31)[-1]
    rows, asof = theme_peers.compute(closes, peers, tw, {"熱": -1.0, "冷": 3.0}, CFG)
    by = {r["theme"]: r for r in rows}
    assert by["熱"]["foreign"] > 5 and by["熱"]["strong"] and by["熱"]["lead"]      # 國外在動、台灣 rel20 ≤ 0
    assert by["熱"]["label"] == "國外在動" and by["熱"]["abs"] > 0 and not by["熱"]["few_peers"]
    assert by["冷"]["foreign"] < 0 and by["冷"]["strong"] is False and by["冷"]["lead"] is False
    assert by["沒資料"]["foreign"] is None and by["沒資料"]["lead"] is None and by["沒資料"]["missing"] == ["XXX"]
    assert asof == d[-1]


def test_only_uses_foreign_closes_before_tw_date():
    d = make_dates(30)
    closes = {"A": _ser(d, 100, 0.0), "B": _ser(d, 100, 0.0)}
    closes["A"][-1][1] = 200.0                                  # 台股當天（d[-1]）國外暴漲，不應該看到
    rows, asof = theme_peers.compute(closes, {"T": ["A"], "U": ["B"]}, d[-1], {}, CFG)
    assert asof == d[-2] and rows[0]["foreign"] == 0


def test_stale_series_is_not_used():
    d = make_dates(30)
    rows, _ = theme_peers.compute({"A": _ser(d, 100, 0.01)}, {"T": ["A"]}, "2099-01-01", {}, CFG)
    assert rows[0]["foreign"] is None                            # 最後一筆離台股日期太遠


def test_build_writes_out_and_ledger_and_keeps_previous_when_yahoo_fails(root):
    dates = write_history(root, n_days=30)
    _peers(root, {"熱": ["UP"], "冷": ["DN"]})
    fd = make_dates(80, start="2025-12-01")
    fd = [x for x in fd if x < dates[-1]]
    good = lambda tickers, **kw: ({"UP": _ser(fd, 10, 0.01), "DN": _ser(fd, 10, -0.01)}, [])
    out = theme_peers.build(root, fetch=good)
    assert out["tw_data_date"] == dates[-1] and not out["stale"]
    assert out["themes"][0]["theme"] == "熱"                       # 國外強度高的排前面
    rec = ledger.read(root, "theme_peers")
    assert {r["theme"] for r in rec} == {"熱", "冷"} and rec[0]["rules_version"]
    assert theme_peers.build(root, fetch=good) and len(ledger.read(root, "theme_peers")) == 2   # 重跑不重複

    out2 = theme_peers.build(root, fetch=lambda tickers, **kw: ({}, list(tickers)))
    saved = json.load(open(os.path.join(root, "out", "theme_peers.json"), encoding="utf-8"))
    assert out2["stale"] and saved["stale"] and saved["themes"] == out["themes"]   # 沿用上一次並標示
    assert saved["failed"] == ["DN", "UP"]
    assert len(ledger.read(root, "theme_peers")) == 2              # 抓不到時不寫 ledger


def test_config_peers_cover_only_known_themes(root):
    peers = theme_peers.load(root)
    themes = yaml.safe_load(open(os.path.join(root, "config", "themes.yaml"), encoding="utf-8"))["themes"]
    assert peers and set(peers) <= set(themes)
    assert all(isinstance(t, str) for v in peers.values() for t in v)   # 000660.KS 之類不會被讀成數字


def test_falling_less_is_labelled_resilient_not_moving():
    d = make_dates(30)
    closes = {"A": _ser(d, 100, -0.001), "B": _ser(d, 100, -0.0012), "C": _ser(d, 100, -0.02), "D": _ser(d, 100, -0.025)}
    rows, _ = theme_peers.compute(closes, {"抗跌": ["A", "B"], "重挫": ["C", "D"], "單檔": ["A"]},
                                  make_dates(31)[-1], {"抗跌": -3.0}, CFG)
    by = {r["theme"]: r for r in rows}
    assert by["抗跌"]["strong"] and by["抗跌"]["abs"] < 0                # 比其他對照股強，但本身在跌
    assert by["抗跌"]["label"] == "國外抗跌" and by["抗跌"]["lead"] is False
    assert by["重挫"]["label"] is None
    assert by["單檔"]["few_peers"] and not by["抗跌"]["few_peers"]


def test_foreign_weekly_maps_us_to_next_tw_day_and_asia_to_same_day():
    tw = make_dates(12)                                          # 週一 2026-01-05 開始
    flat = [[x, 100.0] for x in tw]
    step = [[x, 110.0 if i >= 4 else 100.0] for i, x in enumerate(tw)]   # 週五（tw[4]）漲 10% 之後持平
    fw = theme_peers.foreign_weekly({"US": step, "6981.T": step, "Z": flat}, {"美": ["US"], "日": ["6981.T"]}, tw)
    w1, w2 = fw["美"].index[:2]
    assert fw["日"][w1] > 0.05 and fw["日"][w2] == 0                # 日股同一天反映，算在第一週
    assert fw["美"][w1] == 0 and fw["美"][w2] > 0.05                # 美股週五收盤，台股下週一才看得到


def _weeks(n):
    return pd.period_range("2023-01-06", periods=n, freq="W-FRI")


def test_leadlag_finds_foreign_lead_and_ignores_noise():
    rng = np.random.default_rng(1)
    w = _weeks(150)
    f = pd.Series(rng.normal(0, 1, 150), index=w)
    t = f.shift(1) * 0.8 + pd.Series(rng.normal(0, 0.6, 150), index=w)   # 台灣晚國外一週
    r = theme_peers.leadlag(f, t, LL)
    assert r["verdict"] == "國外先動" and r["lag_weeks"] == 1 and r["corr"]["1"] > 0.6
    r2 = theme_peers.leadlag(t, f, LL)                                     # 反過來就是台灣先動
    assert r2["verdict"] == "台灣先動" and r2["lag_weeks"] == 1
    noise = pd.Series(rng.normal(0, 1, 150), index=w)
    assert theme_peers.leadlag(f, noise, LL)["verdict"] == "看不出關係"
    assert theme_peers.leadlag(f, f * 2, LL)["verdict"] == "同步"
    short = theme_peers.leadlag(f[:30], t[:30], LL)
    assert short["verdict"] is None and short["weeks"] < 52


def test_leadlag_counts_holiday_weeks_as_real_weeks():
    rng = np.random.default_rng(2)
    w = _weeks(120)
    f = pd.Series(rng.normal(0, 1, 120), index=w)
    t = f.shift(1)
    keep = [x for i, x in enumerate(w) if i != 60]                         # 中間有一整週休市
    r = theme_peers.leadlag(f[keep], t[keep], LL)
    assert r["verdict"] == "國外先動" and r["lag_weeks"] == 1 and r["corr"]["1"] > 0.99
