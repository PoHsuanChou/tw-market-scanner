"""HTTP 抓取：逾時、重試、完整性檢查。

實測發現：TPEx 的融資融券檔案曾經只下載到一半（245 KB，實際 550 KB），
JSON 在中間截斷。所以每次下載都要檢查 Content-Length 與 JSON 是否能完整解析，
不完整就重試，重試用盡才算失敗。
"""
import json
import time
import logging

import requests

log = logging.getLogger("http")
UA = {"User-Agent": "Mozilla/5.0"}


class FetchError(Exception):
    pass


def get_json(url, params=None, retries=3, timeout=90, sleep=3.0):
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            r.raise_for_status()
            expected = r.headers.get("Content-Length")
            if expected and "Content-Encoding" not in r.headers and len(r.content) != int(expected):
                raise FetchError(f"下載不完整：{len(r.content)} / {expected} bytes")
            return json.loads(r.content.decode("utf-8-sig"))
        except (requests.RequestException, ValueError, FetchError) as e:
            last = e
            log.warning(f"{url} 第 {attempt} 次失敗：{e}")
            if attempt < retries:
                time.sleep(sleep * attempt)
    raise FetchError(f"{url} 重試 {retries} 次仍失敗：{last}")
