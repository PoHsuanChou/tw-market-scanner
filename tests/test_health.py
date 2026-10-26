import json
import os
from datetime import datetime, timedelta, timezone

from scanner import health

TW = timezone(timedelta(hours=8))
MON_EVENING = datetime(2026, 10, 26, 18, 45, tzinfo=TW)       # 週一晚上，掃描之後


def _write(root, name, obj):
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    json.dump(obj, open(os.path.join(root, "out", name), "w", encoding="utf-8"))


def _ok_status(ran_at, data_date="2026-10-26"):
    return {"run_at": ran_at.astimezone(timezone.utc).isoformat(), "data_date": data_date,
            "sources": {k: {"ok": True} for k in health.REQUIRED}}


def _market(ran_at, failed=()):
    return {"updated_at": ran_at.astimezone(timezone.utc).isoformat(), "failed": list(failed)}


def codes(res):
    return {p["code"] for p in res["problems"]}


def test_healthy_run_has_no_problems(root):
    ran = datetime(2026, 10, 26, 17, 31, tzinfo=TW)
    _write(root, "fetch_status.json", _ok_status(ran))
    _write(root, "market.json", _market(ran))
    res = health.check(root, MON_EVENING, has_trading=lambda d: False)
    assert res["ok"] and res["problems"] == []


def test_scan_that_never_ran_today_is_an_error(root):
    ran = datetime(2026, 10, 23, 17, 31, tzinfo=TW)                  # 上週五跑的，今天（週一）沒跑
    _write(root, "fetch_status.json", _ok_status(ran, "2026-10-23"))
    res = health.check(root, MON_EVENING, has_trading=lambda d: False)
    assert "scan_not_run" in codes(res)


def test_weekend_and_before_scan_time_are_not_checked(root):
    ran = datetime(2026, 10, 23, 17, 31, tzinfo=TW)
    _write(root, "fetch_status.json", _ok_status(ran, "2026-10-23"))
    sat = datetime(2026, 10, 24, 18, 45, tzinfo=TW)
    assert "scan_not_run" not in codes(health.check(root, sat, has_trading=lambda d: False))
    mon_morning = datetime(2026, 10, 26, 9, 0, tzinfo=TW)
    assert "scan_not_run" not in codes(health.check(root, mon_morning, has_trading=lambda d: False))


def test_failed_source_and_yahoo_failure_are_reported_in_plain_words(root):
    ran = datetime(2026, 10, 26, 17, 31, tzinfo=TW)
    st = _ok_status(ran)
    st["sources"]["tdcc"] = {"ok": False, "error": "重試 3 次仍失敗"}
    _write(root, "fetch_status.json", st)
    _write(root, "market.json", _market(ran, failed=["vix", "twd"]))
    res = health.check(root, MON_EVENING, has_trading=lambda d: False)
    msgs = " ".join(p["msg"] for p in res["problems"])
    assert "集保大戶資料抓不到" in msgs and "Yahoo 這次抓不到：vix、twd" in msgs
    assert {"source_tdcc", "market_failed"} <= codes(res)


def test_days_the_exchange_has_but_we_do_not_are_flagged_but_holidays_and_today_are_not(root):
    ran = datetime(2026, 10, 26, 17, 31, tzinfo=TW)
    _write(root, "fetch_status.json", _ok_status(ran, "2026-10-22"))
    _write(root, "market.json", _market(ran))
    # 10/23（五）休市、10/26（一）是今天不檢查 → 沒問題
    assert health.check(root, MON_EVENING, has_trading=lambda d: False)["ok"]
    # 10/23 證交所其實有資料 → 我們漏了
    res = health.check(root, MON_EVENING, has_trading=lambda d: d == "2026-10-23")
    assert "behind_2026-10-23" in codes(res)
    # 查不到（None）就不亂報
    assert health.check(root, MON_EVENING, has_trading=lambda d: None)["ok"]


def test_us_leaders_failure_and_staleness(root):
    ran = datetime(2026, 10, 26, 17, 31, tzinfo=TW)
    _write(root, "fetch_status.json", _ok_status(ran))
    _write(root, "market.json", _market(ran))
    _write(root, "us_leaders_status.json", {"ok": False, "at": ran.isoformat(), "reason": "沒有設定 EDGAR_UA"})
    assert "us_failed" in codes(health.check(root, MON_EVENING, has_trading=lambda d: False))
    old = ran - timedelta(days=6)
    _write(root, "us_leaders_status.json", {"ok": True, "at": old.astimezone(timezone.utc).isoformat()})
    assert "us_old" in codes(health.check(root, MON_EVENING, has_trading=lambda d: False))
