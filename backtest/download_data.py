# -*- coding: utf-8 -*-
"""
AI-5D 回測系統 — 資料下載模組
1. 從 FinMind 取得上市櫃一般股票清單（僅 1 次 API 呼叫）
2. 從 Yahoo Finance chart API 下載日 K（2020-01-01 起，含回測暖身期）
3. 快取為 backtest/data/*.csv，支援中斷續傳
"""
import json
import csv
import os
import sys
import time
import datetime
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOKEN_FILE = os.path.join(BASE_DIR, "finmind_token.txt")
STOCK_LIST_FILE = os.path.join(BASE_DIR, "stock_list.csv")
FAILED_FILE = os.path.join(BASE_DIR, "failed_downloads.csv")

# 回測期間 2016-01-01 起；提前一年下載供 200MA / 動能漲幅暖身
DOWNLOAD_START = datetime.date(2015, 1, 1)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
REQUEST_INTERVAL = 0.4  # 秒，避免 Yahoo 限速


def http_get(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if e.code == 429:  # 被限速，加長等待
                time.sleep(10 * (attempt + 1))
            else:
                time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download failed after {retries} retries: {url}")


def fetch_stock_list():
    """FinMind TaiwanStockInfo → 過濾為上市櫃一般股票（4 碼、非 00 開頭）"""
    if os.path.exists(STOCK_LIST_FILE):
        with open(STOCK_LIST_FILE, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        print(f"[stock list] 使用既有清單 {len(rows)} 檔")
        return rows

    with open(TOKEN_FILE, encoding="utf-8") as f:
        token = f.read().strip()
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo&token={token}"
    data = json.loads(http_get(url))
    if data.get("status") != 200:
        raise RuntimeError(f"FinMind error: {data.get('msg')}")

    seen = {}
    for r in data["data"]:
        sid = r["stock_id"]
        typ = r.get("type", "")          # twse=上市, tpex=上櫃
        cat = r.get("industry_category", "")
        if typ not in ("twse", "tpex"):
            continue
        if len(sid) != 4 or not sid.isdigit():
            continue  # 排除權證、特別股、ETN 等
        if sid.startswith("00"):
            continue  # 排除 ETF
        if cat in ("ETF", "ETN", "Index", "大盤"):
            continue
        # 同一檔可能多筆（不同產業分類），保留一筆
        seen[sid] = {
            "stock_id": sid,
            "name": r.get("stock_name", ""),
            "type": typ,
            "industry": cat,
        }
    rows = sorted(seen.values(), key=lambda x: x["stock_id"])
    with open(STOCK_LIST_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["stock_id", "name", "type", "industry"])
        w.writeheader()
        w.writerows(rows)
    print(f"[stock list] 取得 {len(rows)} 檔上市櫃一般股票")
    return rows


def yahoo_symbol(stock_id, market_type):
    return f"{stock_id}.TW" if market_type == "twse" else f"{stock_id}.TWO"


def extract_splits(result_item):
    """從 Yahoo Finance API 回傳中取出拆股事件，回傳 {date_str: ratio}。"""
    splits = {}
    for ts_str, sp in (result_item.get("events", {}).get("splits") or {}).items():
        split_date = datetime.date.fromtimestamp(int(ts_str)).isoformat()
        splits[split_date] = round(sp["numerator"] / sp["denominator"], 6)
    return splits


def save_splits(splits, out_path):
    """存拆股資料；若無拆股則刪除舊檔。"""
    splits_path = out_path.replace(".csv", "_splits.json")
    if splits:
        with open(splits_path, "w", encoding="utf-8") as f:
            json.dump(splits, f)
    elif os.path.exists(splits_path):
        os.remove(splits_path)


def download_symbol(symbol, out_path):
    """下載單一標的日 K 並存 CSV；同時存拆股事件。回傳資料筆數，無資料回傳 0。"""
    p1 = int(time.mktime(DOWNLOAD_START.timetuple()))
    p2 = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={p1}&period2={p2}&interval=1d&events=div%2Csplit")
    try:
        raw = json.loads(http_get(url))
    except urllib.error.HTTPError:
        return 0
    result = raw.get("chart", {}).get("result")
    if not result or not result[0].get("timestamp"):
        return 0
    d = result[0]
    ts = d["timestamp"]
    q = d["indicators"]["quote"][0]
    adj_list = d["indicators"].get("adjclose", [{}])[0].get("adjclose")
    rows = []
    for i in range(len(ts)):
        c = q["close"][i]
        o = q["open"][i]
        if c is None or o is None:
            continue
        adj = adj_list[i] if adj_list and adj_list[i] is not None else c
        rows.append([
            datetime.date.fromtimestamp(ts[i]).isoformat(),
            round(o, 4), round(q["high"][i], 4), round(q["low"][i], 4),
            round(c, 4), round(adj, 6), q["volume"][i] or 0,
        ])
    if not rows:
        return 0
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "adj_close", "volume"])
        w.writerows(rows)
    save_splits(extract_splits(d), out_path)
    return len(rows)


def fetch_splits_only(symbol, out_path):
    """僅取得拆股事件，不重新下載 K 線。供 --splits-only 模式使用。"""
    p1 = int(time.mktime(DOWNLOAD_START.timetuple()))
    p2 = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={p1}&period2={p2}&interval=1d&events=split&range=1d")
    try:
        raw = json.loads(http_get(url))
    except Exception:
        return
    result = raw.get("chart", {}).get("result")
    if not result:
        return
    save_splits(extract_splits(result[0]), out_path)


def main():
    force = "--force" in sys.argv  # 覆寫既有檔案（變更下載起始日時使用）
    splits_only = "--splits-only" in sys.argv  # 僅更新拆股資料，不重下 K 線
    os.makedirs(DATA_DIR, exist_ok=True)

    # --splits-only：快速補齊所有個股及 ETF 的拆股事件
    if splits_only:
        print("【僅更新拆股資料模式】", flush=True)
        stocks = fetch_stock_list()
        etf_symbols = [("0050.TW", os.path.join(DATA_DIR, "0050.csv")),
                       ("00631L.TW", os.path.join(DATA_DIR, "00631L.csv"))]
        all_targets = [(yahoo_symbol(s["stock_id"], s["type"]),
                        os.path.join(DATA_DIR, f"{s['stock_id']}.csv"))
                       for s in stocks] + etf_symbols
        for idx, (sym, path) in enumerate(all_targets, 1):
            if not os.path.exists(path):
                continue
            try:
                fetch_splits_only(sym, path)
            except Exception:
                pass
            if idx % 200 == 0:
                print(f"進度 {idx}/{len(all_targets)}", flush=True)
            time.sleep(REQUEST_INTERVAL)
        print("拆股資料更新完成", flush=True)
        return

    # 大盤指數
    taiex_path = os.path.join(DATA_DIR, "TAIEX.csv")
    if force or not os.path.exists(taiex_path):
        n = download_symbol("^TWII", taiex_path)
        print(f"[TAIEX] ^TWII {n} 筆")
        if n == 0:
            sys.exit("TAIEX 下載失敗，中止")

    stocks = fetch_stock_list()
    failed = []
    done = skipped = empty = 0
    total = len(stocks)
    t0 = time.time()

    for idx, s in enumerate(stocks, 1):
        out_path = os.path.join(DATA_DIR, f"{s['stock_id']}.csv")
        if os.path.exists(out_path):
            if not force:
                skipped += 1
                continue
            # force 模式：檔案若已涵蓋新起始年（或標的上市較晚）仍可跳過 —
            # 以首筆日期是否落在舊起始日(2020)之前判斷是否為新版資料
            try:
                with open(out_path, encoding="utf-8") as f:
                    f.readline()
                    first_date = f.readline().split(",")[0]
                if first_date and first_date < "2020-01-01":
                    skipped += 1
                    continue
            except Exception:
                pass
        symbol = yahoo_symbol(s["stock_id"], s["type"])
        try:
            n = download_symbol(symbol, out_path)
        except Exception as e:
            failed.append([s["stock_id"], symbol, str(e)])
            time.sleep(REQUEST_INTERVAL)
            continue
        if n == 0:
            empty += 1
            failed.append([s["stock_id"], symbol, "no data"])
        else:
            done += 1
        if idx % 100 == 0:
            elapsed = time.time() - t0
            print(f"進度 {idx}/{total}  成功={done} 略過={skipped} 無資料={empty}  "
                  f"耗時 {elapsed/60:.1f} 分", flush=True)
        time.sleep(REQUEST_INTERVAL)

    if failed:
        with open(FAILED_FILE, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["stock_id", "symbol", "reason"])
            w.writerows(failed)

    print(f"完成。成功={done} 既有略過={skipped} 無資料/失敗={len(failed)}")


if __name__ == "__main__":
    main()
