import pandas as pd

from scanner import score
from tests.conftest import write_history, write_revenue


def test_A_B_C_are_null_when_history_is_too_short(root):
    write_history(root, n_days=3)
    write_revenue(root, "2025-12", [("1000", 40.0, 20.0, "2026-01-01")])
    dates = score.trading_dates(root)
    df, _ = score.score_stocks(root, dates[-1])
    hero = df[df.code == "1000"].iloc[0]
    assert hero["A"] is None and hero["B"] is None and hero["C"] is None   # 不是 False
    assert hero["D"] is True


def test_signals_true_for_trending_stock_with_enough_history(root):
    write_history(root, n_days=30)
    write_revenue(root, "2025-12", [("1000", 40.0, 20.0, "2026-01-01")])
    df, s = score.score_stocks(root, score.trading_dates(root)[-1])
    h = df[df.code == "1000"].iloc[0]
    assert (h["A"], h["B"], h["C"], h["D"]) == (True, True, True, True)
    assert h["c_basis"] == "margin_proxy" and h["d_basis"] == "ytd_yoy_proxy"
    assert s["benchmark_r20"] is not None


def test_revenue_published_after_data_date_is_not_visible(root):
    """D 不能偷看未來：出表日期晚於資料日期的營收一律當作還沒公布。"""
    dates = write_history(root, n_days=30)
    write_revenue(root, "2025-12", [("1000", 40.0, 20.0, "2099-01-01")])
    df, _ = score.score_stocks(root, dates[-1])
    assert df[df.code == "1000"].iloc[0]["D"] is None


def test_D_uses_previous_month_when_available(root):
    dates = write_history(root, n_days=30)
    write_revenue(root, "2025-11", [("1000", 50.0, 10.0, "2025-12-10")])
    write_revenue(root, "2025-12", [("1000", 40.0, 10.0, "2026-01-01")])  # 40 < 前月 50 → 沒有加速
    df, _ = score.score_stocks(root, dates[-1])
    h = df[df.code == "1000"].iloc[0]
    assert h["D"] is False and h["d_basis"] == "prev_month_yoy"


def test_industry_needs_enough_known_stocks(root):
    write_history(root, n_days=30)
    write_revenue(root, "2025-12", [(str(1000 + i), 40.0, 20.0, "2026-01-01") for i in range(10)])
    rules = score.load_rules(root)
    df, _ = score.score_stocks(root, score.trading_dates(root)[-1], rules)
    ind = {i["industry"]: i for i in score.score_industries(df, rules)}["半導體業"]
    assert ind["D"] is True and ind["n_stocks"] == 10
    df2 = df.head(3)
    assert score.score_industries(df2, rules)[0]["D"] is None   # 少於 5 檔 → 未知
