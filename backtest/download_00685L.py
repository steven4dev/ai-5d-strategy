# -*- coding: utf-8 -*-
"""下載 00685L.TW（富邦台灣加權正2）歷史資料，儲存為 backtest/data/00685L.csv"""
import os, csv, datetime
import yfinance as yf

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "00685L.csv")

ticker = yf.Ticker("00685L.TW")
df = ticker.history(start="2016-12-01", auto_adjust=False)
df = df.sort_index()

if df.empty:
    print("ERROR: yfinance 未回傳資料，請確認 ticker 代號或網路連線")
    exit(1)

rows = 0
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["date", "open", "high", "low", "close", "adj_close", "volume"])
    for idx, row in df.iterrows():
        date_str = idx.strftime("%Y-%m-%d")
        w.writerow([
            date_str,
            round(float(row["Open"]), 6),
            round(float(row["High"]), 6),
            round(float(row["Low"]), 6),
            round(float(row["Close"]), 6),
            round(float(row["Adj Close"]), 6),
            int(row["Volume"]),
        ])
        rows += 1

print(f"已儲存 {rows} 筆資料 → {OUT}")
# 顯示首尾幾列供確認
import pandas as pd
df2 = pd.read_csv(OUT)
print(df2.head(5).to_string(index=False))
print("...")
print(df2.tail(5).to_string(index=False))
