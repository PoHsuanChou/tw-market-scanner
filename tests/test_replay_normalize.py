import pandas as pd

from scanner import normalize as N


def _mi(stat="OK"):
    return {"stat": stat, "tables": [
        {"title": "115年01月05日 價格指數(臺灣證券交易所)", "fields": ["指數", "收盤指數"],
         "data": [["寶島股價指數", "1"], ["發行量加權股價指數", "30,105.04"]]},
        {"title": "115年01月05日 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))",
         "fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"],
         "data": [["2330", "台積電", "76,182,999", "160,941", "126,883,687,153", "1,630.00", "1,695.00", "1,625.00", "1,670.00"],
                  ["1101", "台泥", "0", "0", "0", "--", "--", "--", "--"]]}]}


def test_twse_mi_index_prices_and_index():
    day, twii = N.twse_mi_index(_mi(), "2026-01-05")
    assert twii == 30105.04
    assert day["2330"]["close"] == 1670.0 and day["2330"]["turnover"] == 126883687153
    assert day["1101"]["close"] is None            # 沒成交 → None，不是 0


def test_holiday_returns_empty():
    assert N.twse_mi_index({"stat": "很抱歉，沒有符合條件的資料!"}, "2026-01-01") == ({}, None)


def test_twse_margin_dated_positions():
    row = ["2330", "台積電", "597", "434", "21", "35,330", "35,472", "6,483,092", "1", "10", "0", "6", "15", "6,483,092", "0", " "]
    p = {"tables": [{"title": None, "data": []}, {"title": "115年10月22日 融資融券彙總 (股票)", "data": [row]}]}
    assert N.twse_margin_dated(p) == {"2330": {"margin_prev": 35330, "margin_bal": 35472, "short_prev": 6, "short_bal": 15}}


def test_tpex_inst_dated_positions_and_field_count_guard():
    row = ["3324", "雙鴻", "630,411", "445,400", "185,011", "0", "0", "0", "630,411", "445,400", "185,011",
           "10,000", "0", "10,000", "100,325", "4,000", "96,325", "50,976", "50,818", "158", "151,301", "54,818",
           "96,483", "291,494"]
    p = {"tables": [{"title": "三大法人買賣明細資訊", "fields": ["x"] * 24, "data": [row]}]}
    assert N.tpex_inst_dated(p) == {"3324": {"foreign_net": 185011, "trust_net": 10000}}
    p["tables"][0]["fields"] = ["x"] * 23          # 欄位數變了 → 整份丟掉，不猜
    assert N.tpex_inst_dated(p) == {}


def test_tpex_otc_and_margin_dated_use_field_names_with_stray_spaces():
    otc = {"tables": [{"title": "上櫃股票每日收盤行情(不含定價)",
                       "fields": ["代號", "名稱", "收盤 ", "漲跌", "開盤 ", "最高 ", "最低", "成交股數  ", " 成交金額(元)"],
                       "data": [["3324", "雙鴻", "1,005.00", "+5.00", "1,010.00", "1,020.00", "990.00", "1,888,000", "1,899,136,000"]]}]}
    d = N.tpex_otc_dated(otc, "2026-01-05")["3324"]
    assert d["close"] == 1005.0 and d["volume"] == 1888000 and d["turnover"] == 1899136000
    mg = {"tables": [{"title": "上櫃股票融資融券餘額", "fields": ["代號", "名稱", "前資餘額(張)", "資餘額", "前券餘額(張)", "券餘額"],
                      "data": [["3324", "雙鴻", "4,481", "4,600", "568", "566"]]}]}
    assert N.tpex_margin_dated(mg)["3324"] == {"margin_prev": 4481, "margin_bal": 4600, "short_prev": 568, "short_bal": 566}


def test_mops_revenue_table_skips_non_company_rows():
    cols = [f"c{i}" for i in range(11)]
    t = pd.DataFrame([["2330", "台積電", 511857399, 514805337, 330980920, -0.57, 54.64, 3898726974, 2762963851, 41.1, "-"],
                      ["合計", "", 1, 1, 1, 1, 1, 1, 1, 1, "-"]], columns=cols)
    out = N.mops_revenue_table([t, pd.DataFrame({"a": [1]})], "TWSE", "2026-09", "2026-10-10")
    assert list(out) == ["2330"] and out["2330"]["yoy"] == 54.64 and out["2330"]["published"] == "2026-10-10"
    assert out["2330"]["ytd_yoy"] == 41.1
