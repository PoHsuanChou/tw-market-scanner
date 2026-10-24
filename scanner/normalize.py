"""把證交所、櫃買、集保的原始 JSON 轉成統一欄位。

統一規則：
  - 日期一律 ISO（2026-10-22）。民國 1151022 → 西元 +1911。
  - 數字去逗號；空字串、"--"、"-" → None（不是 0）。
  - 所有 key 先 strip，並去掉 BOM。TPEx 三大法人有的欄位名開頭帶空白。
  - 單位：價格＝元；成交量、法人買賣超＝股；成交金額＝元；融資融券餘額＝張（交易所原單位）；
    月營收＝千元（交易所原單位，用 2330 九月營收 511,857,399 千元＝約 5,119 億核對過量級）。
  - 代號一律純代號字串，市場用 market 欄位區分（TWSE / TPEx）。
"""
import re

_NULLS = {"", "-", "--", "---", "X", "x"}


def roc_to_iso(s):
    """'1151022' → '2026-10-22'；'20261022' → '2026-10-22'；其他回 None。"""
    if s is None:
        return None
    s = str(s).strip().replace("/", "").replace("-", "")
    if re.fullmatch(r"\d{7}", s):
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def roc_ym_to_iso(s):
    """'11509' → '2026-09'。"""
    s = str(s or "").strip()
    if re.fullmatch(r"\d{5}", s):
        return f"{int(s[:3]) + 1911}-{s[3:5]}"
    return None


def to_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "")
    if s in _NULLS:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() and "." not in s else f


def clean_keys(row):
    return {str(k).replace("﻿", "").strip(): v for k, v in row.items()}


def is_common_stock(code):
    """4 碼數字且不以 00 開頭（排除 ETF）。TDR（91xx）與特別股不在此列，之後視需要再排除。"""
    return bool(re.fullmatch(r"[1-9]\d{3}", code or ""))


# ---------------- 日成交 ----------------

def twse_day(rows):
    out = {}
    for r in map(clean_keys, rows):
        code = str(r.get("Code", "")).strip()
        if not code:
            continue
        out[code] = {
            "market": "TWSE", "name": str(r.get("Name", "")).strip(),
            "date": roc_to_iso(r.get("Date")),
            "open": to_num(r.get("OpeningPrice")), "high": to_num(r.get("HighestPrice")),
            "low": to_num(r.get("LowestPrice")), "close": to_num(r.get("ClosingPrice")),
            "volume": to_num(r.get("TradeVolume")), "turnover": to_num(r.get("TradeValue")),
        }
    return out


def tpex_day(rows):
    out = {}
    for r in map(clean_keys, rows):
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if not code:
            continue
        out[code] = {
            "market": "TPEx", "name": str(r.get("CompanyName", "")).strip(),
            "date": roc_to_iso(r.get("Date")),
            "open": to_num(r.get("Open")), "high": to_num(r.get("High")),
            "low": to_num(r.get("Low")), "close": to_num(r.get("Close")),
            "volume": to_num(r.get("TradingShares")), "turnover": to_num(r.get("TransactionAmount")),
        }
    return out


# ---------------- 融資融券（張） ----------------

def twse_margin(rows):
    out = {}
    for r in map(clean_keys, rows):
        code = str(r.get("股票代號", "")).strip()
        if not code:
            continue
        out[code] = {
            "margin_bal": to_num(r.get("融資今日餘額")), "margin_prev": to_num(r.get("融資前日餘額")),
            "short_bal": to_num(r.get("融券今日餘額")), "short_prev": to_num(r.get("融券前日餘額")),
        }
    return out


def tpex_margin(rows):
    out = {}
    for r in map(clean_keys, rows):
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if not code:
            continue
        out[code] = {
            "date": roc_to_iso(r.get("Date")),
            "margin_bal": to_num(r.get("MarginPurchaseBalance")),
            "margin_prev": to_num(r.get("MarginPurchaseBalancePreviousDay")),
            "short_bal": to_num(r.get("ShortSaleBalance")),
            "short_prev": to_num(r.get("ShortSaleBalancePreviousDay")),
        }
    return out


# ---------------- 三大法人（股） ----------------

def twse_t86(payload):
    """T86 用欄位名找位置，不用固定索引。外資 = 外陸資（不含外資自營商）。"""
    if not payload or payload.get("stat") != "OK":
        return {}
    fields = [f.strip() for f in payload.get("fields", [])]
    try:
        i_code = fields.index("證券代號")
        i_for = fields.index("外陸資買賣超股數(不含外資自營商)")
        i_trust = fields.index("投信買賣超股數")
    except ValueError:
        return {}
    out = {}
    for row in payload.get("data", []):
        code = str(row[i_code]).strip()
        out[code] = {"foreign_net": to_num(row[i_for]), "trust_net": to_num(row[i_trust])}
    return out


_TPEX_FOREIGN = "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference"
_TPEX_TRUST = "SecuritiesInvestmentTrustCompanies-Difference"


def tpex_inst(rows):
    out = {}
    for raw in rows:
        r = clean_keys(raw)
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if not code:
            continue
        out[code] = {
            "date": roc_to_iso(r.get("Date")),
            "foreign_net": to_num(r.get(_TPEX_FOREIGN)),
            "trust_net": to_num(r.get(_TPEX_TRUST)),
        }
    return out


# ---------------- 月營收（千元） ----------------

def revenue(rows, market):
    code_key = "公司代號"
    out = {}
    for r in map(clean_keys, rows):
        code = str(r.get(code_key, "")).strip()
        if not code:
            continue
        out[code] = {
            "market": market,
            "ym": roc_ym_to_iso(r.get("資料年月")),
            "published": roc_to_iso(r.get("出表日期")),
            "industry": str(r.get("產業別", "")).strip(),
            "revenue": to_num(r.get("營業收入-當月營收")),
            "revenue_prev_month": to_num(r.get("營業收入-上月營收")),
            "revenue_last_year": to_num(r.get("營業收入-去年當月營收")),
            "yoy": to_num(r.get("營業收入-去年同月增減(%)")),
            "mom": to_num(r.get("營業收入-上月比較增減(%)")),
            "ytd_yoy": to_num(r.get("累計營業收入-前期比較增減(%)")),
        }
    return out


# ---------------- 公司基本資料 → 產業代碼 ----------------

def twse_profile(rows):
    out = {}
    for r in map(clean_keys, rows):
        code = str(r.get("公司代號", "")).strip()
        if code:
            out[code] = {"industry_code": str(r.get("產業別", "")).strip(),
                         "short_name": str(r.get("公司簡稱", "")).strip(),
                         "issued_shares": to_num(r.get("已發行普通股數或TDR原股發行股數"))}
    return out


def tpex_profile(rows):
    out = {}
    for r in map(clean_keys, rows):
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if code:
            out[code] = {"industry_code": str(r.get("SecuritiesIndustryCode", "")).strip(),
                         "short_name": str(r.get("CompanyAbbreviation", "")).strip(),
                         "issued_shares": to_num(r.get("IssueShares"))}
    return out


def industry_names(profile, rev):
    """產業代碼 → 中文名稱。不手寫對照表：用「基本資料的代碼」join「月營收的名稱」，
    同一代碼多數決。回傳 (對照表, 衝突清單)。"""
    votes = {}
    for code, p in profile.items():
        name = (rev.get(code) or {}).get("industry")
        if name and p.get("industry_code"):
            votes.setdefault(p["industry_code"], {}).setdefault(name, 0)
            votes[p["industry_code"]][name] += 1
    mapping, conflicts = {}, []
    for ic, names in votes.items():
        best = max(names, key=names.get)
        mapping[ic] = best
        if len(names) > 1:
            conflicts.append({"industry_code": ic, "names": names})
    return mapping, conflicts


# ---------------- 集保股權分散（週） ----------------

def tdcc(rows, levels_cfg):
    """每檔股票彙總：總人數、總股數、400 張以上與 1000 張以上的占比與人數。
    sum_ok：分級 1–16 的股數加總是否等於分級 17（驗證級距沒有改）。"""
    total_lv = levels_cfg["total_level"]
    big400 = set(levels_cfg["big400_levels"])
    big1000 = set(levels_cfg["big1000_levels"])
    by_code = {}
    for r in map(clean_keys, rows):
        code = str(r.get("證券代號", "")).strip()
        lv = to_num(r.get("持股分級"))
        if not code or lv is None:
            continue
        d = by_code.setdefault(code, {"date": roc_to_iso(r.get("資料日期")), "lv": {}})
        d["lv"][int(lv)] = (to_num(r.get("人數")) or 0, to_num(r.get("股數")) or 0,
                           to_num(r.get("占集保庫存數比例%")) or 0.0)
    out = {}
    for code, d in by_code.items():
        lv = d["lv"]
        if total_lv not in lv:
            continue
        holders, shares, _ = lv[total_lv]
        parts = sum(v[1] for k, v in lv.items() if k != total_lv)
        out[code] = {
            "date": d["date"], "holders": holders, "shares": shares,
            "big400_pct": round(sum(lv[k][2] for k in big400 if k in lv), 2),
            "big400_holders": sum(lv[k][0] for k in big400 if k in lv),
            "big1000_pct": round(sum(lv[k][2] for k in big1000 if k in lv), 2),
            "sum_ok": parts == shares,
        }
    return out


# ---------------- 注意股 / 處置股 ----------------

def warnings_flags(twse_notice, twse_punish, tpex_notice, tpex_disposal):
    """code → 'disposal' | 'notice'。處置優先於注意。來源是「當日公布」，不是歷史。
    TWSE 注意股在沒有公告時會回一筆 Code 為空的佔位資料，要略過。"""
    out = {}
    for r in map(clean_keys, twse_notice or []):
        c = str(r.get("Code", "")).strip()
        if c:
            out[c] = "notice"
    for r in map(clean_keys, tpex_notice or []):
        c = str(r.get("SecuritiesCompanyCode", "")).strip()
        if c:
            out[c] = "notice"
    for r in map(clean_keys, twse_punish or []):
        c = str(r.get("Code", "")).strip()
        if c:
            out[c] = "disposal"
    for r in map(clean_keys, tpex_disposal or []):
        c = str(r.get("SecuritiesCompanyCode", "")).strip()
        if c:
            out[c] = "disposal"
    return out
