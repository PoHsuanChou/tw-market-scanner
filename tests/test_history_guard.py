import os

import pandas as pd

from scanner import history


def _day(codes):
    return {c: {"market": m, "name": c, "date": "2026-11-13", "close": 1.0, "open": 1, "high": 1, "low": 1,
                "volume": 1, "turnover": 1} for c, m in codes}


def _write(root, codes, margin=True):
    return history.write_all(root, "2026-11-13", day=_day(codes),
                             margin={c: {"margin_bal": 5, "margin_prev": 5} for c, _ in codes} if margin else {},
                             inst={}, profile={}, rev={}, ind_map={}, tdcc={})


def _rows(root):
    return pd.read_csv(os.path.join(root, "history", "daily", "2026-11-13.csv.gz"), dtype={"code": str})


def test_existing_day_is_not_overwritten_by_less_complete_data(tmp_path):
    root = str(tmp_path)
    both = [("2330", "TWSE"), ("2317", "TWSE"), ("3324", "TPEx"), ("6488", "TPEx")]
    _write(root, both)
    res = _write(root, both[:2])                                  # 傍晚那次：少了整個上櫃
    assert res["daily_kept_existing"] and len(_rows(root)) == 4
    res = _write(root, both, margin=False)                        # 融資全空也比較差
    assert res["daily_kept_existing"] and _rows(root)["margin_bal"].notna().all()


def test_more_complete_data_overwrites(tmp_path):
    root = str(tmp_path)
    _write(root, [("2330", "TWSE"), ("3324", "TPEx")], margin=False)
    res = _write(root, [("2330", "TWSE"), ("3324", "TPEx")])      # 晚上那次：補上融資
    assert "daily_kept_existing" not in res and _rows(root)["margin_bal"].notna().all()
