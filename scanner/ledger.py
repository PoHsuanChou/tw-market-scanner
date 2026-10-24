"""只增不改的判斷紀錄（JSONL，一行一筆）。

這個模組刻意只提供 append 與 read：
  - append 只以 'a' 模式開檔，已存在的行永遠不會被改動或刪除。
  - 以 key 欄位去重：同一天重跑不會寫出重複的行。
  - 要更正舊紀錄，寫一筆 {"type": "correction", ...}，不要動舊的。
回測的可信度全靠這個：事後不能偷偷改當時的判斷。
"""
import os
import json


def _path(root, name):
    p = os.path.join(root, "ledger", f"{name}.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def read(root, name):
    p = _path(root, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append(root, name, records, key):
    """寫入 key 尚未出現過的紀錄，回傳實際新增的筆數。"""
    p = _path(root, name)
    seen = {tuple(r.get(k) for k in key) for r in read(root, name)}
    added = 0
    with open(p, "a", encoding="utf-8") as f:
        for r in records:
            k = tuple(r.get(x) for x in key)
            if k in seen:
                continue
            seen.add(k)
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
            added += 1
    return added
