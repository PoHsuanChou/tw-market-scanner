import pandas as pd

from scanner import picks, ledger, grade, score, run_daily
from tests.conftest import write_history, write_revenue


def _row(code, **kw):
    r = {"code": code, "name": f"股{code}", "market": "TWSE", "industry": "半導體業", "close": 100.0,
         "avg_turnover": 2e8, "flag": "", "A": True, "B": True, "C": True, "D": True, "c_basis": "tdcc",
         "d_basis": "prev_month_yoy", "r20": 10.0, "bench_r20": 2.0, "foreign_5d": 1e6, "trust_5d": 1e5,
         "margin_growth_5d": 0.0, "c_big400_now": 60.0, "c_big400_old": 58.0, "d_ym": "2026-09",
         "d_yoy": 40.0, "d_ref_yoy": 30.0}
    r.update(kw)
    return r


INDS = [{"industry": "半導體業", "score": 4, "delta_4w": None}]


def _variant(picked, name):
    return [p for p in picked if p["variant"] == name]


def test_at_most_three_sent_but_all_ten_recorded(root):
    rules = score.load_rules(root)
    df = pd.DataFrame([_row(str(2000 + i), r20=float(i)) for i in range(12)])
    picked, _ = picks.select(df, INDS, rules, "2026-10-23")
    base = _variant(picked, "base")
    assert len(base) == 10                         # record_max
    assert sum(p["sent"] for p in base) == 3       # weekly_max
    assert [p["rank"] for p in base if p["sent"]] == [1, 2, 3]
    assert base[0]["code"] == "2000"               # base：20 日報酬最低（還沒漲）排前面
    assert base[0]["pick_id"] == "2026-W43-01"


def test_momentum_variant_is_recorded_but_never_sent(root):
    rules = score.load_rules(root)
    df = pd.DataFrame([_row(str(2000 + i), r20=float(i * 5)) for i in range(8)])   # 最高 35%
    picked, _ = picks.select(df, INDS, rules, "2026-10-23")
    mom = _variant(picked, "momentum")
    assert mom and not any(p["sent"] for p in mom)
    assert mom[0]["code"] == "2007" and mom[0]["pick_id"] == "2026-W43-M01"   # 漲最多的排前面、沒被 25% 擋掉
    assert "2007" not in {p["code"] for p in _variant(picked, "base")}       # base 仍排除漲 ≥ 25%


def test_zero_picks_when_nothing_passes_and_control_group_kept(root):
    rules = score.load_rules(root)
    df = pd.DataFrame([_row("2001", D=False), _row("2002", A=False, B=False, C=None),
                       _row("2003", A=False, B=False, C=False, D=False)])
    picked, universe = picks.select(df, INDS, rules, "2026-10-23")
    assert picked == []
    got = {r["code"]: (r["failed_at"], r["signals"]) for r in universe}
    assert got == {"2001": ("P.signals", 3), "2002": ("P.signals", 1), "2003": ("P.signals", 0)}   # 0 訊號也記錄


def test_each_gate_records_where_it_failed(root):
    rules = score.load_rules(root)
    df = pd.DataFrame([
        _row("3001", r20=30.0),                                   # P.not_priced（只在 base）
        _row("3002", flag="disposal"),                             # P.no_trap
        _row("3003", margin_growth_5d=15.0, r20=5.0),              # P.no_trap（漲 + 融資暴增）
        _row("3004", industry="食品工業"),                          # 產業關卡已關閉 → 會通過
    ])
    picked, uni = picks.select(df, INDS, rules, "2026-10-23")
    assert {r["code"]: r["failed_at"] for r in uni} == {"3001": "P.not_priced", "3002": "P.no_trap",
                                                         "3003": "P.no_trap", "3004": None}   # 通過的也在對照組
    assert "3004" in {p["code"] for p in _variant(picked, "base")}
    assert "3001" in {p["code"] for p in _variant(picked, "momentum")}


def test_industry_gate_still_works_when_enabled(root):
    rules = score.load_rules(root)
    rules["picks"]["industry_gate"] = True
    _, uni = picks.select(pd.DataFrame([_row("3004", industry="食品工業")]), INDS, rules, "2026-10-23")
    assert uni[0]["failed_at"] == "P.industry"


def test_reason_text_states_unmet_categories_explicitly(root):
    rules = score.load_rules(root)
    df = pd.DataFrame([_row("2001", C=False)])
    picked, _ = picks.select(df, INDS, rules, "2026-10-23")
    assert "C 大戶未成立" in picked[0]["reason_text"] and "看錯的條件" in picked[0]["reason_text"]


def test_week_close_is_friday_only():
    assert picks.is_week_close("2026-10-23") and not picks.is_week_close("2026-10-22")
    assert picks.week_id("2026-10-23") == "2026-W43"


def test_end_to_end_grade_and_idempotent(root):
    dates = write_history(root, n_days=90, heroes={str(1000 + i) for i in range(8)})
    # 90 個交易日，從 2026-01-05 起，找一個週五當資料日期
    fri = [d for d in dates[25:30] if pd.Timestamp(d).weekday() == 4][0]
    write_revenue(root, "2025-12", [(str(1000 + i), 40.0, 20.0, "2026-01-01") for i in range(10)])
    # 把 history 截到 fri，模擬「當天」
    import os
    for d in dates:
        if d > fri:
            os.rename(os.path.join(root, "history", "daily", f"{d}.csv.gz"),
                      os.path.join(root, "_later_{}.gz".format(d)))
    latest = run_daily.run(root, do_fetch=False)
    p = ledger.read(root, "picks")
    pb = [x for x in p if x["variant"] == "base"]
    assert len(pb) == 8 and sum(x["sent"] for x in p) == 3          # 8 檔通過，只有 base 的 3 檔 sent
    assert pb[0]["week"] == picks.week_id(fri) and {x["code"] for x in pb} == {str(1000 + i) for i in range(8)}
    assert not ledger.read(root, "outcomes")        # 還沒到期
    again = ledger.append(root, "picks", [{"week": p[0]["week"], "code": "1000"}], key=("week", "code"))
    assert again == 0
    # 把後面的日子放回去，再跑一次 → 5、20、60 日各自到期
    for d in dates:
        if d > fri:
            os.rename(os.path.join(root, "_later_{}.gz".format(d)),
                      os.path.join(root, "history", "daily", f"{d}.csv.gz"))
    rules = score.load_rules(root)
    rules["grade"]["min_bench_stocks"] = 5          # 合成資料只有 10 檔電子股
    n = grade.run(root, rules, dates[-1])
    assert n > 0
    outs = [o for o in ledger.read(root, "outcomes") if o["ref_type"] == "pick" and o["ref_id"] == pb[0]["pick_id"]]
    assert sorted(o["horizon"] for o in outs) == [5, 20, 60]
    h5 = [o for o in outs if o["horizon"] == 5][0]
    assert h5["ret"] > 0 and h5["excess"] is not None and h5["delisted"] is False
    assert h5["bench_method"] == "ew_median_focus" and "bench_twii" in h5
    assert grade.run(root, rules, dates[-1]) == 0    # 再跑一次不會重複


def test_delisted_stock_is_kept_in_sample(root):
    dates = write_history(root, n_days=40, delist="1000")  # 最後 10 天消失
    rules = score.load_rules(root)
    ledger.append(root, "picks", [{"pick_id": "2026-W10-01", "week": "2026-W10", "code": "1000",
                                   "data_date": dates[20], "sent": True}], key=("week", "code"))
    grade.run(root, rules, dates[-1])
    h5 = [x for x in ledger.read(root, "outcomes") if x["ref_id"] == "2026-W10-01" and x["horizon"] == 5][0]
    assert h5["ret"] is not None   # 到期日前就有收盤價，正常算


def test_benchmark_is_equal_weight_median_of_focus_stocks(root):
    """基準 = 同日 focus 股票（這裡是 10 檔半導體）報酬的中位數，不含食品業。"""
    dates = write_history(root, n_days=40, heroes={str(1000 + i) for i in range(4)})
    rules = score.load_rules(root)
    rules["grade"]["min_bench_stocks"] = 5
    ledger.append(root, "picks", [{"pick_id": "X-01", "week": "X", "code": "1009", "data_date": dates[20],
                                   "sent": False, "variant": "base"}], key=("week", "code"))
    grade.run(root, rules, dates[-1])
    o = [x for x in ledger.read(root, "outcomes") if x["ref_id"] == "X-01" and x["horizon"] == 5][0]
    close = pd.concat([pd.read_csv(f"{root}/history/daily/{d}.csv.gz", dtype={"code": str}) for d in (dates[20], dates[25])])
    w = close[close.code.isin([str(1000 + i) for i in range(10)])].pivot(index="date", columns="code", values="close")
    expected = float(((w.iloc[1] / w.iloc[0] - 1) * 100).median())
    assert abs(o["bench_ret"] - expected) < 1e-2
