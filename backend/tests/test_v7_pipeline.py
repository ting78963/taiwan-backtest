"""
v7 Pipeline Tests
=================
T29  compute_outcome → row dict → _determine_exit 完整資料流
     不直接建 dict，而是從 compute_outcome 的輸出走完整路徑
T30  TP 在第 6 根觸發：5m 截止 timeout，10m 截止 hit（完整路徑）
T31  新增 TP levels（2.5%/3%/4%/5%）在 compute_outcome 正確記錄
T32  新增 SL levels（1.25%/1.5%/2%/2.5%/3%）在 compute_outcome 正確記錄
T33  trigger 不在 ENTRY_MODES 和 run_backtest 的 entry_modes 中
T34  generate_summary 包含 sample_count、trading_day_count、hit_rate_pct
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from events.outcome_engine import compute_outcome, TP_LEVELS, SL_LEVELS, ENTRY_MODES, _tp_key, _sl_key
from backtest.backtest_engine import _determine_exit, DEFAULT_ENTRY_MODES


def make_bar(t, o, h, l, c, v):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}

def make_df(bars):
    df = pd.DataFrame(bars)
    df["time_str"] = df["time"].astype(str)
    return df


# ────────────────────────────────────────────────────────────
# T29：compute_outcome → _determine_exit 完整資料流
# ────────────────────────────────────────────────────────────

def test_t29_full_pipeline_compute_to_exit():
    """
    從 compute_outcome 的輸出直接傳入 _determine_exit。
    驗證 within 旗標的鍵名格式在兩個函數之間完全一致。

    場景：
        entry_price = 100.0
        bars: 第 3 根 high = 101.5（TP 1.0% 在第 3 根觸發）
              第 5 根 low  = 99.3（SL 0.75% 在第 5 根觸發）
              第 7 根 close = 100.8

    exit=5m：TP 在第 3 根（within_5m=True）→ hit
    exit=10m：TP 在第 3 根（within_10m=True）→ hit
    """
    entry_price = 100.0
    bars_after = make_df([
        make_bar("09:16:00", 100.0, 100.5, 99.8, 100.2, 100),  # bar 1
        make_bar("09:17:00", 100.2, 100.8, 99.9, 100.5, 80),   # bar 2
        make_bar("09:18:00", 100.5, 101.5, 100.3, 101.2, 200), # bar 3，TP 1.0% 觸發
        make_bar("09:19:00", 101.2, 101.3, 100.8, 100.9, 90),  # bar 4
        make_bar("09:20:00", 100.9, 101.0,  99.2, 100.0, 150), # bar 5，SL 0.75% 觸發（low=-0.8%）
        make_bar("09:21:00", 100.0, 100.5, 99.8, 100.3, 70),   # bar 6
        make_bar("09:22:00", 100.3, 100.8, 100.0, 100.8, 60),  # bar 7（5m exit price）
    ])

    outcome = compute_outcome(bars_after, entry_price)

    # 確認 TP 1.0% 在第 3 根觸發
    tp_key = _tp_key(1.0)  # "100"
    assert outcome[f"first_plus{tp_key}_time"] == "09:18:00", (
        f"TP 1.0% 應在 09:18 觸發（bar3），實際={outcome[f'first_plus{tp_key}_time']}"
    )
    assert outcome[f"tp{tp_key}_within_5m"]  is True,  "TP 1.0% 在第3根，within_5m 應 True"
    assert outcome[f"tp{tp_key}_within_10m"] is True,  "TP 1.0% 在第3根，within_10m 應 True"

    # 確認 SL 0.75% 在第 5 根觸發
    sl_key = _sl_key(0.75)  # "075"
    assert outcome[f"first_minus{sl_key}_time"] == "09:20:00", (
        f"SL 0.75% 應在 09:20 觸發（bar5），實際={outcome[f'first_minus{sl_key}_time']}"
    )
    assert outcome[f"sl{sl_key}_within_5m"]  is True,  "SL 0.75% 在第5根，within_5m 應 True"
    assert outcome[f"sl{sl_key}_within_10m"] is True,  "SL 0.75% 在第5根，within_10m 應 True"

    # 把 outcome 傳入 _determine_exit（exit=5m，TP=1.0%，SL=0.75%）
    # TP 在 bar3，SL 在 bar5，TP 先 → hit
    result_5m = _determine_exit(1.0, 0.75, "5m", outcome)
    assert result_5m["exit_reason"] == "hit", (
        f"TP 在 bar3 < bar5（SL），exit=5m 應 hit，實際={result_5m['exit_reason']}"
    )
    assert result_5m["hit"] is True
    assert result_5m["observed_return_pct"] == 1.0

    # exit=10m 也應 hit
    result_10m = _determine_exit(1.0, 0.75, "10m", outcome)
    assert result_10m["exit_reason"] == "hit", f"exit=10m 應 hit，實際={result_10m['exit_reason']}"

    print("✓ T29 PASS — compute_outcome → _determine_exit 完整資料流正確")
    print(f"   TP 1.0% @ bar3 ({outcome[f'first_plus{tp_key}_time']})，"
          f"SL 0.75% @ bar5 ({outcome[f'first_minus{sl_key}_time']})")
    print(f"   exit=5m: {result_5m['exit_reason']} (gross={result_5m['observed_return_pct']}%)")
    print(f"   exit=10m: {result_10m['exit_reason']}")


# ────────────────────────────────────────────────────────────
# T30：TP 在第 6 根，5m=timeout，10m=hit（完整路徑）
# ────────────────────────────────────────────────────────────

def test_t30_tp_bar6_full_pipeline():
    """
    TP 1.0% 在進場後第 6 根才觸發。

    compute_outcome：
        tp100_within_5m  = False（bar6 > 5）
        tp100_within_10m = True（bar6 <= 10）

    _determine_exit（exit=5m）：tp_time = None（within_5m=False）→ timeout
    _determine_exit（exit=10m）：tp_time = bar6 time → hit
    """
    entry_price = 100.0
    bars_after = make_df([
        make_bar("09:16:00", 100.0, 100.3, 99.9, 100.1, 50),   # bar 1
        make_bar("09:17:00", 100.1, 100.4, 100.0, 100.2, 60),  # bar 2
        make_bar("09:18:00", 100.2, 100.5, 100.1, 100.3, 55),  # bar 3
        make_bar("09:19:00", 100.3, 100.6, 100.2, 100.4, 65),  # bar 4
        make_bar("09:20:00", 100.4, 100.8, 100.3, 100.5, 70),  # bar 5（5m exit）
        make_bar("09:21:00", 100.5, 101.5, 100.4, 101.2, 200), # bar 6，TP 1.0% 觸發
        make_bar("09:22:00", 101.2, 101.8, 101.0, 101.5, 80),  # bar 7
    ])

    outcome = compute_outcome(bars_after, entry_price)

    tp_key = _tp_key(1.0)
    assert outcome[f"tp{tp_key}_within_5m"]  is False, "bar6 > 5，within_5m 應 False"
    assert outcome[f"tp{tp_key}_within_10m"] is True,  "bar6 <= 10，within_10m 應 True"
    assert outcome[f"first_plus{tp_key}_time"] == "09:21:00"

    result_5m  = _determine_exit(1.0, None, "5m",  outcome)
    result_10m = _determine_exit(1.0, None, "10m", outcome)

    assert result_5m["exit_reason"]  == "timeout", (
        f"TP 在 bar6，exit=5m 應 timeout，實際={result_5m['exit_reason']}"
    )
    assert result_10m["exit_reason"] == "hit", (
        f"TP 在 bar6，exit=10m 應 hit，實際={result_10m['exit_reason']}"
    )

    # 5m 的 gross_return 應使用截止時的實際 close（bar5=100.5）
    ep5 = outcome.get("exit_price_5m")
    r5  = outcome.get("return_5m")
    assert ep5 is not None and abs(ep5 - 100.5) < 0.01, f"exit_price_5m 應=100.5，實際={ep5}"
    assert abs(result_5m["observed_return_pct"] - float(r5 or 0)) < 0.001

    print("✓ T30 PASS — TP bar6：5m=timeout，10m=hit（完整路徑）")
    print(f"   TP 1.0% @ bar6，within_5m={outcome[f'tp{tp_key}_within_5m']}，within_10m={outcome[f'tp{tp_key}_within_10m']}")
    print(f"   exit=5m:  {result_5m['exit_reason']} (gross={result_5m['observed_return_pct']:.4f}%，exit_price={ep5})")
    print(f"   exit=10m: {result_10m['exit_reason']}")


# ────────────────────────────────────────────────────────────
# T31：新增 TP levels 正確記錄
# ────────────────────────────────────────────────────────────

def test_t31_new_tp_levels():
    """
    驗證 TP_LEVELS 包含 2.5%/3.0%/4.0%/5.0%，
    且 compute_outcome 對這些 level 正確記錄首次觸及時間。
    """
    assert 2.50 in TP_LEVELS, "TP_LEVELS 應包含 2.5%"
    assert 3.00 in TP_LEVELS, "TP_LEVELS 應包含 3.0%"
    assert 4.00 in TP_LEVELS, "TP_LEVELS 應包含 4.0%"
    assert 5.00 in TP_LEVELS, "TP_LEVELS 應包含 5.0%"

    entry_price = 100.0
    bars_after = make_df([
        make_bar("09:16:00", 100.0, 102.6, 99.9, 102.5, 500),  # bar1，high=+2.6%，觸 2.5%
        make_bar("09:17:00", 102.5, 103.2, 102.0, 103.0, 300), # bar2，觸 3.0%
        make_bar("09:18:00", 103.0, 104.2, 102.8, 104.0, 200), # bar3，觸 4.0%
        make_bar("09:19:00", 104.0, 105.5, 103.8, 105.2, 150), # bar4，觸 5.0%
        make_bar("09:20:00", 105.2, 105.4, 104.8, 105.0,  80), # bar5（5m exit）
    ])

    outcome = compute_outcome(bars_after, entry_price)

    for tp, expected_bar_time in [
        (2.5, "09:16:00"),
        (3.0, "09:17:00"),
        (4.0, "09:18:00"),
        (5.0, "09:19:00"),
    ]:
        k = _tp_key(tp)
        actual_time = outcome.get(f"first_plus{k}_time")
        assert actual_time == expected_bar_time, (
            f"TP {tp}%: 應在 {expected_bar_time} 觸發，實際={actual_time}"
        )
        assert outcome.get(f"tp{k}_within_5m") is True, f"TP {tp}% 在 bar1~4，within_5m 應 True"

    print("✓ T31 PASS — 新增 TP levels（2.5%/3%/4%/5%）正確記錄")
    for tp in [2.5, 3.0, 4.0, 5.0]:
        k = _tp_key(tp)
        print(f"   TP {tp}%: first_hit={outcome[f'first_plus{k}_time']}, within_5m={outcome[f'tp{k}_within_5m']}")


# ────────────────────────────────────────────────────────────
# T32：新增 SL levels 正確記錄
# ────────────────────────────────────────────────────────────

def test_t32_new_sl_levels():
    """
    驗證 SL_LEVELS 包含 1.25%/1.5%/2.0%/2.5%/3.0%，
    且 compute_outcome 正確記錄。
    """
    assert 1.25 in SL_LEVELS, "SL_LEVELS 應包含 1.25%"
    assert 1.50 in SL_LEVELS, "SL_LEVELS 應包含 1.50%"
    assert 2.00 in SL_LEVELS, "SL_LEVELS 應包含 2.0%"
    assert 2.50 in SL_LEVELS, "SL_LEVELS 應包含 2.5%"
    assert 3.00 in SL_LEVELS, "SL_LEVELS 應包含 3.0%"

    entry_price = 100.0
    bars_after = make_df([
        make_bar("09:16:00", 100.0, 100.2, 98.6, 98.8, 200),  # bar1，low=-1.4%，觸 0.25/0.5/0.75/1.0/1.25%
        make_bar("09:17:00",  98.8, 99.0, 98.4, 98.5, 150),   # bar2，low=-1.6%，觸 1.5%
        make_bar("09:18:00",  98.5, 98.8, 97.9, 98.0, 120),   # bar3，low=-2.1%，觸 2.0%
        make_bar("09:19:00",  98.0, 98.3, 97.4, 97.5,  90),   # bar4，low=-2.6%，觸 2.5%
        make_bar("09:20:00",  97.5, 97.8, 96.9, 97.0,  80),   # bar5，low=-3.1%，觸 3.0%
    ])

    outcome = compute_outcome(bars_after, entry_price)

    for sl, expected_bar in [
        (0.25, "09:16:00"),
        (1.25, "09:16:00"),  # bar1 low = -1.4% <= -1.25%
        (1.50, "09:17:00"),  # bar2 low = -1.6% <= -1.5%
        (2.00, "09:18:00"),
        (2.50, "09:19:00"),
        (3.00, "09:20:00"),
    ]:
        k = _sl_key(sl)
        actual_time = outcome.get(f"first_minus{k}_time")
        assert actual_time == expected_bar, (
            f"SL {sl}%: 應在 {expected_bar} 觸發，實際={actual_time}"
        )

    print("✓ T32 PASS — 新增 SL levels（1.25%/1.5%/2%/2.5%/3%）正確記錄")
    for sl in [0.25, 1.25, 1.50, 2.00, 2.50, 3.00]:
        k = _sl_key(sl)
        print(f"   SL {sl}%: first_hit={outcome[f'first_minus{k}_time']}")


# ────────────────────────────────────────────────────────────
# T33：trigger 不在 ENTRY_MODES 和 DEFAULT_ENTRY_MODES
# ────────────────────────────────────────────────────────────

def test_t33_trigger_removed():
    """
    'trigger' 不應出現在任何 entry mode 清單中。
    1 分 K 無法重建 trigger 瞬間的成交價，已移除。
    """
    assert "trigger" not in ENTRY_MODES, (
        f"outcome_engine.ENTRY_MODES 不應含 trigger，實際={ENTRY_MODES}"
    )
    assert "trigger" not in DEFAULT_ENTRY_MODES, (
        f"backtest_engine.DEFAULT_ENTRY_MODES 不應含 trigger，實際={DEFAULT_ENTRY_MODES}"
    )
    assert "bar_close"  in ENTRY_MODES
    assert "next_open"  in ENTRY_MODES
    assert "next_close" in ENTRY_MODES

    print("✓ T33 PASS — trigger 已從所有 entry mode 清單移除")
    print(f"   ENTRY_MODES = {ENTRY_MODES}")
    print(f"   DEFAULT_ENTRY_MODES = {DEFAULT_ENTRY_MODES}")


# ────────────────────────────────────────────────────────────
# T34：generate_summary 欄位驗證（不連 DB，模擬 SQL 輸出）
# ────────────────────────────────────────────────────────────

def test_t34_summary_columns():
    """
    驗證 generate_summary 查詢的輸出欄位包含必要的達成率統計。
    不連 DB，直接驗證 backtest_engine 的 SQL 邏輯。
    """
    # 模擬 generate_summary 的輸出欄位
    expected_cols = {
        "sample_count",
        "trading_day_count",
        "avg_signals_per_day",
        "hits",
        "hit_rate_pct",
        "avg_mfe",
        "avg_mae",
        "avg_return",
        "tp_pct",
        "exit_time_limit",
        "attack_number",
        "entry_mode",
    }

    # 讀取 generate_summary 的 cols 定義
    from backtest.backtest_engine import generate_summary
    import inspect
    src = inspect.getsource(generate_summary)

    # 確認這些欄位名稱都出現在 SQL 中
    for col in expected_cols:
        assert col in src, f"generate_summary 缺少欄位：{col}"

    print("✓ T34 PASS — generate_summary 包含所有必要統計欄位")
    for col in sorted(expected_cols):
        print(f"   ✓ {col}")


# ────────────────────────────────────────────────────────────
# 執行
# ────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_t29_full_pipeline_compute_to_exit,
        test_t30_tp_bar6_full_pipeline,
        test_t31_new_tp_levels,
        test_t32_new_sl_levels,
        test_t33_trigger_removed,
        test_t34_summary_columns,
    ]

    passed = 0
    failed = 0
    print("=" * 60)
    print("v7 Pipeline Tests")
    print("=" * 60)
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            print(f"✗ FAIL — {test_fn.__name__}")
            print(f"  {e}")
            failed += 1
        except Exception as e:
            print(f"✗ ERROR — {test_fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("=" * 60)
    print(f"結果：{passed} PASS / {failed} FAIL")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
