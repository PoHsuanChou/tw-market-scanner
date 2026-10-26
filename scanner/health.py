"""資料健康檢查：每個平日掃描之後跑一次，發現問題就讓 job 失敗，GitHub 會寄 email 通知 repo 擁有者。

為什麼不用「掃描自己寄信」：掃描根本沒跑（例如 GitHub 排程被略過）時，它不可能寄信。
所以另外排一個獨立的 job，在掃描之後檢查結果；它發現問題就以失敗結束，
GitHub 預設會對「失敗的 workflow」寄 email，不需要在公開 repo 放任何信箱密碼。

檢查項目（每一項都有白話說明，寫進 out/health.json，網站會顯示）：
  1. 今天（台灣時間、平日）的每日掃描有沒有執行
  2. 每個資料來源有沒有抓到（證交所、櫃買、集保、月營收…）
  3. 證交所有交易資料、但我們還沒收到的日子（不看今天，避免公布時間差造成誤報）
  4. 市場資料（美股、匯率）有沒有抓到最新；是不是沿用舊的
  5. 美股領先指標有沒有正常更新

用法：python -m scanner.health [--root .]   # 有問題時 exit code 1
"""
import os
import json
import logging
import argparse
from datetime import datetime, date, timedelta, timezone

from scanner.http import get_json, FetchError
from scanner import normalize as N

log = logging.getLogger("health")
TW = timezone(timedelta(hours=8))
AFTER_SCAN = (17, 45)                 # 台灣時間 17:45 以後才要求「今天的掃描已經跑過」
REQUIRED = ["twse_day", "twse_margin", "twse_rev", "twse_profile", "twse_t86", "tpex_day", "tpex_margin",
            "tpex_inst", "tpex_rev", "tpex_profile", "tdcc"]
SOURCE_NAME = {"twse_day": "上市日成交", "twse_margin": "上市融資融券", "twse_rev": "上市月營收",
               "twse_profile": "上市公司資料", "twse_t86": "上市三大法人", "tpex_day": "上櫃日成交",
               "tpex_margin": "上櫃融資融券", "tpex_inst": "上櫃三大法人", "tpex_rev": "上櫃月營收",
               "tpex_profile": "上櫃公司資料", "tdcc": "集保大戶資料"}
MI_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _parse_utc(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def twse_has_trading(d, fetch=get_json):
    """證交所帶日期的收盤行情有沒有這一天的資料（沒有＝休市）。查不到回 None，不亂判斷。"""
    try:
        payload = fetch(MI_INDEX, params={"date": d.replace("-", ""), "type": "ALLBUT0999", "response": "json"},
                        retries=2, sleep=2.0)
    except FetchError:
        return None
    day, _ = N.twse_mi_index(payload, d)
    return bool(day)


def check(root=".", now=None, has_trading=twse_has_trading):
    now = (now or datetime.now(timezone.utc)).astimezone(TW)
    today = now.date()
    after_scan = (now.hour, now.minute) >= AFTER_SCAN
    problems = []

    def add(level, code, msg):
        problems.append({"level": level, "code": code, "msg": msg})

    fs = _load(os.path.join(root, "out", "fetch_status.json")) or {}
    # 1. 今天的掃描有沒有跑
    if today.weekday() < 5 and after_scan:
        run_at = _parse_utc(fs.get("run_at", ""))
        if not run_at or run_at.astimezone(TW).date() != today:
            last = run_at.astimezone(TW).strftime("%m/%d %H:%M") if run_at else "沒有紀錄"
            add("error", "scan_not_run", f"今天（{today.isoformat()}）的每日掃描沒有執行，最近一次是 {last}。可能是 GitHub 排程被略過，請到 Actions 手動按 Daily Scan。")
    # 2. 資料來源
    for k in REQUIRED:
        v = (fs.get("sources") or {}).get(k)
        if v is not None and not v.get("ok"):
            add("error", f"source_{k}", f"{SOURCE_NAME[k]}抓不到：{v.get('error', '原因不明')}")
        elif v and v.get("warning"):
            add("warn", f"source_{k}_warn", f"{SOURCE_NAME[k]}：{v['warning']}")
    if fs.get("fatal"):
        add("error", "fatal", f"每日掃描中止：{fs['fatal']}")
    # 3. 證交所有、我們沒有的交易日（不含今天）
    data_date = fs.get("data_date")
    if data_date:
        d = date.fromisoformat(data_date) + timedelta(days=1)
        checked = 0
        while d < today and checked < 7:
            if d.weekday() < 5:
                checked += 1
                if has_trading(d.isoformat()):
                    add("error", f"behind_{d.isoformat()}", f"證交所有 {d.isoformat()} 的交易資料，但我們的資料還停在 {data_date}。")
                    break
            d += timedelta(days=1)
    # 4. 市場資料
    mk = _load(os.path.join(root, "out", "market.json"))
    if mk is None:
        add("warn", "market_missing", "沒有市場資料檔（網站總覽的美股與匯率會是舊的）")
    else:
        if mk.get("failed"):
            add("warn", "market_failed", f"Yahoo 這次抓不到：{'、'.join(mk['failed'])}（網站沿用上一次的資料）")
        upd = _parse_utc(mk.get("updated_at", ""))
        if today.weekday() < 5 and after_scan and upd and upd.astimezone(TW).date() != today:
            add("warn", "market_old", "市場資料今天沒有更新")
    # 5. 美股領先指標
    us = _load(os.path.join(root, "out", "us_leaders_status.json"))
    if us is not None:
        at = _parse_utc(us.get("at", ""))
        if us.get("ok") is False:
            add("warn", "us_failed", f"美股領先指標更新失敗：{us.get('reason', '原因不明')}")
        elif at and (now - at.astimezone(TW)).days >= 4:
            add("warn", "us_old", f"美股領先指標已經 {(now - at.astimezone(TW)).days} 天沒有更新（排程是週二到週六）")

    out = {"checked_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "ok": not problems,
           "problems": problems}
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    with open(os.path.join(root, "out", "health.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    res = check(ap.parse_args().root)
    if res["ok"]:
        print("資料健康：沒有問題")
        return
    for p in res["problems"]:
        print(f"[{p['level']}] {p['msg']}")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
