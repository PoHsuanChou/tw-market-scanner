from scanner import fetch


def test_twse_lagging_openapi_falls_back_to_dated(tmp_path, monkeypatch):
    """上市 OpenAPI 還停在前一天、上櫃已是新的一天 → 上市改用帶日期版，主日期跟上櫃一致。"""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tdcc_levels.yaml").write_text("{}")
    monkeypatch.setattr(fetch.history, "write_all", lambda *a, **k: {})
    monkeypatch.setattr(fetch.history, "upsert_index", lambda *a, **k: None)
    monkeypatch.setattr(fetch.history, "upsert_shares", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "_save_gz", lambda *a, **k: None)
    saved = []
    monkeypatch.setattr(fetch, "_write_status", lambda root, status: saved.append(status))

    twse_old = [{"Date": "1151102", "Code": "2330", "Name": "台積電", "ClosingPrice": "1"}]
    tpex_new = [{"Date": "1151103", "SecuritiesCompanyCode": "3324", "CompanyName": "雙鴻", "Close": "2"}]
    mi = {"stat": "OK", "tables": [{"title": "每日收盤行情", "fields": ["證券代號", "證券名稱", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"],
                                    "data": [["2330", "台積電", "1", "1", "1", "1", "1", "1"]]}]}
    mg = {"tables": [{"title": "融資融券彙總", "data": [["2330", 0, 0, 0, 0, 5, 6, 0, 0, 0, 0, 1, 2]]}]}

    def fake(url, params=None, **kw):
        if url == fetch.URLS["twse_day"]:
            return twse_old
        if url == fetch.URLS["tpex_day"]:
            return tpex_new
        if url == fetch.MI_INDEX_URL:
            assert params["date"] == "20261103"
            return mi
        if url == fetch.MI_MARGN_URL:
            return mg
        return []
    monkeypatch.setattr(fetch, "get_json", fake)
    status = fetch.fetch_all(str(tmp_path))
    assert status["data_date"] == "2026-11-03"
    assert status["sources"]["twse_day_dated"]["ok"]


def _setup(tmp_path, monkeypatch, written):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tdcc_levels.yaml").write_text("{}")
    monkeypatch.setattr(fetch.history, "write_all", lambda root, d, **k: written.append((d, k)) or {})
    monkeypatch.setattr(fetch.history, "upsert_index", lambda *a, **k: None)
    monkeypatch.setattr(fetch.history, "upsert_shares", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "_save_gz", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "_write_status", lambda root, status: None)


TWSE_OLD = [{"Date": "1151113", "Code": "2330", "Name": "台積電", "ClosingPrice": "1"}]
TPEX_NEW = [{"Date": "1151116", "SecuritiesCompanyCode": "3324", "CompanyName": "雙鴻", "Close": "2"}]
MI_NEW = {"stat": "OK", "tables": [{"title": "每日收盤行情", "fields": ["證券代號", "證券名稱", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"],
                                    "data": [["2330", "台積電", "1", "1", "1", "1", "1", "1"]]}]}


def test_margin_not_published_yet_still_moves_to_new_date_and_leaves_margin_blank(tmp_path, monkeypatch):
    """17:30 時帶日期的上市融資融券還沒公布：仍然換到上櫃的新日期，上市融資留空，不能退回去重寫前一天。"""
    written = []
    _setup(tmp_path, monkeypatch, written)

    def fake(url, params=None, **kw):
        return {fetch.URLS["twse_day"]: TWSE_OLD, fetch.URLS["tpex_day"]: TPEX_NEW, fetch.MI_INDEX_URL: MI_NEW,
                fetch.MI_MARGN_URL: {"stat": "很抱歉，沒有符合條件的資料!"}}.get(url, [])
    monkeypatch.setattr(fetch, "get_json", fake)
    status = fetch.fetch_all(str(tmp_path))
    assert status["data_date"] == "2026-11-16" and not status.get("skipped")
    assert "note" in status["sources"]["twse_margin_dated"]
    (d, k), = written
    assert d == "2026-11-16" and "3324" in k["day"] and "2330" in k["day"]   # 上櫃沒有被丟掉
    assert "2330" not in k["margin"]                                          # 上市融資留空，不用舊日期的


def test_twse_dated_not_published_skips_instead_of_rewriting_previous_day(tmp_path, monkeypatch):
    written = []
    _setup(tmp_path, monkeypatch, written)

    def fake(url, params=None, **kw):
        return {fetch.URLS["twse_day"]: TWSE_OLD, fetch.URLS["tpex_day"]: TPEX_NEW,
                fetch.MI_INDEX_URL: {"stat": "很抱歉，沒有符合條件的資料!"}}.get(url, [])
    monkeypatch.setattr(fetch, "get_json", fake)
    status = fetch.fetch_all(str(tmp_path))
    assert status["skipped"] and status["data_date"] is None
    assert written == []                                                      # 2026-11-13 的檔案沒被碰
