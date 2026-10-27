from scanner import accuracy, ledger, grade, score
from tests.conftest import write_history


def test_stats_median_win_and_ignores_missing():
    s = accuracy.stats([1.0, -2.0, 3.0, None, float("nan")])
    assert s == {"n": 3, "median": 1.0, "win": round(2 / 3, 3)}
    assert accuracy.stats([]) == {"n": 0, "median": None, "win": None}


def test_summarize_groups_by_swing_and_variant_top3_only():
    rows = [
        {"ref_type": "theme", "horizon": 20, "excess": 5.0, "swing_strong": True},
        {"ref_type": "theme", "horizon": 20, "excess": -1.0, "swing_strong": False},
        {"ref_type": "theme", "horizon": 20, "excess": 9.0, "swing_strong": None},      # 資料不足不算
        {"ref_type": "pick", "horizon": 20, "excess": 2.0, "variant": "base", "rank": 1},
        {"ref_type": "pick", "horizon": 20, "excess": 8.0, "variant": "base", "rank": 7},  # 第 7 名不算進前 3
        {"ref_type": "theme_event", "horizon": 20, "excess": 4.0, "kind": "swing"},
        {"ref_type": "theme_event", "horizon": 20, "excess": 9.0, "kind": "daily"},       # 單日版不算
    ]
    s = accuracy.summarize(rows, [20])
    assert s["themes"]["強"]["20"]["median"] == 5.0 and s["themes"]["不強"]["20"]["n"] == 1
    assert s["picks"]["base"]["20"] == {"n": 1, "median": 2.0, "win": 1.0}
    assert s["theme_events"]["20"]["n"] == 1


def test_picks_to_date_reports_return_since_pick(root):
    dates = write_history(root, n_days=40, heroes={str(1000 + i) for i in range(4)})
    rules = score.load_rules(root)
    rules["grade"]["min_bench_stocks"] = 5
    ledger.append(root, "picks", [{"pick_id": "W-01", "week": "W", "code": "1000", "name": "股1000",
                                   "data_date": dates[30], "variant": "base", "sent": True, "rank": 1,
                                   "price_at_pick": 1.0}], key=("week", "code", "variant"))
    grade.run(root, rules, dates[-1])
    rows = accuracy.picks_to_date(root, rules, dates[-1])
    r = rows[0]
    assert r["days_held"] == 9 and r["ret_to_date"] > 0 and r["excess_to_date"] is not None
    assert r["outcomes"]["5"] is not None and r["outcomes"]["20"] is None    # 5 日已到期、20 日還沒


def test_by_mode_joins_outcomes_to_the_recorded_mode_of_the_judgement_day(root):
    ledger.append(root, "market_modes", [
        {"tw_data_date": "2026-01-05", "mode": "riskon", "stale": False},
        {"tw_data_date": "2026-01-06", "mode": "riskoff", "stale": False},
        {"tw_data_date": "2026-01-07", "mode": "panic", "stale": True}], key=("tw_data_date",))
    mk = lambda d, ex, strong: {"ref_type": "theme", "horizon": 20, "start_date": d, "excess": ex, "swing_strong": strong}
    outs = [mk("2026-01-05", 5.0, True), mk("2026-01-05", 1.0, False), mk("2026-01-06", -2.0, True),
            mk("2026-01-07", 9.0, True), mk("2026-01-08", 9.0, True)]       # 後兩筆：stale 燈號、沒有燈號紀錄
    res, days = accuracy.by_mode(root, outs, [20])
    assert set(res) == {"riskon", "riskoff"} and days == {"riskon": 1, "riskoff": 1}
    assert res["riskon"]["themes"]["強"]["20"]["median"] == 5.0 and res["riskoff"]["themes"]["強"]["20"]["median"] == -2.0
