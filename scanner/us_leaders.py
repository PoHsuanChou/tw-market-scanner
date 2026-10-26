"""美股領先指標（從私人 repo track_stock 的 sources/us_leaders.py 搬來）。

美國客戶的資本支出／出貨，通常領先台灣供應商的營收 1 到 2 季。
config/us_leaders.yaml 定義多條供應鏈，每條鏈有自己的美股領先群（capex 或 revenue），
獨立聚合 YoY 與加速度、獨立判定，再對應到台灣的產業分類（tw_categories）與細分主題（themes）。

  1. ticker → CIK 由 SEC 官方對照表自動解析
  2. 同公司出現在多條鏈只抓一次（(cik, metric) 快取）
  3. 10-Q 累計值還原單季、跨會計年度日曆季對齊（edgar_client）
  4. 鏈總量 YoY 只用「本季+去年同季皆已公布」的共同集合，避免假減速
  5. 偵測新財報落地（history/us_leaders_state.json）

和私人 repo 版本的差別：不寄信、不寫 tailwind.json，改輸出 out/us_leaders.json，
私人 repo 與網站讀這個檔。SEC 要求附真實識別資訊，所以一定要設 EDGAR_UA（GitHub Secrets），
沒設就不抓、寫 out/us_leaders_status.json 說明原因，不影響每日掃描。

用法：python -m scanner.us_leaders [--root .]
"""
import os
import json
import logging
import argparse
from datetime import date, datetime, timedelta, timezone

import yaml

from scanner import edgar_client

log = logging.getLogger("us_leaders")

METRIC_TAGS = {"capex": edgar_client.CAPEX_TAGS, "revenue": edgar_client.REVENUE_TAGS}
METRIC_LABEL = {"capex": "Capex", "revenue": "營收"}


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


_series_cache = {}


def fetch_company(name, ticker, metric, quarters_keep=10):
    """回傳 {"ticker","series","_raw"} 或 None（抓不到就缺席，不編數字）。"""
    cik = edgar_client.cik_for_ticker(ticker)
    if not cik:
        log.warning(f"  {name}({ticker}): SEC 對照表查無 CIK")
        return None
    key = (cik, metric)
    if key not in _series_cache:
        series = edgar_client.quarterly_series(cik, METRIC_TAGS[metric])
        _series_cache[key] = edgar_client.with_yoy(series) if series else []
    raw = _series_cache[key][-quarters_keep:]
    if not raw:
        log.warning(f"  {name}({ticker}): EDGAR 抓不到季度 {metric}")
        return None
    return {"ticker": ticker,
            "series": [{"end": p["end"].isoformat(), "val_b": round(p["val"] / 1e9, 2), "yoy": p["yoy"]}
                       for p in raw],
            "_raw": raw}


def aggregate_chain(chain_data, min_reported):
    by_bucket = {}
    for name, d in chain_data.items():
        for p in d["_raw"]:
            b = edgar_client.calendar_bucket(p["end"])
            if b:
                by_bucket.setdefault(b, {})[name] = p["val"]

    def yoy_for(bucket):
        cur = by_bucket.get(bucket) or {}
        base = by_bucket.get(edgar_client.prev_bucket(bucket, 4)) or {}
        common = sorted(set(cur) & set(base))
        if len(common) < min_reported:
            return None
        c = sum(cur[n] for n in common)
        b = sum(base[n] for n in common)
        if b <= 0:
            return None
        return {"names": common, "total_b": round(c / 1e9, 1), "yoy": round((c / b - 1) * 100, 1)}

    for bucket in sorted(by_bucket.keys(), reverse=True):
        cur = yoy_for(bucket)
        if not cur:
            continue
        prev = yoy_for(edgar_client.prev_bucket(bucket, 1))
        accel = round(cur["yoy"] - prev["yoy"], 1) if prev else None
        return {"bucket": f"{bucket[0]}Q{bucket[1]}", "total_b": cur["total_b"], "yoy": cur["yoy"],
                "prev_yoy": prev["yoy"] if prev else None, "accel": accel,
                "n": len(cur["names"]), "names": cur["names"]}
    return None


def chain_verdict(agg, accel_pp):
    """回傳 (方向, 說明)。方向：順風／逆風／平穩／資料不足。"""
    if not agg:
        return "資料不足", "公布家數不足或基期缺失"
    if agg["accel"] is None:
        return "資料不足", "沒有前一季可比"
    if agg["accel"] >= accel_pp:
        return "順風", f"加速中（{agg['accel']:+.1f}pp）"
    if agg["accel"] <= -accel_pp:
        return "逆風", f"減速中（{agg['accel']:+.1f}pp）"
    return "平穩", f"平穩（{agg['accel']:+.1f}pp）"


def signals(chains_out, today, days):
    """只有順風／逆風才發訊號（平穩不發，避免雜訊）。回傳 (依舊產業分類, 依細分主題)。"""
    exp = (today + timedelta(days=days)).isoformat()
    by_cat, by_theme = [], {}
    for c in chains_out:
        if c["direction"] not in ("順風", "逆風"):
            continue
        agg = c["agg"]
        detail = (f"{c['name']}鏈 {agg['bucket']} {METRIC_LABEL[c['metric']]} YoY {agg['yoy']:+.1f}%"
                  f"（{agg['accel']:+.1f}pp，{agg['n']} 家）")
        sig = {"direction": c["direction"], "detail": detail, "chain": c["name"],
               "date": today.isoformat(), "expires": exp, "small": agg["n"] <= 1}
        for cat in c.get("tw_categories") or []:
            by_cat.append({"scope": "category", "key": cat, **sig})
        for th in c.get("themes") or []:
            by_theme.setdefault(th, []).append(sig)
    return by_cat, by_theme


def run(root=".", fetch=fetch_company, today=None):
    today = today or date.today()
    status_path = os.path.join(root, "out", "us_leaders_status.json")
    if not os.environ.get("EDGAR_UA") and fetch is fetch_company:
        msg = "沒有設定 EDGAR_UA（SEC 規定要附「姓名 email」），本次不抓。請在 GitHub repo 的 Settings → Secrets 加上 EDGAR_UA。"
        log.error(msg)
        _save_json(status_path, {"ok": False, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": msg})
        return None
    with open(os.path.join(root, "config", "us_leaders.yaml"), encoding="utf-8") as f:
        chains_cfg = (yaml.safe_load(f) or {}).get("chains") or []
    with open(os.path.join(root, "config", "rules.yaml"), encoding="utf-8") as f:
        rcfg = (yaml.safe_load(f) or {}).get("us_leaders") or {}
    accel_pp, default_min = rcfg.get("accel_pp", 5), rcfg.get("min_reported", 2)

    state_path = os.path.join(root, "history", "us_leaders_state.json")
    state = _load_json(state_path, {})
    new_updates, chains_out = [], []
    for ch in chains_cfg:
        cname, metric = ch["name"], ch.get("metric", "revenue")
        if metric not in METRIC_TAGS:
            continue
        data, missing = {}, []
        for name, ticker in (ch.get("companies") or {}).items():
            d = fetch(name, ticker, metric)
            if d:
                data[name] = d
            else:
                missing.append(f"{name}({ticker})")
        agg = aggregate_chain(data, ch.get("min_reported", default_min)) if data else None
        direction, verdict = chain_verdict(agg, accel_pp)
        chains_out.append({"name": cname, "metric": metric, "agg": agg, "direction": direction, "verdict": verdict,
                           "tw_note": ch.get("tw_note", ""), "tw_categories": ch.get("tw_categories") or [],
                           "themes": ch.get("themes") or [], "missing": missing,
                           "companies": {n: {"ticker": d["ticker"], "series": d["series"]} for n, d in data.items()}})
        for name, d in data.items():
            latest_end = d["series"][-1]["end"]
            if state.get(f"{name}:{metric}") != latest_end:
                new_updates.append({"company": name, "end": latest_end, "chain": cname})
                state[f"{name}:{metric}"] = latest_end

    if not any(c["companies"] for c in chains_out):
        msg = "EDGAR 全部抓不到（網路、SEC 封鎖或 EDGAR_UA 格式不對），保留上一次的結果"
        log.error(msg)
        _save_json(status_path, {"ok": False, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": msg})
        return None

    by_cat, by_theme = signals(chains_out, today, rcfg.get("signal_days", 100))
    out = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "date": today.isoformat(),
           "accel_pp": accel_pp, "chains": chains_out, "signals": by_cat, "theme_signals": by_theme,
           "new_updates": new_updates}
    _save_json(os.path.join(root, "out", "us_leaders.json"), out)
    _save_json(state_path, state)
    _save_json(status_path, {"ok": True, "at": out["updated_at"], "new_updates": len(new_updates)})
    log.info(f"美股領先指標：{len(chains_out)} 條鏈、{len(by_cat)} 個產業訊號、新財報 {len(new_updates)} 筆")
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    run(ap.parse_args().root)


if __name__ == "__main__":
    main()
