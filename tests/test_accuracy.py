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
