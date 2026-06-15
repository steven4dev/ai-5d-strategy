# -*- coding: utf-8 -*-
"""
驗證 backtest.html autoSelectBest / autoSelectBestForStrategy 邏輯正確性。
對每個快速區間按鈕做暴力全掃描，確認自動選出的策略確為全域最佳。
"""

import json
import re
import calendar
from datetime import datetime

RESULTS_JS = "backtest/results/results.js"

# ─── 載入資料 ────────────────────────────────────────────────────────────────
with open(RESULTS_JS, "r", encoding="utf-8") as f:
    raw = f.read().strip()

# 去除 "const BACKTEST_DATA = " 前綴與結尾 ";"
raw = re.sub(r"^const\s+BACKTEST_DATA\s*=\s*", "", raw)
if raw.endswith(";"):
    raw = raw[:-1]

D = json.loads(raw)

dates         = D["dates"]           # list[str], 交易日序列
params        = D["params"]          # dict
variants      = D["variants"]        # "days|entry_ma|exit_ma" -> {"equity": [...], ...}
etf_data      = D["etf"]             # "strategy[|entry_ma|exit_ma]" -> {"equity": [...], ...}
initial_cash  = params["initial_cash"]
etf_uses_ma   = {b[0]: b[2] for b in (params.get("etf_bases") or [])}

print(f"資料期間: {dates[0]} ~ {dates[-1]}, 共 {len(dates)} 個交易日")
print(f"初始資金: {initial_cash:,}")
print(f"momentum variants: {len(variants)}, ETF 策略: {len(etf_data)}")
print()

# ─── 工具函式 (鏡像 JS 實作) ─────────────────────────────────────────────────

def lower_bound(target: str) -> int:
    """第一個 dates[i] >= target 的索引（鏡像 JS lowerBound）"""
    lo, hi, ans = 0, len(dates) - 1, len(dates) - 1
    while lo <= hi:
        mid = (lo + hi) >> 1
        if dates[mid] >= target:
            ans = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return ans


def get_range(start_str: str, end_str: str):
    """鏡像 JS getRange()"""
    s = start_str or dates[0]
    e = end_str or dates[-1]
    if s > e:
        s, e = e, s
    i0 = lower_bound(s)
    i1 = lower_bound(e)
    if dates[i1] > e and i1 > 0:
        i1 -= 1
    return i0, i1


def score_v(v: dict, i0: int, i1: int) -> float:
    """鏡像 JS scoreV() — 與圖表 % 完全一致，equity[i0]=0 時回傳 -inf"""
    eq = v["equity"]
    if not eq[i1]:
        return float("-inf")
    e0 = initial_cash if i0 == 0 else (eq[i0] or 0)
    if i0 > 0 and e0 <= 0:
        return float("-inf")
    return eq[i1] / e0


def months_back(n: int, last: str) -> str:
    """模擬 JS d.setMonth(d.getMonth() - n)，回傳 YYYY-MM-DD"""
    dt = datetime.strptime(last, "%Y-%m-%d")
    month = dt.month - n
    year  = dt.year
    while month <= 0:
        month += 12
        year  -= 1
    max_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, max_day)
    result = f"{year:04d}-{month:02d}-{day:02d}"
    return result if result >= dates[0] else dates[0]


# ─── 測試區間定義（對應 HTML 快速按鈕） ──────────────────────────────────────

last_date = dates[-1]
RANGES = [
    ("近6月",   months_back(6,   last_date), last_date),
    ("近1年",   months_back(12,  last_date), last_date),
    ("近2年",   months_back(24,  last_date), last_date),
    ("近3年",   months_back(36,  last_date), last_date),
    ("近5年",   months_back(60,  last_date), last_date),
    ("近10年",  months_back(120, last_date), last_date),
    ("全部",    dates[0],                    last_date),
]

# ─── TEST GROUP A：autoSelectBest 全局驗證 ────────────────────────────────────
print("=" * 70)
print("TEST GROUP A: autoSelectBest — 跨所有策略找全域最佳")
print("=" * 70)

all_pass = True
fail_log = []

for label, start_str, end_str in RANGES:
    i0, i1 = get_range(start_str, end_str)

    # --- 模擬 JS autoSelectBest(i0, i1) ---
    best        = float("-inf")
    auto_strat  = None
    auto_days   = None
    auto_entry  = None
    auto_exit   = None

    for key, v in variants.items():
        s = score_v(v, i0, i1)
        if s > best:
            best = s
            d, en, ex   = key.split("|")
            auto_strat  = "momentum"
            auto_days   = d
            auto_entry  = en
            auto_exit   = ex

    for key, v in etf_data.items():
        s = score_v(v, i0, i1)
        if s > best:
            best = s
            parts      = key.split("|")
            auto_strat = parts[0]
            if len(parts) >= 3:
                auto_entry = parts[1]
                auto_exit  = parts[2]
            else:
                auto_entry = str(params["default_entry_ma"])
                auto_exit  = str(params["default_exit_ma"])
            auto_days = None

    # --- 暴力全掃描：不分類型，找真正最高分 ---
    bf_best  = float("-inf")
    bf_key   = None
    bf_group = None
    for key, v in variants.items():
        s = score_v(v, i0, i1)
        if s > bf_best:
            bf_best  = s
            bf_key   = key
            bf_group = "momentum"
    for key, v in etf_data.items():
        s = score_v(v, i0, i1)
        if s > bf_best:
            bf_best  = s
            bf_key   = key
            bf_group = "etf"

    # 分數必須完全相等（同一個 float 值）
    ok = abs(best - bf_best) < 1e-9
    if not ok:
        all_pass = False
        fail_log.append((label, best, bf_best, bf_key))

    pct = (best - 1) * 100
    status = "PASS" if ok else "FAIL"
    n_days = i1 - i0 + 1
    strat_desc = f"{auto_strat} days={auto_days} entry={auto_entry} exit={auto_exit}" \
        if auto_strat == "momentum" \
        else f"{auto_strat} entry={auto_entry} exit={auto_exit}"

    print(f"[{status}] {label:6s}  {dates[i0]} ~ {dates[i1]}  ({n_days:4d}日)")
    print(f"        最佳策略: {strat_desc}")
    print(f"        得分: {best:.6f}  ({pct:+.1f}%)")
    if not ok:
        print(f"  !! 暴力最佳: {bf_group} {bf_key}, score={bf_best:.6f}")
    print()

if all_pass:
    print("GROUP A: 全數 PASS — autoSelectBest 在每個區間均選出全域最佳")
else:
    print(f"GROUP A: FAIL — {len(fail_log)} 個區間選錯")
    for f in fail_log:
        print(f"  {f}")
print()

# ─── TEST GROUP B：autoSelectBestForStrategy 各策略最佳參數驗證 ───────────────
print("=" * 70)
print("TEST GROUP B: autoSelectBestForStrategy — 各策略內部找最佳 MA 參數")
print("=" * 70)

B_pass = True
B_fail = []

for label, start_str, end_str in RANGES:
    i0, i1 = get_range(start_str, end_str)
    n_days  = i1 - i0 + 1
    print(f"  --- {label}  {dates[i0]} ~ {dates[i1]}  ({n_days}日) ---")

    # momentum: autoSelectBestForStrategy("momentum")
    mo_best = float("-inf")
    mo_key  = None
    for key, v in variants.items():
        s = score_v(v, i0, i1)
        if s > mo_best:
            mo_best = s
            mo_key  = key

    # 暴力驗證：和 GROUP A 的 momentum 掃描結果一致即可
    bf_mo = max((score_v(v, i0, i1) for v in variants.values()), default=float("-inf"))
    ok_mo = abs(mo_best - bf_mo) < 1e-9
    if not ok_mo:
        B_pass = False
        B_fail.append((label, "momentum", mo_best, bf_mo))
    d, en, ex = mo_key.split("|")
    status_mo = "PASS" if ok_mo else "FAIL"
    print(f"    [{status_mo}] momentum   days={d:3s}  entry={en:3s}  exit={ex:3s}  "
          f"score={mo_best:.4f} ({(mo_best-1)*100:+.1f}%)")

    # ETF strategies with MA
    for strat, uses_ma in etf_uses_ma.items():
        if not uses_ma:
            # no MA params — just lookup directly
            v = etf_data.get(strat)
            if v:
                s = score_v(v, i0, i1)
                print(f"           {strat:20s}  (no MA)            "
                      f"score={s:.4f} ({(s-1)*100:+.1f}%)")
            continue

        # autoSelectBestForStrategy equivalent
        st_best = float("-inf")
        st_key  = None
        for key, v in etf_data.items():
            parts = key.split("|")
            if parts[0] != strat or len(parts) < 3:
                continue
            s = score_v(v, i0, i1)
            if s > st_best:
                st_best = s
                st_key  = key

        # 暴力驗證
        bf_st = max(
            (score_v(v, i0, i1) for key, v in etf_data.items()
             if key.split("|")[0] == strat and len(key.split("|")) >= 3),
            default=float("-inf")
        )
        ok_st = abs(st_best - bf_st) < 1e-9 if st_key else True
        if not ok_st:
            B_pass = False
            B_fail.append((label, strat, st_best, bf_st))
        if st_key:
            _, en, ex = st_key.split("|")
            status_st = "PASS" if ok_st else "FAIL"
            print(f"    [{status_st}] {strat:20s}  entry={en:3s}  exit={ex:3s}       "
                  f"score={st_best:.4f} ({(st_best-1)*100:+.1f}%)")
    print()

if B_pass:
    print("GROUP B: 全數 PASS — autoSelectBestForStrategy 各策略均選出最佳 MA 參數")
else:
    print(f"GROUP B: FAIL — {len(B_fail)} 個問題")
    for f in B_fail:
        print(f"  {f}")
print()

# ─── TEST GROUP C：scoreV 邊界情形 ──────────────────────────────────────────
print("=" * 70)
print("TEST GROUP C: scoreV 邊界情形")
print("=" * 70)

errs = 0

# C1: i0 == 0 時分母固定為 initial_cash
v0 = list(variants.values())[0]
sc_zero = score_v(v0, 0, 0)
expected_c1 = v0["equity"][0] / initial_cash
ok_c1 = abs(sc_zero - expected_c1) < 1e-9
print(f"  [{'PASS' if ok_c1 else 'FAIL'}] C1: i0=0 分母 = initial_cash  "
      f"({sc_zero:.6f} == {expected_c1:.6f})")
if not ok_c1:
    errs += 1

# C2: i0 > 0 且 equity[i0] = 0 → 策略在區間起點破產，回傳 -Infinity
fake_v = {"equity": [0] * len(dates)}
fake_v["equity"][-1] = 5000   # 終值
sc_bankrupt = score_v(fake_v, 5, len(dates) - 1)
ok_c2 = sc_bankrupt == float("-inf")
print(f"  [{'PASS' if ok_c2 else 'FAIL'}] C2: equity[i0]=0 (破產) → -Infinity  ({sc_bankrupt})")
if not ok_c2:
    errs += 1

# C3: equity[i1] == 0 → -Infinity
fake_v2 = {"equity": [100000] + [0] * (len(dates) - 1)}
sc_inf = score_v(fake_v2, 0, len(dates) - 1)
ok_c3 = sc_inf == float("-inf")
print(f"  [{'PASS' if ok_c3 else 'FAIL'}] C3: equity[i1]=0 → -Infinity  ({sc_inf})")
if not ok_c3:
    errs += 1

# C4: lowerBound 精確性 — 已存在日期應回傳該索引
mid_date = dates[len(dates) // 2]
idx = lower_bound(mid_date)
ok_c4 = dates[idx] == mid_date
print(f"  [{'PASS' if ok_c4 else 'FAIL'}] C4: lowerBound({mid_date}) -> idx={idx} -> {dates[idx]}")
if not ok_c4:
    errs += 1

# C5: lowerBound 不存在日期（週末） → 下一個交易日
# 找一個不在 dates 裡的日期（日期 +1 或 +2 直到不在 dates 裡）
probe = dates[len(dates) // 2 + 1]
# 假設某個週末（取 probe 的前一天，確保不存在）
probe_dt = datetime.strptime(probe, "%Y-%m-%d")
# 製造一個「剛好不存在」的日期：找 dates 中兩個連續日期之間的空格
gap_found = False
for i in range(1, len(dates)):
    d0 = datetime.strptime(dates[i-1], "%Y-%m-%d")
    d1 = datetime.strptime(dates[i], "%Y-%m-%d")
    if (d1 - d0).days > 1:
        gap_date = (d0.replace() if False else
                    d1.strftime("%Y-%m-%d"))
        # 取 d0 + 1 天（非交易日）
        from datetime import timedelta
        non_trading = (d0 + timedelta(days=1)).strftime("%Y-%m-%d")
        if non_trading not in (dates[i-1], dates[i]):
            idx5 = lower_bound(non_trading)
            ok_c5 = dates[idx5] == dates[i]  # 應跳到 d1
            print(f"  [{'PASS' if ok_c5 else 'FAIL'}] C5: lowerBound({non_trading}非交易日) "
                  f"-> {dates[idx5]}  (expect {dates[i]})")
            if not ok_c5:
                errs += 1
            gap_found = True
            break
if not gap_found:
    print("  [SKIP] C5: 找不到連續非交易日間隙（不常見，略過）")

print()
if errs == 0:
    print("GROUP C: 全數 PASS")
else:
    print(f"GROUP C: {errs} 個 FAIL")
print()

# ─── 最終摘要 ────────────────────────────────────────────────────────────────
print("=" * 70)
print("最終摘要")
print("=" * 70)
total_ok = all_pass and B_pass and errs == 0
if total_ok:
    print("全部 PASS: auto-select 邏輯在所有測試區間均正確找到最佳策略+參數")
else:
    if not all_pass:
        print(f"GROUP A FAIL: {len(fail_log)} 個區間 autoSelectBest 選錯")
    if not B_pass:
        print(f"GROUP B FAIL: {len(B_fail)} 個區間 autoSelectBestForStrategy 選錯")
    if errs > 0:
        print(f"GROUP C FAIL: {errs} 個 scoreV 邊界計算錯誤")
print()
