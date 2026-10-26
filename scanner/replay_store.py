"""回放結果的存法：明細不進 git，只留「摘要」與「最近 130 天的主題紀錄」。

為什麼：三年回放的明細（outcomes.jsonl）會超過 70 MB，接近 GitHub 單檔 100 MB 的上限，
而且每次重算 git 就多一份幾十 MB 的歷史。明細可以用 `python -m scanner.replay score` 隨時重算，
所以只把「雲端每天需要用到的東西」存進 git：

  replay/summary.json       準確率摘要（全部期間 + 分年度）與回放最近一週的推薦（網站在真實推薦出現前當範例）
  replay/theme_recent.json  最近 130 個交易日的主題強弱與轉強事件（真實紀錄還不夠 120 天時，熱力圖用它補前面的日子）

ledger_replay/（明細）在 .gitignore 裡，只留在本機。
"""
import os
import json
from datetime import datetime, timezone

from scanner import ledger, score

BASE = "ledger_replay"
RECENT_DAYS = 130


def _path(root, name):
    return os.path.join(root, "replay", name)


def _year(r):
    return str(r.get("start_date", ""))[:4]


def _latest_picks(root):
    rows = ledger.read(root, "picks", BASE)
    if not rows:
        return None
    from scanner.run_daily import _latest_picks
    return _latest_picks(root, BASE)


def export(root, rules=None):
    from scanner import accuracy                  # 延後載入：accuracy 也會 import 這個模組，避免循環
    rules = rules or score.load_rules(root)
    hz = rules["grade"]["horizons"]
    outcomes = ledger.read(root, "outcomes", BASE)
    years = sorted({_year(r) for r in outcomes if _year(r)})
    scores = ledger.read(root, "theme_scores", BASE)
    dates = sorted({r["data_date"] for r in scores})
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rules_version": rules["version"], "horizons": hz,
        "period": {"from": dates[0] if dates else None, "to": dates[-1] if dates else None, "trading_days": len(dates)},
        "all": accuracy.summarize(outcomes, hz),
        "by_year": {y: accuracy.summarize([r for r in outcomes if _year(r) == y], hz) for y in years},
        "latest_picks": _latest_picks(root),
    }
    keep = set(dates[-RECENT_DAYS:])
    recent = {"theme_scores": [r for r in scores if r["data_date"] in keep],
              "theme_events": [r for r in ledger.read(root, "theme_events", BASE) if r["data_date"] in keep]}
    os.makedirs(os.path.join(root, "replay"), exist_ok=True)
    with open(_path(root, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, separators=(",", ":"))
    with open(_path(root, "theme_recent.json"), "w", encoding="utf-8") as f:
        json.dump(recent, f, ensure_ascii=False, separators=(",", ":"))
    return summary


def _load(root, name):
    try:
        with open(_path(root, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_summary(root):
    return _load(root, "summary.json")


def load_recent(root):
    return _load(root, "theme_recent.json") or {"theme_scores": [], "theme_events": []}
