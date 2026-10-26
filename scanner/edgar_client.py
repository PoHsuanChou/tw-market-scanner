#!/usr/bin/env python3
"""
scanner/edgar_client.py — SEC EDGAR XBRL 客戶端（從私人 repo track_stock 的 common/edgar_client.py 搬來）
=========================================
免費、免 API key。抓美股公司的季度財務指標（capex / 營收），
供 scanner/us_leaders.py 做台灣供應鏈的領先指標。

SEC 規定：
  - 必帶識別用 User-Agent：環境變數 EDGAR_UA = "姓名 email"（沒設定就不抓）
  - 速率上限 10 req/s，這裡保守限 5 req/s

關鍵細節：10-Q 現金流量表項目（含 capex）是「會計年度起算的累計值」，
單季值 = 相鄰累計值相減（同一 start 日期）。quarterly_series() 處理這件事。
會計年度各家不同（MSFT 6 月截止、NVDA 1 月截止），跨公司比較用
calendar_bucket() 對齊到最近的日曆季。

資料誠實原則：抓不到就回空 list，不編造。
"""

import os
import time
import logging
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger("edgar_client")

CONCEPT_URL = ("https://data.sec.gov/api/xbrl/companyconcept/"
               "CIK{cik:010d}/us-gaap/{tag}.json")

# SEC 規定要附真實的識別資訊（「姓名 email」）。公開 repo 不能把 email 寫在程式裡，
# 一律從環境變數（GitHub Secrets 的 EDGAR_UA）讀；沒設定就不抓，由呼叫端跳過。
HEADERS = {"User-Agent": os.environ.get("EDGAR_UA", "")}

# 各公司申報用的 XBRL 標籤不完全一致，按順序 fallback
CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets",
              "PaymentsForCapitalImprovements"]
REVENUE_TAGS = [
    # 現代 ASC 606 常見標籤
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",

    # 部分公司仍使用的舊標籤
    "SalesRevenueNet",
    "Revenues",
]

_last_call = 0.0

TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
_cik_cache = None


def cik_for_ticker(ticker):
    """用 SEC 官方對照表解析 ticker → CIK（一次請求，行程內快取）。查不到回 None。"""
    global _cik_cache
    if _cik_cache is None:
        _throttle()
        try:
            r = requests.get(TICKER_URL, headers=HEADERS, timeout=20)
            r.raise_for_status()
            _cik_cache = {v["ticker"].upper(): int(v["cik_str"])
                          for v in r.json().values()}
        except Exception as e:
            log.warning(f"  ⚠️ CIK 對照表抓取失敗: {e}")
            _cik_cache = {}
    return _cik_cache.get(str(ticker).upper())


def _throttle(min_interval=0.2):
    global _last_call
    wait = _last_call + min_interval - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def fetch_concept(cik, tag):
    """單一 XBRL concept。404（該公司沒用這個標籤）回 None，其他錯誤也回 None。"""
    _throttle()
    url = CONCEPT_URL.format(cik=int(cik), tag=tag)
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json()
        if r.status_code != 404:
            log.warning(f"  ⚠️ EDGAR {r.status_code} [{tag}] CIK{cik}")
    except Exception as e:
        log.warning(f"  ⚠️ EDGAR 請求失敗 [{tag}] CIK{cik}: {e}")
    return None


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def quarterly_series(cik, tags):
    """
    回傳 [{"end": date, "val": float(美元)}, ...] 按 end 升冪的「單季」序列。

    會嘗試「每一個」tag 並合併結果（同一 end 以較新 filed 為準），不是撿到
    第一個有回應的 tag 就停。原因：公司常在某年切換 XBRL 標籤（如會計準則
    更新），舊標籤仍會回傳 200（只是內容停在換標籤那年，不是 404），
    「撿到就停」會永久鎖死在舊標籤上，導致「最新一季」其實是好幾年前的
    資料、YoY 也配對到不連續的季度上（曾在 Amazon/NVIDIA/Micron 上實測到）。

    處理三種申報型態：
      - 直接的單季區間（duration 75-105 天）→ 直接用
      - YTD 累計（半年/九月/全年）→ 找同一 start、最近一個較早的累計值相減還原
      - 同一區間多次申報（原報+重編）→ 取最新 filed
    """
    merged = {}  # end -> (val, filed)

    for tag in tags:
        data = fetch_concept(cik, tag)
        if not data:
            continue
        entries = (data.get("units") or {}).get("USD") or []
        if not entries:
            continue

        spans = {}  # (start, end) -> (val, filed)
        for u in entries:
            try:
                s, e = _d(u["start"]), _d(u["end"])
                v, filed = float(u["val"]), u.get("filed", "")
            except Exception:
                continue
            k = (s, e)
            if k not in spans or filed > spans[k][1]:
                spans[k] = (v, filed)

        quarters = {}  # end -> (val, filed)，本 tag 內部先解出單季
        # 1) 天生就是單季的區間
        for (s, e), (v, filed) in spans.items():
            if 75 <= (e - s).days <= 105:
                if e not in quarters or filed > quarters[e][1]:
                    quarters[e] = (v, filed)
        # 2) YTD 累計相減（同 start、取「相差最接近一季」的較早累計）
        for (s, e), (v, filed) in spans.items():
            if (e - s).days <= 105:
                continue
            best = None  # (gap, s2, e2, v2, filed2)
            for (s2, e2), (v2, filed2) in spans.items():
                if s2 == s and e2 < e and 75 <= (e - e2).days <= 105:
                    gap = (e - e2).days
                    if best is None or gap < best[0]:
                        best = (gap, v2, filed2)
            if best is not None:
                _, v2, filed2 = best
                val, filed_eff = v - v2, max(filed, filed2)
                if e not in quarters or filed_eff > quarters[e][1]:
                    quarters[e] = (val, filed_eff)

        # 合併進總表：同一 end 以較新 filed 為準（跨 tag 也適用）
        for e, (v, filed) in quarters.items():
            if e not in merged or filed > merged[e][1]:
                merged[e] = (v, filed)

    return [{"end": e, "val": v} for e, (v, _) in sorted(merged.items())]


def with_yoy(series):
    """為每季找去年同季（end 相差 365±45 天）算 YoY%。找不到基期 yoy=None。"""
    out = []
    for i, p in enumerate(series):
        yoy = None
        best, best_gap = None, 46
        for q in series[:i]:
            gap = abs((p["end"] - q["end"]).days - 365)
            if gap < best_gap:
                best, best_gap = q, gap
        if best and best["val"] > 0:
            yoy = round((p["val"] / best["val"] - 1) * 100, 1)
        out.append({**p, "yoy": yoy})
    return out


def calendar_bucket(d):
    """
    會計季末 → 最近的日曆季 (year, q)。距離超過 45 天回 None（不硬塞）。
    例：NVDA 1/26 → 前一年 Q4；MSFT 3/31 → Q1。
    """
    candidates = []
    for yr in (d.year - 1, d.year, d.year + 1):
        for m, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            candidates.append(date(yr, m, day))
    nearest = min(candidates, key=lambda c: abs((d - c).days))
    if abs((d - nearest).days) > 45:
        return None
    return (nearest.year, (nearest.month - 1) // 3 + 1)


def prev_bucket(b, n=1):
    y, q = b
    q -= n
    while q < 1:
        q += 4
        y -= 1
    return (y, q)