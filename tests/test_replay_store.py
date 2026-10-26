import json
import os

from scanner import ledger, replay_store, accuracy, run_daily
from scanner import score as score_mod


def _seed(root):
    base = replay_store.BASE
    for i, d in enumerate(["2024-03-01", "2024-03-04", "2025-03-03", "2025-03-04"]):
        ledger.append(root, "theme_scores", [{"data_date": d, "theme": "散熱", "strong": True, "swing_strong": i % 2 == 0,
                                              "strong_members": 2, "n": 3, "top": []}], key=("data_date", "theme"), base=base)
    outs = []
    for i, (d, ex) in enumerate([("2024-03-01", 5.0), ("2024-03-04", -1.0), ("2025-03-03", 2.0), ("2025-03-04", 3.0)]):
        outs.append({"ref_type": "theme", "ref_id": f"{d}|T|散熱", "horizon": 20, "start_date": d, "excess": ex,
                     "swing_strong": i % 2 == 0})
    ledger.append(root, "outcomes", outs, key=("ref_id", "horizon"), base=base)


def test_export_keeps_summary_by_year_and_only_recent_theme_rows(root, monkeypatch):
    monkeypatch.setattr(replay_store, "RECENT_DAYS", 2)
    _seed(root)
    s = replay_store.export(root)
    assert s["period"] == {"from": "2024-03-01", "to": "2025-03-04", "trading_days": 4}
    assert set(s["by_year"]) == {"2024", "2025"}
    assert s["by_year"]["2024"]["themes"]["強"]["20"]["n"] == 1 and s["by_year"]["2025"]["themes"]["強"]["20"]["median"] == 2.0
    assert s["all"]["themes"]["強"]["20"]["n"] == 2
    recent = replay_store.load_recent(root)
    assert {r["data_date"] for r in recent["theme_scores"]} == {"2025-03-03", "2025-03-04"}      # 只留最近 2 天


def test_accuracy_reads_summary_not_the_details(root):
    _seed(root)
    replay_store.export(root)
    import shutil
    shutil.rmtree(os.path.join(root, replay_store.BASE))                    # 模擬雲端：沒有回放明細
    from tests.conftest import write_history
    dates = write_history(root, n_days=30)
    acc = accuracy.build(root, score_mod.load_rules(root), dates[-1])
    assert acc["replay"]["themes"]["強"]["20"]["n"] == 2                       # 仍然讀得到回放摘要
    assert set(acc["replay_by_year"]) == {"2024", "2025"}


def test_ledger_replay_is_not_tracked_by_git():
    gi = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".gitignore"), encoding="utf-8").read()
    assert "ledger_replay/" in gi


def test_by_year_report_shows_the_gap_between_strong_and_not_strong(root):
    from scanner import report
    cell = lambda m, n=40: {"n": n, "median": m, "win": 0.6}
    ys = lambda a, b: {"themes": {"強": {"20": cell(a)}, "不強": {"20": cell(b)}}, "theme_events": {"20": cell(1.0)},
                       "picks": {"theme_swing_top3": {"20": cell(2.0, 10)}}}
    summary = {"horizons": [20], "rules_version": "x", "period": {"from": "2023-10-02", "to": "2026-10-22", "trading_days": 700},
               "all": ys(5.0, 3.0), "by_year": {"2023": ys(1.0, 2.0), "2024": ys(8.0, 3.0)}}
    md = report.by_year_md(summary)
    assert "| 2023 | -1.00 個百分點（強 +1.00、不強 +2.00） |" in md              # 這一年強的反而比較差，要看得出來
    assert "| 2024 | +5.00 個百分點" in md and "| 全部 | +2.00 個百分點" in md
    assert "n=10 ⚠" in md                                                          # 樣本不足有標示
