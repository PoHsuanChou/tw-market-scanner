"""每日抓取：證交所、櫃買、集保 → raw/ 與 history/ 與 out/fetch_status.json。

原則：
  - 單一來源失敗不中斷其他來源，失敗與否記在 fetch_status.json。
  - 資料日期以回傳內容裡的日期欄位為準，不是執行日。
  - 某來源的日期和主日期（上市日成交）對不上，就把那個來源的欄位留 None，並記 warning，
    不補值、不猜。
  - 公開資料 API 只給「最新一個交易日」，沒有歷史，所以每天都要存。

用法：python -m scanner.fetch [--root .]
"""
import os
import gzip
import json
import logging
import argparse
from datetime import datetime, timezone

import yaml

from scanner import normalize as N
from scanner.http import get_json, FetchError
from scanner import history

log = logging.getLogger("fetch")

TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"
URLS = {
    "twse_day": f"{TWSE}/exchangeReport/STOCK_DAY_ALL",
    "twse_margin": f"{TWSE}/exchangeReport/MI_MARGN",
    "twse_rev": f"{TWSE}/opendata/t187ap05_L",
    "twse_profile": f"{TWSE}/opendata/t187ap03_L",
    "tpex_day": f"{TPEX}/tpex_mainboard_quotes",
    "tpex_margin": f"{TPEX}/tpex_mainboard_margin_balance",
    "tpex_inst": f"{TPEX}/tpex_3insti_daily_trading",
    "tpex_rev": f"{TPEX}/mopsfin_t187ap05_O",
    "tpex_profile": f"{TPEX}/mopsfin_t187ap03_O",
    "tdcc": "https://openapi.tdcc.com.tw/v1/opendata/1-5",
}
# 選用來源：抓不到不影響主流程（只是少了注意/處置旗標）
OPTIONAL_URLS = {
    "twse_notice": f"{TWSE}/announcement/notice",
    "twse_punish": f"{TWSE}/announcement/punish",
    "tpex_notice": f"{TPEX}/tpex_trading_warning_information",
    "tpex_disposal": f"{TPEX}/tpex_disposal_information",
}
T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"


def _save_gz(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def _try(status, name, fn):
    """執行一個抓取，成功回傳結果，失敗回 None 並記錄。"""
    try:
        res = fn()
        status[name] = {"ok": True}
        return res
    except FetchError as e:
        status[name] = {"ok": False, "error": str(e)}
        log.error(f"{name} 失敗：{e}")
        return None


def fetch_all(root="."):
    status = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": {}}
    st = status["sources"]
    raw = {}
    for name, url in URLS.items():
        raw[name] = _try(st, name, lambda u=url: get_json(u))

    opt = {}
    for name, url in OPTIONAL_URLS.items():
        opt[name] = _try(st, name, lambda u=url: get_json(u, retries=2))
    flags = N.warnings_flags(opt.get("twse_notice"), opt.get("twse_punish"),
                             opt.get("tpex_notice"), opt.get("tpex_disposal"))

    # ---- 主日期：上市日成交 ----
    if not raw.get("twse_day"):
        status["data_date"] = None
        status["fatal"] = "上市日成交抓不到，無法決定資料日期，本次不寫入 history"
        _write_status(root, status)
        return status
    twse_day = N.twse_day(raw["twse_day"])
    data_date = next((v["date"] for v in twse_day.values() if v["date"]), None)
    status["data_date"] = data_date
    st["twse_day"].update(rows=len(twse_day), date=data_date)

    # ---- 三大法人（上市要帶日期） ----
    t86 = {}
    if data_date:
        payload = _try(st, "twse_t86", lambda: get_json(
            T86_URL, params={"date": data_date.replace("-", ""), "selectType": "ALLBUT0999", "response": "json"}))
        t86 = N.twse_t86(payload) if payload else {}
        if payload and not t86:
            st["twse_t86"] = {"ok": False, "error": f"T86 回應無資料：{payload.get('stat')}"}
        else:
            st.get("twse_t86", {}).update(rows=len(t86))

    # ---- 其他來源正規化 ----
    twse_margin = N.twse_margin(raw["twse_margin"]) if raw.get("twse_margin") else {}
    tpex_day = N.tpex_day(raw["tpex_day"]) if raw.get("tpex_day") else {}
    tpex_margin = N.tpex_margin(raw["tpex_margin"]) if raw.get("tpex_margin") else {}
    tpex_inst = N.tpex_inst(raw["tpex_inst"]) if raw.get("tpex_inst") else {}
    twse_rev = N.revenue(raw["twse_rev"], "TWSE") if raw.get("twse_rev") else {}
    tpex_rev = N.revenue(raw["tpex_rev"], "TPEx") if raw.get("tpex_rev") else {}
    twse_prof = N.twse_profile(raw["twse_profile"]) if raw.get("twse_profile") else {}
    tpex_prof = N.tpex_profile(raw["tpex_profile"]) if raw.get("tpex_profile") else {}

    # ---- 日期一致性：對不上的來源整個丟掉（欄位留 None） ----
    def _check(name, rows):
        dates = {v.get("date") for v in rows.values() if v.get("date")}
        if not rows:
            return {}
        if dates != {data_date}:
            msg = f"日期 {sorted(dates)} 與主日期 {data_date} 不一致，該來源欄位留空"
            st.setdefault(name, {})["warning"] = msg
            log.warning(f"{name}：{msg}")
            return {}
        st.setdefault(name, {}).update(rows=len(rows), date=data_date)
        return rows
    tpex_day = _check("tpex_day", tpex_day)
    tpex_margin = _check("tpex_margin", tpex_margin)
    tpex_inst = _check("tpex_inst", tpex_inst)

    # ---- 集保 ----
    with open(os.path.join(root, "config", "tdcc_levels.yaml"), encoding="utf-8") as f:
        levels = yaml.safe_load(f)
    tdcc = N.tdcc(raw["tdcc"], levels) if raw.get("tdcc") else {}
    if tdcc:
        bad = [c for c, v in tdcc.items() if not v["sum_ok"]]
        tdcc_date = next(iter(tdcc.values()))["date"]
        st["tdcc"].update(rows=len(tdcc), date=tdcc_date, sum_mismatch=len(bad))
        if len(bad) > len(tdcc) * 0.05:
            st["tdcc"]["warning"] = f"{len(bad)} 檔股數加總與分級 17 不符，級距可能改了，C 類別暫停使用"
            log.warning(st["tdcc"]["warning"])
            tdcc = {}

    # ---- 產業名稱對照 ----
    profile = {**twse_prof, **tpex_prof}
    rev = {**twse_rev, **tpex_rev}
    ind_map, conflicts = N.industry_names(profile, rev)
    if conflicts:
        log.warning(f"產業代碼對到多個名稱：{conflicts}")
    status["industry_code_conflicts"] = conflicts

    # ---- 寫 raw（只存每日變動的三項；其餘存最新一份覆蓋） ----
    for name in ("twse_day", "tpex_day", "tpex_margin", "tpex_inst", "twse_margin"):
        if raw.get(name):
            _save_gz(os.path.join(root, "raw", data_date, f"{name}.json.gz"), raw[name])
    _save_gz(os.path.join(root, "raw", data_date, "twse_t86.json.gz"), {"rows": t86}) if t86 else None
    for name in ("twse_rev", "tpex_rev", "twse_profile", "tpex_profile", "tdcc"):
        if raw.get(name):
            _save_gz(os.path.join(root, "raw", "_latest", f"{name}.json.gz"), raw[name])

    # ---- history ----
    res = history.write_all(
        root, data_date,
        day={**twse_day, **tpex_day},
        margin={**twse_margin, **tpex_margin},
        inst={**t86, **tpex_inst},
        profile=profile, rev=rev, ind_map=ind_map, tdcc=tdcc,
        flags=flags,
        inst_markets={m for m, ok in [("TWSE", bool(t86)), ("TPEx", bool(tpex_inst))] if ok},
    )
    status["history"] = res
    _write_status(root, status)
    return status


def _write_status(root, status):
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    with open(os.path.join(root, "out", "fetch_status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    status = fetch_all(args.root)
    ok = [k for k, v in status["sources"].items() if v.get("ok")]
    bad = [k for k, v in status["sources"].items() if not v.get("ok")]
    print(f"資料日期 {status.get('data_date')}｜成功 {len(ok)}｜失敗 {bad or '無'}")
    if status.get("fatal"):
        raise SystemExit(status["fatal"])


if __name__ == "__main__":
    main()
