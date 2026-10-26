import json
import os
from datetime import date

import yaml

from scanner import us_leaders as U


def _fake(growth):
    """回傳一個假的 fetch：8 季，YoY 由 growth(季) 決定，最後一季加速。"""
    ends = [date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31),
            date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)]

    def fetch(name, ticker, metric):
        vals = [100.0] * 4 + [100.0 * (1 + growth(i) / 100) for i in range(4)]
        raw = [{"end": e, "val": v * 1e9, "yoy": None} for e, v in zip(ends, vals)]
        return {"ticker": ticker, "series": [{"end": e.isoformat(), "val_b": v, "yoy": None} for e, v in zip(ends, vals)],
                "_raw": raw}
    return fetch


def _cfg(root, chains):
    with open(os.path.join(root, "config", "us_leaders.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({"chains": chains}, f, allow_unicode=True)


CHAIN = {"name": "AI運算", "metric": "revenue", "companies": {"A": "AAA", "B": "BBB"},
         "tw_categories": ["ABF載板/CCL"], "themes": ["ABF載板", "封測"]}


def test_without_edgar_ua_it_skips_and_explains(root, monkeypatch):
    monkeypatch.delenv("EDGAR_UA", raising=False)
    assert U.run(root) is None
    st = json.load(open(os.path.join(root, "out", "us_leaders_status.json"), encoding="utf-8"))
    assert st["ok"] is False and "EDGAR_UA" in st["reason"]


def test_accelerating_chain_becomes_tailwind_for_categories_and_themes(root):
    _cfg(root, [CHAIN])
    out = U.run(root, fetch=_fake(lambda i: [10, 10, 10, 30][i]), today=date(2026, 1, 20))
    c = out["chains"][0]
    assert c["direction"] == "順風" and c["agg"]["accel"] == 20.0
    assert out["signals"][0]["key"] == "ABF載板/CCL" and out["signals"][0]["expires"] == "2026-04-30"
    assert set(out["theme_signals"]) == {"ABF載板", "封測"}
    assert len(out["new_updates"]) == 2                        # 兩家都是第一次看到
    again = U.run(root, fetch=_fake(lambda i: [10, 10, 10, 30][i]), today=date(2026, 1, 21))
    assert again["new_updates"] == []                          # 沒有新財報


def test_flat_chain_sends_no_signal(root):
    _cfg(root, [CHAIN])
    out = U.run(root, fetch=_fake(lambda i: 10), today=date(2026, 1, 20))
    assert out["chains"][0]["direction"] == "平穩" and out["signals"] == [] and out["theme_signals"] == {}
