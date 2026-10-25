"""每日主流程：抓資料 → 評分 → 寫 ledger → （週五）推薦 → 對答案 → out/latest.json。

用法：
  python -m scanner.run_daily               # 完整流程
  python -m scanner.run_daily --no-fetch    # 用 history 裡最新一天重算（本機除錯）
  python -m scanner.run_daily --force-picks # 不是週五也產生推薦（本機測試用）
"""
import os
import json
import logging
import argparse

from scanner import fetch, score, picks as picks_mod, ledger, grade, history, themes

log = logging.getLogger("run_daily")


def _prev_industry_scores(root, data_date, rules):
    dates = score.trading_dates(root, data_date)
    n = rules["industry"]["delta_trading_days"]
    if len(dates) <= n:
        return {}
    d0 = dates[-n - 1]
    return {r["industry"]: r["score"] for r in ledger.read(root, "industry_scores") if r["data_date"] == d0}


def _write_theme_history(root):
    """out/theme_history.json：網頁畫主題強弱圖用。真實紀錄優先，缺的日子用回放補，並標來源。"""
    h = themes.history(ledger.read(root, "theme_scores"), ledger.read(root, "theme_scores", "ledger_replay"),
                       ledger.read(root, "theme_events"), ledger.read(root, "theme_events", "ledger_replay"))
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    with open(os.path.join(root, "out", "theme_history.json"), "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, separators=(",", ":"))


def _clean(v):
    if isinstance(v, float) and v != v:
        return None
    return v


def run(root=".", do_fetch=True, force_picks=False):
    rules = score.load_rules(root)
    if do_fetch:
        status = fetch.fetch_all(root)
        data_date = status.get("data_date")
        if status.get("fatal") or not data_date:
            raise SystemExit(status.get("fatal", "沒有資料日期"))
    else:
        data_date = score.trading_dates(root)[-1]

    df, summary = score.score_stocks(root, data_date, rules)
    inds = score.score_industries(df, rules, _prev_industry_scores(root, data_date, rules))
    ver = rules["version"]

    n_ind = ledger.append(root, "industry_scores",
                          [{"data_date": data_date, "rules_version": ver, **i} for i in inds],
                          key=("data_date", "industry"))
    stock_cols = ["code", "name", "market", "industry", "close", "A", "B", "C", "D", "c_basis", "d_basis", "r20"]
    n_stk = ledger.append(root, "stock_scores",
                          [{"data_date": data_date, "rules_version": ver,
                            **{k: _clean(r[k]) for k in stock_cols}} for r in df.to_dict("records")],
                          key=("data_date", "code"))

    # 細分主題（themes.yaml reviewed 為 true 才使用）
    tcfg = themes.load(root)
    theme_rows, strong_top, events_today = [], None, []
    if tcfg:
        theme_rows, strong_top = themes.score(df, tcfg, history.load_shares(root), rules)
        ledger.append(root, "theme_scores",
                      [{"data_date": data_date, "rules_version": ver, **t} for t in theme_rows],
                      key=("data_date", "theme"))
        # 主題轉強：和前一個交易日比（每天都檢查，不等週五）
        dates = score.trading_dates(root, data_date)
        prev_rows = [r for r in ledger.read(root, "theme_scores") if len(dates) > 1 and r["data_date"] == dates[-2]]
        events_today = themes.events(theme_rows, prev_rows, data_date)
        ledger.append(root, "theme_events", [{"rules_version": ver, **e} for e in events_today],
                      key=("data_date", "theme"))
        _write_theme_history(root)

    n_pick = n_rej = 0
    week_picks = []
    if force_picks or picks_mod.is_week_close(data_date):
        week_picks, universe = picks_mod.select(df, inds, rules, data_date, strong_top)
        stamp = [{"rules_version": ver, **p} for p in week_picks]
        # 同一週已經推薦過就不再寫（key = week+code+variant，週五重跑不會改變當週結果）
        n_pick = ledger.append(root, "picks", stamp, key=("week", "code", "variant"))
        n_rej = ledger.append(root, "pick_universe", [{"rules_version": ver, **r} for r in universe],
                              key=("week", "code"))
    else:
        log.info(f"{data_date} 不是週五，今天不產生推薦")

    n_out = grade.run(root, rules, data_date)

    latest = {
        "data_date": data_date, "rules_version": ver, "summary": summary,
        "industries": inds,
        "themes": theme_rows,
        "theme_events_today": events_today,
        # 只有 base 版會寄出；momentum 版是並行記錄的對照，放在 shadow 欄位，私人 repo 不寄、只顯示
        "picks_this_week": [p for p in ledger.read(root, "picks")
                            if p["week"] == picks_mod.week_id(data_date) and p.get("variant", "base") == "base"],
        "shadow_picks_this_week": [p for p in ledger.read(root, "picks")
                                   if p["week"] == picks_mod.week_id(data_date) and p.get("variant", "base") != "base"],
        "stocks": [{k: _clean(r[k]) for k in stock_cols + ["flag"]} for r in df.to_dict("records")],
    }
    os.makedirs(os.path.join(root, "out"), exist_ok=True)
    with open(os.path.join(root, "out", "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)
    msg = (f"{data_date}｜產業 {len(inds)}（新增 ledger {n_ind}）｜個股 {len(df)}（新增 {n_stk}）｜"
           f"推薦新增 {n_pick}、對照組 {n_rej}｜對答案新增 {n_out}")
    print(msg)
    return latest


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--force-picks", action="store_true")
    a = ap.parse_args()
    run(a.root, do_fetch=not a.no_fetch, force_picks=a.force_picks)


if __name__ == "__main__":
    main()
