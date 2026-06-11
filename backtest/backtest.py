# -*- coding: utf-8 -*-
"""
AI-5D 強勢動能與大盤均線輪動回測系統（依 SRS 規格）

進場：TAIEX 連續 3 日收盤 > 200MA → 次日開盤全額買入過去 N 日漲幅第一名（平手比累計成交量）
出場：TAIEX 連續 3 日收盤 < 20MA → 次日開盤清倉
持有期間死抱，不換股。漲跌停鎖死順延至下一交易日。
成本：手續費 0.1425% × 2.8 折（最低 1 元，買賣皆收）、賣出證交稅 0.3%。

動能回看天數 N 一次回測多組（MOMENTUM_OPTIONS），供網頁端切換。
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

BACKTEST_START = datetime.date(2021, 1, 1)
INITIAL_CASH = 100_000.0
FEE_RATE = 0.001425 * 0.28   # 手續費 0.1425% × 2.8 折
MIN_FEE = 1.0                # 最低手續費 1 元
TAX_RATE = 0.003             # 證交稅（賣出）
MOMENTUM_OPTIONS = [5, 10, 20, 30, 60, 90, 120]  # 動能回看天數選項
DEFAULT_MOMENTUM = 30        # SRS 預設值（報告檔以此輸出）
LIMIT_PCT = 0.10             # 一般股票漲跌幅 10%

Bar = namedtuple("Bar", "date open high low close adj_close volume")


def load_csv(path):
    bars = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            bars.append(Bar(
                datetime.date.fromisoformat(r["date"]),
                float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r["adj_close"]), int(float(r["volume"])),
            ))
    return bars


def trade_fee(amount):
    return max(MIN_FEE, amount * FEE_RATE)


def simulate(momentum_days, taiex_dates, closes, buy_signal, sell_signal,
             start_i, stocks, stock_meta):
    """執行單一動能天數的完整回測，回傳 (trades, equity, open_pos, final_equity)。"""
    n = len(taiex_dates)

    def momentum_rank(sig_date):
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
            key = (ret, vol_sum)
            if best is None or key > best[0]:
                best = (key, sid)
        return best[1] if best else None

    def limit_locked(sid, date, direction):
        """開盤即鎖死一字（漲停買不到 / 跌停賣不掉）"""
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
        today = taiex_dates[i]

        # === 開盤：執行前一日確定的動作 ===
        if state == "PENDING_BUY" and pending_sid:
            s = stocks[pending_sid]
            k = s["by_date"].get(today)
            if k is not None and not limit_locked(pending_sid, today, "up"):
                price = s["bars"][k].open
                shares = int(cash / (price * (1 + FEE_RATE)))
                while shares > 0 and shares * price + trade_fee(shares * price) > cash:
                    shares -= 1
                if shares > 0:
                    amount = shares * price
                    fee = trade_fee(amount)
                    cash -= amount + fee
                    hold_sid, hold_shares = pending_sid, shares
                    hold_cost = amount + fee
                    entry_info = {"date": today, "price": price}
                    state = "HOLDING"
                    pending_sid = None
        elif state == "PENDING_SELL" and hold_sid:
            s = stocks[hold_sid]
            k = s["by_date"].get(today)
            if k is not None and not limit_locked(hold_sid, today, "down"):
                price = s["bars"][k].open
                amount = hold_shares * price
                fee = trade_fee(amount)
                tax = amount * TAX_RATE
                cash += amount - fee - tax
                meta = stock_meta.get(hold_sid, {})
                trades.append({
                    "stock_id": hold_sid,
                    "name": meta.get("name", ""),
                    "buy_date": entry_info["date"].isoformat(),
                    "buy_price": round(entry_info["price"], 2),
                    "shares": hold_shares,
                    "buy_cost": round(hold_cost, 2),
                    "sell_date": today.isoformat(),
                    "sell_price": round(price, 2),
                    "sell_net": round(amount - fee - tax, 2),
                    "pnl": round(amount - fee - tax - hold_cost, 2),
                    "ret_pct": round((amount - fee - tax) / hold_cost * 100 - 100, 2),
                })
                hold_sid, hold_shares, hold_cost = None, 0, 0.0
                entry_info = None
                state = "EMPTY"

        # === 收盤：判定訊號，決定次日動作 ===
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

        # === 每日權益（持股以當日收盤估值）===
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
        open_pos = {
            "stock_id": hold_sid, "name": meta.get("name", ""),
            "buy_date": entry_info["date"].isoformat(),
            "buy_price": round(entry_info["price"], 2),
            "shares": hold_shares,
            "last_close": round(last_val / hold_shares, 2) if hold_shares else 0,
            "unrealized_pnl": round(last_val - hold_cost, 2),
        }
    return trades, equity, open_pos


def main():
    os.makedirs(RESULT_DIR, exist_ok=True)

    # ---------- 大盤：均線與連續三日訊號 ----------
    taiex = load_csv(os.path.join(DATA_DIR, "TAIEX.csv"))
    closes = [b.close for b in taiex]
    n = len(taiex)
    ma200 = [None] * n
    ma20 = [None] * n
    for i in range(n):
        if i >= 199:
            ma200[i] = sum(closes[i - 199:i + 1]) / 200
        if i >= 19:
            ma20[i] = sum(closes[i - 19:i + 1]) / 20
    buy_signal = [False] * n
    sell_signal = [False] * n
    for i in range(2, n):
        if all(ma200[j] is not None and closes[j] > ma200[j] for j in (i - 2, i - 1, i)):
            buy_signal[i] = True
        if all(ma20[j] is not None and closes[j] < ma20[j] for j in (i - 2, i - 1, i)):
            sell_signal[i] = True

    trading_dates = [b.date for b in taiex]
    start_i = next(i for i, d in enumerate(trading_dates) if d >= BACKTEST_START)

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
    print(f"載入個股 {len(stocks)} 檔、大盤 {n} 個交易日")

    # ---------- 各動能天數回測 ----------
    eq_dates = [d.isoformat() for d in trading_dates[start_i:]]
    taiex_base = closes[start_i]
    taiex_curve = [round(closes[i] / taiex_base * INITIAL_CASH, 2) for i in range(start_i, n)]
    years = (trading_dates[-1] - trading_dates[start_i]).days / 365.25

    variants = {}
    for md in MOMENTUM_OPTIONS:
        trades, equity, open_pos = simulate(
            md, trading_dates, closes, buy_signal, sell_signal,
            start_i, stocks, stock_meta)
        variants[str(md)] = {
            "equity": equity, "trades": trades, "open_position": open_pos,
        }
        final_eq = equity[-1]
        wins = [t for t in trades if t["pnl"] > 0]
        print(f"  動能 {md:>3} 日：期末 {final_eq:>12,.0f} 元 "
              f"({final_eq / INITIAL_CASH * 100 - 100:+7.1f}%)  "
              f"交易 {len(trades):>3} 次  勝率 "
              f"{len(wins) / len(trades) * 100 if trades else 0:.0f}%", flush=True)

    # ---------- 預設天數的文字報告與 CSV ----------
    dv = variants[str(DEFAULT_MOMENTUM)]
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
        f"動能回看: {DEFAULT_MOMENTUM} 個交易日（預設）",
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
            "fee": "0.1425% x 2.8折 (低消1元)", "tax": "0.3%",
        },
        "dates": eq_dates,
        "taiex": taiex_curve,
        "variants": variants,
    }
    with open(os.path.join(RESULT_DIR, "results.js"), "w", encoding="utf-8") as f:
        f.write("const BACKTEST_DATA = ")
        f.write(json.dumps(payload, ensure_ascii=False))
        f.write(";\n")
    print("results.js 已更新（含 %d 組動能天數）" % len(MOMENTUM_OPTIONS))


if __name__ == "__main__":
    main()
