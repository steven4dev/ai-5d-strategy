# -*- coding: utf-8 -*-
"""
AI-5D 強勢動能與大盤均線輪動回測系統（依 SRS 規格）

進場：TAIEX 連續 3 日收盤 > 200MA → 次日開盤全額買入 30 日漲幅第一名（平手比累計成交量）
出場：TAIEX 連續 3 日收盤 < 20MA → 次日開盤清倉
持有期間死抱，不換股。漲跌停鎖死順延至下一交易日。
成本：手續費 0.1425% × 2.8 折（最低 1 元，買賣皆收）、賣出證交稅 0.3%。
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
MOMENTUM_DAYS = 30           # 動能回看期（交易日）
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


def buy_fee(amount):
    return max(MIN_FEE, amount * FEE_RATE)


def sell_fee(amount):
    return max(MIN_FEE, amount * FEE_RATE)


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

    # buy_signal[i]: 第 i 日(含)往前連續 3 日收盤 > 200MA
    # sell_signal[i]: 連續 3 日收盤 < 20MA
    buy_signal = [False] * n
    sell_signal = [False] * n
    for i in range(2, n):
        if all(ma200[j] is not None and closes[j] > ma200[j] for j in (i - 2, i - 1, i)):
            buy_signal[i] = True
        if all(ma20[j] is not None and closes[j] < ma20[j] for j in (i - 2, i - 1, i)):
            sell_signal[i] = True

    date_index = {b.date: i for i, b in enumerate(taiex)}
    trading_dates = [b.date for b in taiex]

    # ---------- 個股資料 ----------
    with open(STOCK_LIST_FILE, encoding="utf-8-sig") as f:
        stock_meta = {r["stock_id"]: r for r in csv.DictReader(f)}

    stocks = {}   # stock_id -> {date: Bar}, 以及序列
    for sid in stock_meta:
        path = os.path.join(DATA_DIR, f"{sid}.csv")
        if not os.path.exists(path):
            continue
        bars = load_csv(path)
        if len(bars) < MOMENTUM_DAYS + 5:
            continue
        stocks[sid] = {
            "bars": bars,
            "by_date": {b.date: k for k, b in enumerate(bars)},
        }
    print(f"載入個股 {len(stocks)} 檔、大盤 {n} 個交易日")

    def momentum_rank(sig_date):
        """回傳 (stock_id, 30日漲幅, 30日累計量) 排序最佳者；使用還原價計算漲幅"""
        best = None
        for sid, s in stocks.items():
            k = s["by_date"].get(sig_date)
            if k is None or k < MOMENTUM_DAYS:
                continue
            bars = s["bars"]
            p_now = bars[k].adj_close
            p_then = bars[k - MOMENTUM_DAYS].adj_close
            if p_then <= 0:
                continue
            # 排除訊號日沒有成交的殭屍股
            if bars[k].volume <= 0:
                continue
            ret = p_now / p_then - 1
            vol_sum = sum(bars[j].volume for j in range(k - MOMENTUM_DAYS + 1, k + 1))
            key = (ret, vol_sum)
            if best is None or key > best[0]:
                best = (key, sid)
        if best is None:
            return None
        (ret, vol_sum), sid = best
        return sid, ret, vol_sum

    def limit_up_locked(sid, date):
        """開盤即漲停鎖死（開盤=漲停價且全日一字）→ 買不到"""
        s = stocks[sid]
        k = s["by_date"].get(date)
        if k is None or k == 0:
            return False
        b, prev = s["bars"][k], s["bars"][k - 1]
        limit = prev.close * (1 + LIMIT_PCT)
        return b.open >= limit * 0.995 and b.high == b.low

    def limit_down_locked(sid, date):
        s = stocks[sid]
        k = s["by_date"].get(date)
        if k is None or k == 0:
            return False
        b, prev = s["bars"][k], s["bars"][k - 1]
        limit = prev.close * (1 - LIMIT_PCT)
        return b.open <= limit * 1.005 and b.high == b.low

    # ---------- 主回測迴圈 ----------
    cash = INITIAL_CASH
    state = "EMPTY"            # EMPTY / PENDING_BUY / HOLDING / PENDING_SELL
    pending_sid = None         # 待買標的
    hold_sid = None
    hold_shares = 0
    hold_cost = 0.0            # 買入總成本（含手續費）
    trades = []                # 交易紀錄
    equity_curve = []          # (date, equity)
    entry_info = None

    start_i = next(i for i, d in enumerate(trading_dates) if d >= BACKTEST_START)

    for i in range(start_i, n):
        today = trading_dates[i]

        # === 開盤：執行前一日確定的動作 ===
        if state == "PENDING_BUY" and pending_sid:
            s = stocks[pending_sid]
            k = s["by_date"].get(today)
            if k is not None:
                if limit_up_locked(pending_sid, today):
                    pass  # 漲停鎖死，順延
                else:
                    price = s["bars"][k].open
                    shares = int(cash / (price * (1 + FEE_RATE)))
                    # 最低手續費 1 元的精確修正
                    while shares > 0 and shares * price + buy_fee(shares * price) > cash:
                        shares -= 1
                    if shares > 0:
                        amount = shares * price
                        fee = buy_fee(amount)
                        cash -= amount + fee
                        hold_sid, hold_shares = pending_sid, shares
                        hold_cost = amount + fee
                        entry_info = {
                            "date": today, "price": price, "shares": shares,
                            "amount": amount, "fee": fee,
                        }
                        state = "HOLDING"
                        pending_sid = None
        elif state == "PENDING_SELL" and hold_sid:
            s = stocks[hold_sid]
            k = s["by_date"].get(today)
            if k is not None:
                if limit_down_locked(hold_sid, today):
                    pass  # 跌停鎖死，順延
                else:
                    price = s["bars"][k].open
                    amount = hold_shares * price
                    fee = sell_fee(amount)
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
                pending_sid = pick[0]
                state = "PENDING_BUY"
        elif state == "PENDING_BUY" and sell_signal[i]:
            # 尚未成交即遇大盤轉空 → 放棄買進
            pending_sid = None
            state = "EMPTY"
        elif state == "HOLDING" and sell_signal[i]:
            state = "PENDING_SELL"

        # === 每日權益曲線（持股以當日收盤估值）===
        equity = cash
        if hold_shares and hold_sid:
            s = stocks[hold_sid]
            k = s["by_date"].get(today)
            if k is not None:
                equity += hold_shares * s["bars"][k].close
            else:  # 當日無資料，用最近一筆收盤
                prev_bars = [b for b in s["bars"] if b.date <= today]
                if prev_bars:
                    equity += hold_shares * prev_bars[-1].close
        equity_curve.append((today, equity))

    # ---------- 期末若仍持有，以最後收盤估值（不強制平倉）----------
    final_equity = equity_curve[-1][1]

    # ---------- 績效統計 ----------
    eq = [e for _, e in equity_curve]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)
    years = (equity_curve[-1][0] - equity_curve[0][0]).days / 365.25
    total_ret = final_equity / INITIAL_CASH - 1
    cagr = (final_equity / INITIAL_CASH) ** (1 / years) - 1 if years > 0 else 0
    wins = [t for t in trades if t["pnl"] > 0]

    # ---------- 輸出 ----------
    with open(os.path.join(RESULT_DIR, "trades.csv"), "w", newline="", encoding="utf-8-sig") as f:
        if trades:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
    with open(os.path.join(RESULT_DIR, "equity_curve.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity"])
        for d, e in equity_curve:
            w.writerow([d.isoformat(), round(e, 2)])

    lines = [
        "AI-5D 強勢動能與大盤均線輪動回測結果",
        "=" * 50,
        f"回測期間: {equity_curve[0][0]} ~ {equity_curve[-1][0]} ({years:.1f} 年)",
        f"初始資金: {INITIAL_CASH:,.0f} 元",
        f"期末權益: {final_equity:,.0f} 元",
        f"總報酬率: {total_ret * 100:+.2f}%",
        f"年化報酬率 (CAGR): {cagr * 100:+.2f}%",
        f"最大回撤: {max_dd * 100:.2f}%",
        f"完成交易次數: {len(trades)}",
        f"勝率: {len(wins)}/{len(trades)}"
        + (f" ({len(wins) / len(trades) * 100:.0f}%)" if trades else ""),
        f"期末狀態: {'持有 ' + hold_sid + ' ' + str(hold_shares) + ' 股' if hold_sid else '空手'}",
        "",
        "各筆交易:",
    ]
    for t in trades:
        lines.append(
            f"  {t['buy_date']} 買 {t['stock_id']} {t['name']} {t['shares']}股 @{t['buy_price']}"
            f" → {t['sell_date']} 賣 @{t['sell_price']}  損益 {t['pnl']:+,.0f} ({t['ret_pct']:+.1f}%)"
        )
    if hold_sid:
        meta = stock_meta.get(hold_sid, {})
        lines.append(
            f"  {entry_info['date'].isoformat()} 買 {hold_sid} {meta.get('name','')} "
            f"{hold_shares}股 @{entry_info['price']:.2f}（仍持有中）"
        )
    report = "\n".join(lines)
    with open(os.path.join(RESULT_DIR, "report.txt"), "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)

    # ---------- 網頁用資料 results.js ----------
    eq_dates = [d.isoformat() for d, _ in equity_curve]
    # 大盤同期淨值（以回測起點正規化為初始資金）
    taiex_base = closes[start_i]
    taiex_curve = [round(closes[i] / taiex_base * INITIAL_CASH, 2) for i in range(start_i, n)]
    open_pos = None
    if hold_sid:
        meta = stock_meta.get(hold_sid, {})
        open_pos = {
            "stock_id": hold_sid, "name": meta.get("name", ""),
            "buy_date": entry_info["date"].isoformat(),
            "buy_price": round(entry_info["price"], 2),
            "shares": hold_shares,
            "last_close": round((final_equity - cash) / hold_shares, 2) if hold_shares else 0,
            "unrealized_pnl": round(final_equity - cash - hold_cost, 2),
        }
    payload = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "params": {
            "start": eq_dates[0], "end": eq_dates[-1],
            "initial_cash": INITIAL_CASH,
            "stock_count": len(stocks),
            "fee": "0.1425% x 2.8折 (低消1元)", "tax": "0.3%",
        },
        "summary": {
            "final_equity": round(final_equity, 2),
            "total_return_pct": round(total_ret * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "trade_count": len(trades),
            "win_count": len(wins),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "taiex_return_pct": round((taiex_curve[-1] / INITIAL_CASH - 1) * 100, 2),
        },
        "dates": eq_dates,
        "equity": [round(e, 2) for _, e in equity_curve],
        "taiex": taiex_curve,
        "trades": trades,
        "open_position": open_pos,
    }
    with open(os.path.join(RESULT_DIR, "results.js"), "w", encoding="utf-8") as f:
        f.write("const BACKTEST_DATA = ")
        f.write(json.dumps(payload, ensure_ascii=False))
        f.write(";\n")


if __name__ == "__main__":
    main()
