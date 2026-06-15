# -*- coding: utf-8 -*-
"""
AI-5D 強勢動能與大盤均線輪動回測系統（依 SRS 規格，參數化版）

進場：TAIEX 連續 3 日收盤 > 進場MA → 次日開盤全額買入過去 N 日漲幅第一名（平手比累計成交量）
出場：TAIEX 連續 3 日收盤 < 出場MA → 次日開盤清倉
持有期間死抱，不換股。漲跌停鎖死順延至下一交易日。
成本：手續費 0.1425% × 2.8 折（最低 1 元，買賣皆收）、賣出證交稅 0.3%（ETF 0.1%）。

可調參數（全部組合預先回測，供網頁端切換）：
  動能回看天數 N：MOMENTUM_OPTIONS
  進場均線：ENTRY_MA_OPTIONS／出場均線：EXIT_MA_OPTIONS
ETF 擇時策略亦套用同一組進出場均線。
"""
import csv
import json
import os
import datetime
from collections import namedtuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STOCK_LIST_FILE = os.path.join(BASE_DIR, "stock_list.csv")
RESULT_DIR = os.path.join(BASE_DIR, "results")

BACKTEST_START = datetime.date(2006, 1, 1)
INITIAL_CASH = 100_000.0
FEE_RATE = 0.001425 * 0.28   # 手續費 0.1425% × 2.8 折
MIN_FEE = 1.0                # 最低手續費 1 元
TAX_RATE = 0.003             # 證交稅（賣出，一般股票）
ETF_TAX_RATE = 0.001         # 證交稅（賣出，ETF）
MOMENTUM_OPTIONS = [5, 10, 20, 30, 60, 90, 120]
DEFAULT_MOMENTUM = 30
ENTRY_MA_OPTIONS = [60, 120, 200]      # 進場均線（站上）
EXIT_MA_OPTIONS = [10, 20, 60, 200]    # 出場均線（跌破）
DEFAULT_ENTRY_MA = 200
DEFAULT_EXIT_MA = 20                   # SRS 預設 200/20
LIMIT_PCT = 0.10

Bar = namedtuple("Bar", "date open high low close adj_close volume split_factor")


def load_csv(path):
    bars = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            bars.append(Bar(
                datetime.date.fromisoformat(r["date"]),
                float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r["adj_close"]), int(float(r["volume"])),
                1.0,  # split_factor placeholder
            ))

    # 從 _splits.json 載入拆股事件，計算每根K棒的累計拆股倍率
    # split_factor = 從該日期之後到最新日期，所有拆股比率的乘積
    # 顯示用的真實歷史價 = bars[k].open * bars[k].split_factor
    splits_path = path.replace(".csv", "_splits.json")
    if os.path.exists(splits_path):
        with open(splits_path, encoding="utf-8") as f:
            splits_raw = json.load(f)  # {date_str: ratio}
        if splits_raw:
            sorted_splits = sorted(splits_raw.items(), reverse=True)  # 最新在前
            ptr, cum = 0, 1.0
            new_bars = []
            for bar in reversed(bars):
                date_str = bar.date.isoformat()
                while ptr < len(sorted_splits) and sorted_splits[ptr][0] > date_str:
                    cum *= sorted_splits[ptr][1]
                    ptr += 1
                new_bars.append(bar._replace(split_factor=cum))
            bars = list(reversed(new_bars))
    return bars


def build_synthetic_lev_bars(taiex_bars, leverage=2.0, expense_annual=0.015, initial_nav=20.0):
    """
    以大盤日報酬 × leverage 合成槓桿 ETF K棒序列，取代 Yahoo Finance 的 00631L 資料。
    Yahoo Finance 對 00631L 歷史資料有嚴重拆股問題（2016 年顯示 0.78 NTD，實際約 20 NTD），
    直接使用會使回測高估約 10-15 倍。改以 TAIEX 日報酬 × 2 - 年費率（1.5%）合成。
    每日淨值：nav_t = nav_{t-1} × (1 + leverage × r_taiex - daily_cost)
    """
    daily_cost = expense_annual / 252.0
    bars = []
    nav = initial_nav
    for i, b in enumerate(taiex_bars):
        if i > 0 and taiex_bars[i - 1].close > 0:
            daily_ret = (b.close / taiex_bars[i - 1].close - 1) * leverage - daily_cost
            nav = max(nav * (1 + daily_ret), 0.001)
        open_nav = initial_nav if i == 0 else bars[i - 1].close  # 次日開盤 = 前日收盤NAV
        bars.append(Bar(b.date,
                        round(open_nav, 6), round(nav, 6), round(nav, 6),
                        round(nav, 6), round(nav, 6),  # close = adj_close = nav
                        0, 1.0))
    return bars


def trade_fee(amount):
    return max(MIN_FEE, amount * FEE_RATE)


def simulate(momentum_days, trading_dates, start_i, buy_signal, sell_signal,
             stocks, stock_meta, rank_cache):
    """個股動能策略完整回測。rank_cache 讓相同 (日期, 回看天數) 的選股結果跨參數組共用。"""
    n = len(trading_dates)

    def momentum_rank(sig_date):
        key = (sig_date, momentum_days)
        if key in rank_cache:
            return rank_cache[key]
        best = None
        for sid, s in stocks.items():
            k = s["by_date"].get(sig_date)
            if k is None or k < momentum_days:
                continue
            bars = s["bars"]
            p_now = bars[k].adj_close
            p_then = bars[k - momentum_days].adj_close
            if p_then <= 0 or bars[k].volume <= 0:
                continue
            ret = p_now / p_then - 1
            vol_sum = sum(bars[j].volume for j in range(k - momentum_days + 1, k + 1))
            cand = (ret, vol_sum)
            if best is None or cand > best[0]:
                best = (cand, sid)
        result = best[1] if best else None
        rank_cache[key] = result
        return result

    def limit_locked(sid, date, direction):
        s = stocks[sid]
        k = s["by_date"].get(date)
        if k is None or k == 0:
            return False
        b, prev = s["bars"][k], s["bars"][k - 1]
        if direction == "up":
            return b.open >= prev.close * (1 + LIMIT_PCT) * 0.995 and b.high == b.low
        return b.open <= prev.close * (1 - LIMIT_PCT) * 1.005 and b.high == b.low

    cash = INITIAL_CASH
    state = "EMPTY"
    pending_sid = None
    hold_sid, hold_shares, hold_cost = None, 0, 0.0
    trades, equity = [], []
    entry_info = None

    for i in range(start_i, n):
        today = trading_dates[i]

        if state == "PENDING_BUY" and pending_sid:
            s = stocks[pending_sid]
            k = s["by_date"].get(today)
            if k is not None and not limit_locked(pending_sid, today, "up"):
                price = s["bars"][k].open
                # 顯示用的真實歷史開盤價（含後續拆股還原）
                display_buy = round(price * s["bars"][k].split_factor, 2)
                shares = int(cash / (price * (1 + FEE_RATE)))
                while shares > 0 and shares * price + trade_fee(shares * price) > cash:
                    shares -= 1
                if shares > 0:
                    amount = shares * price
                    fee = trade_fee(amount)
                    cash -= amount + fee
                    hold_sid, hold_shares = pending_sid, shares
                    hold_cost = amount + fee
                    entry_info = {"date": today, "price": price, "display_price": display_buy}
                    state = "HOLDING"
                    pending_sid = None
        elif state == "PENDING_SELL" and hold_sid:
            s = stocks[hold_sid]
            k = s["by_date"].get(today)
            if k is not None and not limit_locked(hold_sid, today, "down"):
                price = s["bars"][k].open
                display_sell = round(price * s["bars"][k].split_factor, 2)
                amount = hold_shares * price
                fee = trade_fee(amount)
                tax = amount * TAX_RATE
                cash += amount - fee - tax
                meta = stock_meta.get(hold_sid, {})
                trades.append({
                    "stock_id": hold_sid,
                    "name": meta.get("name", ""),
                    "buy_date": entry_info["date"].isoformat(),
                    "buy_price": entry_info["display_price"],
                    "shares": hold_shares,
                    "buy_cost": round(hold_cost, 2),
                    "sell_date": today.isoformat(),
                    "sell_price": display_sell,
                    "sell_net": round(amount - fee - tax, 2),
                    "pnl": round(amount - fee - tax - hold_cost, 2),
                    "ret_pct": round((amount - fee - tax) / hold_cost * 100 - 100, 2),
                })
                hold_sid, hold_shares, hold_cost = None, 0, 0.0
                entry_info = None
                state = "EMPTY"

        if state == "EMPTY" and buy_signal[i]:
            pick = momentum_rank(today)
            if pick:
                pending_sid = pick
                state = "PENDING_BUY"
        elif state == "PENDING_BUY" and sell_signal[i]:
            pending_sid = None
            state = "EMPTY"
        elif state == "HOLDING" and sell_signal[i]:
            state = "PENDING_SELL"

        eq = cash
        if hold_shares and hold_sid:
            s = stocks[hold_sid]
            k = s["by_date"].get(today)
            if k is not None:
                eq += hold_shares * s["bars"][k].close
            else:
                prev_bars = [b for b in s["bars"] if b.date <= today]
                if prev_bars:
                    eq += hold_shares * prev_bars[-1].close
        equity.append(round(eq, 2))

    open_pos = None
    if hold_sid:
        meta = stock_meta.get(hold_sid, {})
        last_val = equity[-1] - cash
        unreal_pnl = round(last_val - hold_cost, 2)
        open_pos = {
            "stock_id": hold_sid, "name": meta.get("name", ""),
            "buy_date": entry_info["date"].isoformat(),
            "buy_price": entry_info["display_price"],
            "shares": hold_shares,
            "buy_cost": round(hold_cost, 2),
            "last_close": round(last_val / hold_shares, 2) if hold_shares else 0,
            "unrealized_pnl": unreal_pnl,
            "unrealized_ret_pct": round(unreal_pnl / hold_cost * 100, 2) if hold_cost else 0,
        }
    return trades, equity, open_pos


def simulate_etf(etf_id, etf_name, bars, trading_dates, start_i,
                 enter_sig, exit_sig, always_hold=False):
    """ETF 擇時回測：進出場以大盤訊號決定，次日開盤成交，使用還原價含息。"""
    n = len(trading_dates)
    by_date = {b.date: k for k, b in enumerate(bars)}

    # 偵測 ETF 拆股事件：adj_close 單日暴跌 ≥50% 視為前向股票拆分，記錄拆分倍率。
    # Yahoo Finance 對台灣 ETF（包含 0050、00631L）的 adj_close 均未做 backward split 還原：
    #   - 00631L：adj_close ≈ close（無除息），拆股後 adj_close 隨 close 等比下跌
    #   - 0050：adj_close = close × dividend_factor，dividend_factor 在拆股前後不變，
    #           代表 Yahoo 只調整除息、未調整拆股，adj_close 同樣隨 close 等比下跌
    # 因此兩者都需要在拆股日手動將持股數乘以拆分倍率，才能得到正確的累計報酬。
    # 驗證：0050 理論總報酬（價格 12.41x × 除息 1.606x）= 19.93x；
    #       套用補正後模擬值 = 19.95x ✓；不補正則僅 4.98x（低估 4 倍）。
    etf_splits = {}
    for i in range(1, len(bars)):
        pa, ca = bars[i-1].adj_close, bars[i].adj_close
        if pa > 0 and ca > 0 and ca / pa < 0.5:
            etf_splits[bars[i].date] = pa / ca  # 拆分倍率（例：00631L≈21.9，0050≈4.0）

    def adj_open(k):
        b = bars[k]
        o = b.open or b.close  # 開盤價為 0（Yahoo 缺漏）時改用收盤價
        return o * (b.adj_close / b.close) if b.close else o

    cash = INITIAL_CASH
    shares = 0
    state = "EMPTY"
    pending = "BUY" if always_hold else None
    trades, equity = [], []
    entry = None
    last_adj_close = None

    for i in range(start_i, n):
        today = trading_dates[i]
        k = by_date.get(today)

        # 拆股調整在成交前套用：ex-date 開盤即為除權後價格，持股數須先乘以倍率
        if k is not None and state == "HOLDING" and bars[k].date in etf_splits:
            shares = int(shares * etf_splits[bars[k].date])

        if k is not None and pending:
            if pending == "BUY" and state == "EMPTY":
                price = adj_open(k)          # 還原價，用於 P&L 計算
                display_buy = round(bars[k].open * bars[k].split_factor, 2)  # 真實市場開盤價
                qty = int(cash / (price * (1 + FEE_RATE)))
                while qty > 0 and qty * price + trade_fee(qty * price) > cash:
                    qty -= 1
                if qty > 0:
                    amount = qty * price
                    fee = trade_fee(amount)
                    cash -= amount + fee
                    shares = qty
                    state = "HOLDING"
                    entry = {"date": today, "price": price, "display_price": display_buy,
                             "cost": amount + fee}
                pending = None
            elif pending == "SELL" and state == "HOLDING":
                price = adj_open(k)
                display_sell = round(bars[k].open * bars[k].split_factor, 2)
                amount = shares * price
                fee = trade_fee(amount)
                tax = amount * ETF_TAX_RATE
                cash += amount - fee - tax
                trades.append({
                    "stock_id": etf_id, "name": etf_name,
                    "buy_date": entry["date"].isoformat(),
                    "buy_price": entry["display_price"],
                    "shares": shares,
                    "buy_cost": round(entry["cost"], 2),
                    "sell_date": today.isoformat(),
                    "sell_price": display_sell,
                    "sell_net": round(amount - fee - tax, 2),
                    "pnl": round(amount - fee - tax - entry["cost"], 2),
                    "ret_pct": round((amount - fee - tax) / entry["cost"] * 100 - 100, 2),
                })
                shares, state, entry, pending = 0, "EMPTY", None, None

        if not always_hold:
            if state == "EMPTY" and enter_sig[i]:
                pending = "BUY"
            elif state == "EMPTY" and pending == "BUY" and exit_sig[i]:
                pending = None
            elif state == "HOLDING" and exit_sig[i]:
                pending = "SELL"

        if k is not None:
            last_adj_close = bars[k].adj_close
        eq = cash + (shares * last_adj_close if shares and last_adj_close else 0)
        equity.append(round(eq, 2))

    open_pos = None
    if shares and entry:
        unreal_pnl = round(shares * last_adj_close - entry["cost"], 2)
        open_pos = {
            "stock_id": etf_id, "name": etf_name,
            "buy_date": entry["date"].isoformat(),
            "buy_price": entry["display_price"],
            "shares": shares,
            "buy_cost": round(entry["cost"], 2),
            "last_close": round(last_adj_close, 2),
            "unrealized_pnl": unreal_pnl,
            "unrealized_ret_pct": round(unreal_pnl / entry["cost"] * 100, 2) if entry["cost"] else 0,
        }
    return trades, equity, open_pos


def main():
    os.makedirs(RESULT_DIR, exist_ok=True)

    # ---------- 大盤與均線 ----------
    taiex = load_csv(os.path.join(DATA_DIR, "TAIEX.csv"))
    closes = [b.close for b in taiex]
    n = len(taiex)
    trading_dates = [b.date for b in taiex]
    start_i = next(i for i, d in enumerate(trading_dates) if d >= BACKTEST_START)

    ma = {}
    for w in sorted(set(ENTRY_MA_OPTIONS) | set(EXIT_MA_OPTIONS)):
        arr = [None] * n
        s = 0.0
        for i in range(n):
            s += closes[i]
            if i >= w:
                s -= closes[i - w]
            if i >= w - 1:
                arr[i] = s / w
        ma[w] = arr

    def build_signals(entry_w, exit_w):
        me, mx = ma[entry_w], ma[exit_w]
        buy = [False] * n
        sell = [False] * n
        for i in range(2, n):
            if all(me[j] is not None and closes[j] > me[j] for j in (i - 2, i - 1, i)):
                buy[i] = True
            if all(mx[j] is not None and closes[j] < mx[j] for j in (i - 2, i - 1, i)):
                sell[i] = True
        return buy, sell

    # ---------- 個股資料 ----------
    with open(STOCK_LIST_FILE, encoding="utf-8-sig") as f:
        stock_meta = {r["stock_id"]: r for r in csv.DictReader(f)}
    stocks = {}
    for sid in stock_meta:
        path = os.path.join(DATA_DIR, f"{sid}.csv")
        if not os.path.exists(path):
            continue
        bars = load_csv(path)
        if len(bars) < 10:
            continue
        stocks[sid] = {"bars": bars, "by_date": {b.date: k for k, b in enumerate(bars)}}
    print(f"載入個股 {len(stocks)} 檔、大盤 {n} 個交易日", flush=True)

    # ---------- 參數網格回測 ----------
    eq_dates = [d.isoformat() for d in trading_dates[start_i:]]
    taiex_base = closes[start_i]
    taiex_curve = [round(closes[i] / taiex_base * INITIAL_CASH, 2) for i in range(start_i, n)]
    years = (trading_dates[-1] - trading_dates[start_i]).days / 365.25

    etf_bars = {}
    for etf_id, fname in [("0050", "0050.csv"), ("00631L", "00631L.csv")]:
        path = os.path.join(DATA_DIR, fname)
        if os.path.exists(path):
            etf_bars[etf_id] = load_csv(path)

    variants = {}
    etf_strategies = {}
    rank_cache = {}

    # 買進持有（與均線參數無關）
    dummy = [False] * n
    for hold_id, hold_label, hold_key in [("0050", "0050 買進持有", "0050_hold"),
                                           ("00631L", "台灣50正2 買進持有", "00631L_hold")]:
        if hold_id not in etf_bars:
            continue
        trades, equity, op = simulate_etf(hold_id, hold_label,
                                          etf_bars[hold_id], trading_dates, start_i,
                                          dummy, dummy, always_hold=True)
        etf_strategies[hold_key] = {"label": hold_label,
                                    "equity": [int(round(v)) for v in equity],
                                    "trades": trades, "open_position": op}
        print(f"  {hold_label}：期末 {equity[-1]:>12,.0f} 元", flush=True)

    for e_ma in ENTRY_MA_OPTIONS:
        for x_ma in EXIT_MA_OPTIONS:
            buy_sig, sell_sig = build_signals(e_ma, x_ma)
            for md in MOMENTUM_OPTIONS:
                trades, equity, op = simulate(md, trading_dates, start_i,
                                              buy_sig, sell_sig, stocks, stock_meta, rank_cache)
                variants[f"{md}|{e_ma}|{x_ma}"] = {
                    "equity": [int(round(v)) for v in equity],
                    "trades": trades, "open_position": op,
                }
            for etf_id, label in [("0050", "0050 大盤均線擇時"),
                                  ("00631L", "台灣50正2 大盤均線擇時")]:
                if etf_id not in etf_bars:
                    continue
                trades, equity, op = simulate_etf(etf_id, label, etf_bars[etf_id],
                                                  trading_dates, start_i, buy_sig, sell_sig)
                etf_strategies[f"{etf_id}_timing|{e_ma}|{x_ma}"] = {
                    "label": label,
                    "equity": [int(round(v)) for v in equity],
                    "trades": trades, "open_position": op,
                }
            mom30 = variants[f"{DEFAULT_MOMENTUM}|{e_ma}|{x_ma}"]["equity"][-1]
            lev = etf_strategies.get(f"00631L_timing|{e_ma}|{x_ma}", {"equity": [0]})["equity"][-1]
            print(f"  MA {e_ma:>3}/{x_ma:>3}：動能30日 期末 {mom30:>11,.0f}"
                  f"｜正2擇時 期末 {lev:>11,.0f}", flush=True)

    # ---------- 預設參數的文字報告與 CSV ----------
    dkey = f"{DEFAULT_MOMENTUM}|{DEFAULT_ENTRY_MA}|{DEFAULT_EXIT_MA}"
    dv = variants[dkey]
    trades, equity = dv["trades"], dv["equity"]
    final_equity = equity[-1]
    peak, max_dd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)
    cagr = (final_equity / INITIAL_CASH) ** (1 / years) - 1 if years > 0 else 0
    wins = [t for t in trades if t["pnl"] > 0]

    with open(os.path.join(RESULT_DIR, "trades.csv"), "w", newline="", encoding="utf-8-sig") as f:
        if trades:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
    with open(os.path.join(RESULT_DIR, "equity_curve.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity"])
        for d, e in zip(eq_dates, equity):
            w.writerow([d, e])

    lines = [
        "AI-5D 強勢動能與大盤均線輪動回測結果",
        "=" * 50,
        f"回測期間: {eq_dates[0]} ~ {eq_dates[-1]} ({years:.1f} 年)",
        f"參數: 動能 {DEFAULT_MOMENTUM} 日｜進場 {DEFAULT_ENTRY_MA}MA／出場 {DEFAULT_EXIT_MA}MA（SRS 預設）",
        f"初始資金: {INITIAL_CASH:,.0f} 元",
        f"期末權益: {final_equity:,.0f} 元",
        f"總報酬率: {final_equity / INITIAL_CASH * 100 - 100:+.2f}%",
        f"年化報酬率 (CAGR): {cagr * 100:+.2f}%",
        f"最大回撤: {max_dd * 100:.2f}%",
        f"完成交易次數: {len(trades)}",
        f"勝率: {len(wins)}/{len(trades)}"
        + (f" ({len(wins) / len(trades) * 100:.0f}%)" if trades else ""),
    ]
    with open(os.path.join(RESULT_DIR, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # ---------- 網頁用資料 results.js ----------
    payload = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "params": {
            "start": eq_dates[0], "end": eq_dates[-1],
            "initial_cash": INITIAL_CASH,
            "stock_count": len(stocks),
            "momentum_options": MOMENTUM_OPTIONS,
            "default_momentum": DEFAULT_MOMENTUM,
            "entry_ma_options": ENTRY_MA_OPTIONS,
            "exit_ma_options": EXIT_MA_OPTIONS,
            "default_entry_ma": DEFAULT_ENTRY_MA,
            "default_exit_ma": DEFAULT_EXIT_MA,
            "etf_bases": [["0050_hold", "0050 買進持有", False],
                          ["0050_timing", "0050 大盤均線擇時", True],
                          ["00631L_hold", "台灣50正2 買進持有", False],
                          ["00631L_timing", "台灣50正2 大盤均線擇時", True]],
            "fee": "0.1425% x 2.8折 (低消1元)", "tax": "0.3% / ETF 0.1%",
        },
        "dates": eq_dates,
        "taiex": taiex_curve,
        "variants": variants,
        "etf": etf_strategies,
    }
    out = os.path.join(RESULT_DIR, "results.js")
    with open(out, "w", encoding="utf-8") as f:
        f.write("const BACKTEST_DATA = ")
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        f.write(";\n")
    size_mb = os.path.getsize(out) / 1048576
    print(f"results.js 已更新：{len(variants)} 組動能參數＋{len(etf_strategies)} 組 ETF，{size_mb:.1f} MB")


if __name__ == "__main__":
    main()
