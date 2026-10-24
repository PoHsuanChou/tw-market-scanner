import os
import shutil

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY_COLS = ["date", "code", "market", "name", "industry", "close", "open", "high", "low", "volume",
              "turnover", "foreign_net", "trust_net", "margin_bal", "margin_prev", "short_bal",
              "short_prev", "flag"]


def make_dates(n, start="2026-01-05"):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


@pytest.fixture
def root(tmp_path):
    shutil.copytree(os.path.join(ROOT, "config"), tmp_path / "config")
    return str(tmp_path)


def write_history(root, n_days=30, n_stocks=230, hero="1000", delist=None, heroes=()):
    """合成歷史：n_stocks 檔普通股，前 10 檔在「半導體業」。
    hero（預設 1000）與 heroes（一組代號）：持續上漲、外資投信每天買超、融資下降。其他股票原地小幅波動。"""
    rng = np.random.default_rng(0)
    dates = make_dates(n_days)
    os.makedirs(os.path.join(root, "history", "daily"), exist_ok=True)
    margin = {1000 + i: 5000 for i in range(n_stocks)}
    base = {1000 + i: 100.0 + i for i in range(n_stocks)}
    for di, d in enumerate(dates):
        rows = []
        for i in range(n_stocks):
            code = 1000 + i
            if delist and str(code) == delist and di >= n_days - 10:
                continue
            if str(code) == hero or str(code) in heroes:
                base[code] *= 1.007
                f, t = 500000, 100000
                margin[code] -= 40
            else:
                base[code] *= 1 + rng.normal(0, 0.003)
                f, t = int(rng.integers(-1000, 1000)), 0
            rows.append({"date": d, "code": str(code), "market": "TWSE", "name": f"股{code}",
                         "industry": "半導體業" if i < 10 else "食品工業",
                         "close": round(base[code], 2), "open": base[code], "high": base[code], "low": base[code],
                         "volume": 1e6, "turnover": 2e8, "foreign_net": f, "trust_net": t,
                         "margin_bal": margin[code], "margin_prev": margin[code] + 40, "short_bal": 0,
                         "short_prev": 0, "flag": ""})
        pd.DataFrame(rows, columns=DAILY_COLS).to_csv(
            os.path.join(root, "history", "daily", f"{d}.csv.gz"), index=False, compression="gzip")
    return dates


def write_revenue(root, ym, rows):
    """rows: [(code, yoy, ytd_yoy, published)]"""
    os.makedirs(os.path.join(root, "history", "revenue"), exist_ok=True)
    pd.DataFrame([{"code": c, "market": "TWSE", "ym": ym, "published": p, "industry": "半導體業",
                   "revenue": 1, "revenue_prev_month": 1, "revenue_last_year": 1,
                   "yoy": y, "mom": 0, "ytd_yoy": t} for c, y, t, p in rows]).to_csv(
        os.path.join(root, "history", "revenue", f"{ym}.csv.gz"), index=False, compression="gzip")
