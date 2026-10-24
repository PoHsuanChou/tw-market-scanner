import os

from scanner import ledger


def test_append_dedupes_and_never_rewrites(root):
    a = ledger.append(root, "t", [{"k": 1, "v": "old"}, {"k": 2, "v": "x"}], key=("k",))
    assert a == 2
    p = os.path.join(root, "ledger", "t.jsonl")
    before = open(p, encoding="utf-8").read()
    b = ledger.append(root, "t", [{"k": 1, "v": "CHANGED"}, {"k": 3, "v": "y"}], key=("k",))
    assert b == 1
    after = open(p, encoding="utf-8").read()
    assert after.startswith(before)
    assert [r["v"] for r in ledger.read(root, "t")] == ["old", "x", "y"]


def test_rerun_same_day_adds_nothing(root):
    recs = [{"data_date": "2026-10-22", "code": "2330", "n": 1}]
    assert ledger.append(root, "s", recs, key=("data_date", "code")) == 1
    assert ledger.append(root, "s", recs, key=("data_date", "code")) == 0
