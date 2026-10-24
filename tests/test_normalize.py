from scanner import normalize as N


def test_roc_to_iso():
    assert N.roc_to_iso("1151022") == "2026-10-22"
    assert N.roc_to_iso("20261022") == "2026-10-22"
    assert N.roc_to_iso("") is None and N.roc_to_iso(None) is None and N.roc_to_iso("abc") is None
    assert N.roc_ym_to_iso("11509") == "2026-09"


def test_to_num_nulls_are_none_not_zero():
    assert N.to_num("1,234") == 1234
    assert N.to_num("-12,694,626") == -12694626
    assert N.to_num("14.41") == 14.41
    assert N.to_num("") is None and N.to_num("--") is None and N.to_num(None) is None


def test_clean_keys_strips_bom_and_spaces():
    assert N.clean_keys({"﻿資料日期": 1, " Foreign": 2}) == {"資料日期": 1, "Foreign": 2}


def test_tpex_inst_handles_leading_space_keys():
    row = {"Date": "1151022", "SecuritiesCompanyCode": "3324",
           "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy": "1",
           " Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference": "715697",
           "SecuritiesInvestmentTrustCompanies-Difference": "25000"}
    assert N.tpex_inst([row])["3324"] == {"date": "2026-10-22", "foreign_net": 715697, "trust_net": 25000}


def test_twse_t86_uses_field_names_and_commas():
    payload = {"stat": "OK", "fields": ["證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數"],
               "data": [["2330", "台積電  ", "-12,694,626", "1,349,951"]]}
    assert N.twse_t86(payload) == {"2330": {"foreign_net": -12694626, "trust_net": 1349951}}
    assert N.twse_t86({"stat": "很抱歉，沒有符合條件的資料!"}) == {}


def test_is_common_stock_excludes_etf_and_warrants():
    assert N.is_common_stock("2330") and N.is_common_stock("6488")
    assert not N.is_common_stock("0050") and not N.is_common_stock("00400A") and not N.is_common_stock("053040")


def test_notice_placeholder_row_is_ignored():
    placeholder = [{"Number": "0", "Code": "", "Name": ""}]
    assert N.warnings_flags(placeholder, [], [], []) == {}
    assert N.warnings_flags([{"Code": "2330"}], [{"Code": "2330"}], [], []) == {"2330": "disposal"}


def _lv(code, lv, holders, shares, pct):
    return {"證券代號": code, "持股分級": str(lv), "人數": str(holders), "股數": str(shares),
            "占集保庫存數比例%": str(pct), "﻿資料日期": "20261022"}


def test_tdcc_sum_check_and_big400():
    cfg = {"total_level": 17, "big400_levels": [12, 13, 14, 15], "big1000_levels": [15]}
    rows = [_lv("1111", 1, 10, 100, 10.0), _lv("1111", 12, 1, 300, 30.0), _lv("1111", 15, 1, 600, 60.0),
            _lv("1111", 16, 0, 0, 0.0), _lv("1111", 17, 12, 1000, 100.0)]
    r = N.tdcc(rows, cfg)["1111"]
    assert r["sum_ok"] and r["big400_pct"] == 90.0 and r["big400_holders"] == 2 and r["date"] == "2026-10-22"
    rows[0] = _lv("1111", 1, 10, 150, 10.0)
    assert not N.tdcc(rows, cfg)["1111"]["sum_ok"]
