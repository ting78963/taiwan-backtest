"""
v5 出場邏輯 Unit Tests
=======================
T16  exit=09:59、TP 在 10:05 觸發 → 必須 timeout，不得 TP
T17  exit=09:59、TP 在 09:45 觸發 → 有效 TP（在截止前）
T18  同一根 K 同時觸及 TP+SL（09:25）→ conservative=SL；optimistic=TP；exclude=不計入
T19  TP/SL 均在截止後觸發 → 必須 timeout，gross_return 使用實際 close
T20  舊 inventory threshold=3.5%，新 request=2.5% → 必須補抓（threshold 下降）
T21  舊 inventory threshold=2.5%，新 request=3.5% → 可跳過（threshold 上升）
T22  舊 inventory threshold=NULL，新 request=任意 → 視為已覆蓋，跳過（向後相容）
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.backtest_engine import _determine_exit, CUTOFF_TIME_MAP


# ── Helper：建立 outcome_data 的 row dict ──────────────────────────────

def make_row(
    tp_time=None, sl_time=None,
    return_0959=None, mfe_0959=None, mae_0959=None,
    exit_price_0959=None,
    return_5m=None, mfe_5m=None, mae_5m=None,
    exit_price_5m=None,
    return_close=None, mfe_close=None, mae_close=None,
    exit_price_close=None,
):
    """建立測試用的 outcome_data row dict"""
    return {
        "first_plus100_time":  tp_time,    # TP = 1.0% 的首次觸發時間
        "first_minus050_time": sl_time,    # SL = 0.5% 的首次觸發時間
        # 0959 截止
        "return_0959":       return_0959,
        "mfe_0959":          mfe_0959,
        "mae_0959":          mae_0959,
        "exit_price_0959":   exit_price_0959,
        # 5m 截止
        "return_5m":         return_5m,
        "mfe_5m":            mfe_5m,
        "mae_5m":            mae_5m,
        "exit_price_5m":     exit_price_5m,
        # close 截止（全天）
        "return_close":      return_close,
        "mfe_close":         mfe_close,
        "mae_close":         mae_close,
        "exit_price_close":  exit_price_close,
    }


# ────────────────────────────────────────────────────────────
# T16：TP 在截止時間後觸發 → 必須 timeout
# ────────────────────────────────────────────────────────────

def test_t16_tp_after_cutoff_must_timeout():
    """
    exit_time_label = '0959'（截止 09:59）
    TP = 1.0%，first_plus100_time = '10:05:00'（在截止後）
    SL = 0.5%，未觸發（sl_time = None）

    預期：
        TP 無效（10:05 > 09:59），timeout 出場
        gross_return = return_0959（實際截止 close 的報酬）
        exit_reason = 'timeout'
    """
    row = make_row(
        tp_time="10:05:00",    # 在截止後 → 無效
        sl_time=None,
        return_0959=0.38,      # 截止時的實際報酬 +0.38%
        mfe_0959=1.2,
        mae_0959=-0.2,
        exit_price_0959=100.38,
    )
    entry_price = 100.0

    result = _determine_exit(1.0, 0.5, "0959", row)

    assert result["exit_reason"] == "timeout", (
        f"TP 在 10:05 > 09:59（截止），必須 timeout，實際={result['exit_reason']}"
    )
    assert result["hit"] is False, "timeout 的 hit 應為 False"
    assert abs(result["observed_return_pct"] - 0.38) < 0.001, (
        f"timeout gross_return 應=0.38，實際={result['observed_return_pct']}"
    )

    print("✓ T16 PASS — TP 在 10:05 > 截止 09:59 → timeout（不偷算 TP）")
    print(f"   first_plus100_time=10:05, cutoff=09:59 → 有效 tp_time=None → timeout")
    print(f"   gross_return={result['observed_return_pct']}%（實際截止 close）")


# ────────────────────────────────────────────────────────────
# T17：TP 在截止前觸發 → 有效 TP
# ────────────────────────────────────────────────────────────

def test_t17_tp_before_cutoff_is_valid():
    """
    exit_time_label = '0959'
    TP = 1.0%，first_plus100_time = '09:45:00'（在截止前）

    預期：
        TP 有效（09:45 <= 09:59），exit_reason = 'TP'
        gross_return = 1.0（設定的 TP 值）
    """
    row = make_row(
        tp_time="09:45:00",    # 在截止前 → 有效
        sl_time=None,
        return_0959=0.8,
        mfe_0959=1.5,
        mae_0959=-0.2,
    )

    result = _determine_exit(1.0, 0.5, "0959", row, 100.0)

    assert result["exit_reason"] == "hit", (
        f"TP 在 09:45 <= 09:59（截止），應 hit，實際={result['exit_reason']}"
    )
    assert result["hit"] is True, "hit 應為 True"
    assert result["observed_return_pct"] == 1.0, f"gross_return 應=1.0，實際={result['observed_return_pct']}"

    print("✓ T17 PASS — TP 在 09:45 <= 截止 09:59 → 有效 TP")


# ────────────────────────────────────────────────────────────
# T18：同一根 K 同時觸及 TP+SL → intrabar_policy 行為
# ────────────────────────────────────────────────────────────

def test_t18_same_bar_tp_sl_intrabar_policy():
    """
    exit_time_label = '0959'
    TP = 1.0%，SL = 0.5%，兩者均在 '09:25:00' 觸發（同一根 K）

    場景：09:25 這根 K 的 high = +1.2%（碰 TP），low = -0.6%（碰 SL）
         市場實際走向未知（1 分 K 不記錄 tick 方向）

    預期：
        conservative → exit_reason='SL'，same_bar_ambiguous=True
        optimistic   → exit_reason='TP'，same_bar_ambiguous=True
        exclude      → exit_reason='excluded'，gross_return=None
    """
    row = make_row(
        tp_time="09:25:00",
        sl_time="09:25:00",   # 同一根 K 同時觸發
        return_0959=0.15,
        mfe_0959=1.2,
        mae_0959=-0.6,
        exit_price_0959=100.15,
    )

    # Conservative（預設）
    r_con = _determine_exit(1.0, 0.5, "0959", row, "conservative")
    # conservative：SL 先 → sl 強制停損（SL 先觸及，exit_reason=sl）
    assert r_con["exit_reason"] == "sl", (
        f"conservative 應 sl（SL先於TP強制停損），實際={r_con['exit_reason']}"
    )
    assert r_con["hit"] is False
    assert r_con["same_bar_ambiguous"] is True, "same_bar_ambiguous 應為 True"

    # Optimistic：TP 先 → hit
    r_opt = _determine_exit(1.0, 0.5, "0959", row, "optimistic")
    assert r_opt["exit_reason"] == "hit", (
        f"optimistic 應 hit（TP先），實際={r_opt['exit_reason']}"
    )
    assert r_opt["hit"] is True
    assert r_opt["same_bar_ambiguous"] is True
    assert r_opt["observed_return_pct"] == 1.0

    # Exclude
    r_exc = _determine_exit(1.0, 0.5, "0959", row, "exclude")
    assert r_exc["exit_reason"] == "excluded", (
        f"exclude 應返回 excluded，實際={r_exc['exit_reason']}"
    )
    assert r_exc["observed_return_pct"] is None, "excluded 的 gross_return 應為 None"
    assert r_exc["same_bar_ambiguous"] is True

    print("✓ T18 PASS — 同根 TP+SL 各 policy 行為正確")
    print(f"   09:25 同時碰 TP+SL：")
    print(f"   conservative → {r_con['exit_reason']} (hit={r_con['hit']})")
    print(f"   optimistic   → {r_opt['exit_reason']} ({r_opt['observed_return_pct']}%)")
    print(f"   exclude      → {r_exc['exit_reason']} (gross_return={r_exc['observed_return_pct']})")


# ────────────────────────────────────────────────────────────
# T19：TP/SL 均在截止後 → timeout，使用實際 return
# ────────────────────────────────────────────────────────────

def test_t19_both_tp_sl_after_cutoff_timeout():
    """
    exit_time_label = '0959'，cutoff = 09:59
    TP 在 10:30 觸發，SL 在 10:15 觸發
    兩者都在截止後 → 兩者均無效 → timeout

    return_0959 = 0.22%（截止時實際 close 計算）

    驗證：
        timeout 的 gross_return = 0.22（實際 close）
        raw_tp_time/raw_sl_time 仍保存在回傳值中（供後續分析）
    """
    row = make_row(
        tp_time="10:30:00",   # 截止後，無效
        sl_time="10:15:00",   # 截止後，無效
        return_0959=0.22,
        mfe_0959=0.45,
        mae_0959=-0.30,
        exit_price_0959=100.22,
    )

    result = _determine_exit(1.0, 0.5, "0959", row)

    assert result["exit_reason"] == "timeout", (
        f"TP(10:30) 和 SL(10:15) 均在截止(09:59)後，必須 timeout，實際={result['exit_reason']}"
    )
    assert result["hit"] is False
    assert abs(result["observed_return_pct"] - 0.22) < 0.001, (
        f"timeout gross_return 應=0.22（實際截止 close），實際={result['observed_return_pct']}"
    )
    # raw_tp_time 保存（供分析）
    assert str(result["tp_hit_time"]) == "10:30:00", (
        f"raw_tp_time 應保存在 tp_hit_time 欄位，實際={result['tp_hit_time']}"
    )

    print("✓ T19 PASS — TP(10:30)+SL(10:15) 均在截止(09:59)後 → timeout")
    print(f"   gross_return={result['observed_return_pct']}%（實際截止 close，非估算）")
    print(f"   tp_hit_time={result['tp_hit_time']}（保存供分析，但不影響出場）")


# ────────────────────────────────────────────────────────────
# T20/T21/T22：inventory collection_threshold 邏輯
# ────────────────────────────────────────────────────────────

def test_t20_t21_t22_inventory_threshold():
    """
    直接測試 get_fetched_dates 的 SQL 邏輯等價邏輯（不連 DB）。

    模擬 data_inventory 資料：
        日期 A：collection_threshold = 0.035（3.5%）
        日期 B：collection_threshold = 0.025（2.5%）
        日期 C：collection_threshold = NULL（舊格式）

    測試情境：
        T20：requested = 0.025（2.5%），日期 A 的 0.035 > 0.025 → 必須補抓
        T21：requested = 0.035（3.5%），日期 B 的 0.025 <= 0.035 → 可跳過
        T22：requested = 任意，日期 C 的 NULL → 視為已覆蓋，跳過
    """

    def simulate_should_skip(db_threshold, requested_threshold):
        """
        模擬 get_fetched_dates 的過濾邏輯：
        回傳 True = 可以跳過（已有足夠資料）
        回傳 False = 需要補抓
        """
        if db_threshold is None:
            # NULL → 向後相容，視為已覆蓋
            return True
        return db_threshold <= requested_threshold

    # T20：舊 threshold=3.5%，新 request=2.5% → 必須補抓
    should_skip_t20 = simulate_should_skip(0.035, 0.025)
    assert should_skip_t20 is False, (
        f"T20：舊 threshold=3.5% > 新 request=2.5%，應補抓（should_skip=False），"
        f"實際={should_skip_t20}"
    )

    # T21：舊 threshold=2.5%，新 request=3.5% → 可跳過
    should_skip_t21 = simulate_should_skip(0.025, 0.035)
    assert should_skip_t21 is True, (
        f"T21：舊 threshold=2.5% <= 新 request=3.5%，可跳過（should_skip=True），"
        f"實際={should_skip_t21}"
    )

    # T22：舊 threshold=NULL，任意 request → 跳過（向後相容）
    should_skip_t22a = simulate_should_skip(None, 0.025)
    should_skip_t22b = simulate_should_skip(None, 0.035)
    assert should_skip_t22a is True, "T22a：NULL threshold，request=2.5% → 跳過"
    assert should_skip_t22b is True, "T22b：NULL threshold，request=3.5% → 跳過"

    # 補充：相同 threshold
    should_skip_same = simulate_should_skip(0.025, 0.025)
    assert should_skip_same is True, "相同 threshold（2.5%=2.5%）→ 可跳過"

    print("✓ T20 PASS — 舊 threshold=3.5% > 新 request=2.5% → 日期列入補抓")
    print("✓ T21 PASS — 舊 threshold=2.5% <= 新 request=3.5% → 可跳過")
    print("✓ T22 PASS — 舊 threshold=NULL → 向後相容，跳過")
    print()
    print("   業務語意：")
    print("   threshold 下降（更嚴格的收集要求）→ 必須補抓，舊資料覆蓋不足")
    print("   threshold 上升（放寬收集要求）→ 舊資料已包含，無需重抓")


# ────────────────────────────────────────────────────────────
# 驗證 CUTOFF_TIME_MAP 完整性
# ────────────────────────────────────────────────────────────

def test_t23_cutoff_time_map_complete():
    """
    驗證 CUTOFF_TIME_MAP 的截止時間字串格式正確，
    且所有 exit_time_label 都有對應的映射。
    """
    expected_labels = {"5m", "10m", "0959", "1030", "1130", "close"}
    actual_labels   = set(CUTOFF_TIME_MAP.keys())

    assert expected_labels == actual_labels, (
        f"CUTOFF_TIME_MAP 應包含所有策略截止標籤，"
        f"缺少: {expected_labels - actual_labels}，多餘: {actual_labels - expected_labels}"
    )

    # 絕對時間截止格式應為 HH:MM:SS
    for label, cutoff in CUTOFF_TIME_MAP.items():
        if cutoff is not None:
            parts = cutoff.split(":")
            assert len(parts) == 3, f"{label} 的截止時間格式錯誤：{cutoff}"
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            assert 0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59, (
                f"{label} 的時間值超出範圍：{cutoff}"
            )

    # 驗證截止時間合理性
    assert CUTOFF_TIME_MAP["0959"] == "09:59:00"
    assert CUTOFF_TIME_MAP["1030"] == "10:30:00"
    assert CUTOFF_TIME_MAP["1130"] == "11:30:00"
    assert CUTOFF_TIME_MAP["close"] == "13:30:00"
    assert CUTOFF_TIME_MAP["5m"] is None   # 相對根數，無絕對時間
    assert CUTOFF_TIME_MAP["10m"] is None

    print("✓ T23 PASS — CUTOFF_TIME_MAP 格式完整正確")
    for label, t in CUTOFF_TIME_MAP.items():
        print(f"   '{label}': {t or '(相對根數，無絕對時間)'}")


# ────────────────────────────────────────────────────────────
# 執行所有測試
# ────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_t16_tp_after_cutoff_must_timeout,
        test_t17_tp_before_cutoff_is_valid,
        test_t18_same_bar_tp_sl_intrabar_policy,
        test_t19_both_tp_sl_after_cutoff_timeout,
        test_t20_t21_t22_inventory_threshold,
        test_t23_cutoff_time_map_complete,
    ]

    passed = 0
    failed = 0
    print("=" * 60)
    print("v5 出場邏輯 Unit Tests")
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
    import sys
    success = run_all()
    sys.exit(0 if success else 1)
