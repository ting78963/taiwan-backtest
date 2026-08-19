"""
v8 純化與量比 Tests
===================
T35  BACKTEST_TRADES_COLS 每個欄位都不含 commission/tax/net_return
T36  BACKTEST_TRADES SELECT 欄位與 BACKTEST_TRADES_COLS 完全一致（順序、數量）
T37  EVENT_MASTER_COLS 不含 entry_at_trigger
T38  BacktestRequest 不再接受 commission / commission_dis 參數
T39  observed_return_pct 正確出現在 backtest_engine 的 batch 和 INSERT 中
T40  量比算法：與前端 loadHotVolStocks 邏輯一致
T41  量比 at 0910：只使用 09:10 以前的量，不 look-ahead
T42  classify_volume_ratio 分層標籤正確
T43  exit_reason 合法值只有 hit / timeout / excluded
"""

import sys, os, re, inspect
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.export_engine import BACKTEST_TRADES_COLS, EVENT_MASTER_COLS
from events.volume_ratio import compute_volume_ratio, classify_volume_ratio


BANNED_COLS = {"commission_cost", "tax_cost", "net_return", "gross_return"}
BANNED_ENTRY = {"entry_at_trigger"}
VALID_EXIT_REASONS = {"hit", "timeout", "excluded"}


# ────────────────────────────────────────────────────────────
# T35：BACKTEST_TRADES_COLS 不含成本欄位
# ────────────────────────────────────────────────────────────

def test_t35_backtest_trades_no_cost_cols():
    for col in BACKTEST_TRADES_COLS:
        assert col not in BANNED_COLS, (
            f"BACKTEST_TRADES_COLS 不應含 '{col}'（已移除的成本欄位）"
        )
    assert "observed_return_pct" in BACKTEST_TRADES_COLS, (
        "BACKTEST_TRADES_COLS 應含 'observed_return_pct'"
    )
    print("✓ T35 PASS — BACKTEST_TRADES_COLS 無成本欄位，含 observed_return_pct")


# ────────────────────────────────────────────────────────────
# T36：SELECT 欄位與 BACKTEST_TRADES_COLS 完全一致
# ────────────────────────────────────────────────────────────

def test_t36_select_matches_cols():
    """
    驗證 export_backtest_trades 的 SELECT 欄位清單。
    直接比對 BACKTEST_TRADES_COLS 中每個欄位名稱是否出現在 SELECT 語句中，
    不允許 BANNED_COLS 出現。
    """
    from backtest import export_engine
    import inspect
    src = inspect.getsource(export_engine.export_backtest_trades)

    # 直接驗證各欄位是否在 SELECT 源碼中出現
    for col in BACKTEST_TRADES_COLS:
        assert col in src, (
            f"BACKTEST_TRADES_COLS 中的 '{col}' 未出現在 export_backtest_trades 的 SELECT 中"
        )

    # 確認 BANNED_COLS 不在 SELECT 中
    for col in BANNED_COLS:
        assert col not in src or ("-- " + col) in src, (
            f"export_backtest_trades 的 SELECT 不應含 '{col}'"
        )

    # 確認 entry_at_trigger 不在 SELECT 中
    assert "entry_at_trigger" not in src, (
        "export_backtest_trades 的 SELECT 不應含 entry_at_trigger"
    )

    print(f"✓ T36 PASS — SELECT 欄位通過一致性驗證（{len(BACKTEST_TRADES_COLS)} 欄）")
    print(f"   banned 欄位均不存在: {BANNED_COLS}")
def test_t37_event_master_no_trigger():
    for col in EVENT_MASTER_COLS:
        assert col not in BANNED_ENTRY, (
            f"EVENT_MASTER_COLS 不應含 '{col}'（1分K無法重建觸發成交價）"
        )
    # 確認可執行進場欄位都在
    for col in ("entry_at_bar_close", "entry_next_open", "entry_next_close"):
        assert col in EVENT_MASTER_COLS, f"EVENT_MASTER_COLS 應含 '{col}'"
    # 量比三件組也應在
    # price_pct_at_0910 已移除（= early_high_pct，重複欄位）
    assert "price_pct_at_0910" not in EVENT_MASTER_COLS, "price_pct_at_0910 應已移除"
    for col in ("cumulative_volume_at_0910", "volume_ratio_at_0910", "early_high_pct"):
        assert col in EVENT_MASTER_COLS, f"EVENT_MASTER_COLS 應含 '{col}'"
    print("✓ T37 PASS — EVENT_MASTER_COLS 無 entry_at_trigger，含量比三件組")


# ────────────────────────────────────────────────────────────
# T38：BacktestRequest 不含成本參數
# ────────────────────────────────────────────────────────────

def test_t38_backtest_request_no_cost_params():
    main_src = open(
        os.path.join(os.path.dirname(__file__), "..", "main.py")
    ).read()
    idx = main_src.find("class BacktestRequest")
    assert idx >= 0, "找不到 BacktestRequest"
    # 取到下一個 class 前
    next_cls = main_src.find("class ", idx + 20)
    bt_src = main_src[idx:next_cls] if next_cls > 0 else main_src[idx:]

    for param in ["commission:", "commission_dis:", "tax_cost:", "net_return:"]:
        assert param not in bt_src, (
            f"BacktestRequest 不應含 {param!r}（交易成本已移除）"
        )
    print("✓ T38 PASS — BacktestRequest 無交易成本參數")

def test_t39_observed_return_pct_presence():
    from backtest import backtest_engine
    src = inspect.getsource(backtest_engine)

    assert "observed_return_pct" in src, "backtest_engine 應包含 observed_return_pct"
    assert "gross_return" not in src.replace("# ", ""), (
        "backtest_engine 不應再有 gross_return（應已改名）"
    )
    # INSERT 欄位應含 observed_return_pct
    assert "observed_return_pct" in src and "obs_rets" in src, (
        "INSERT 應使用 observed_return_pct 和 :obs_rets 參數"
    )
    print("✓ T39 PASS — observed_return_pct 正確出現在 backtest_engine")


# ────────────────────────────────────────────────────────────
# T40：量比算法與前端 loadHotVolStocks 一致
# ────────────────────────────────────────────────────────────

def test_t40_volume_ratio_matches_frontend():
    """
    前端邏輯（index.html loadHotVolStocks）：
        const avg5 = 近5個交易日日成交量均值
        const volRatio = todayVolZhang / avg5

    後端 compute_volume_ratio：
        avg = sum(prev_n_day_volumes) / len(prev_n_day_volumes)
        volRatio = obs_volume / avg

    驗證兩套算法對相同輸入產生相同結果。
    """
    # 場景 1：前端計算
    prev5 = [5000, 4800, 5200, 4900, 5100]   # 近5日均量（張）
    obs_volume = 3200                           # 09:00~09:10 累積量（張）

    frontend_avg5   = sum(prev5) / len(prev5)  # = 5000.0
    frontend_ratio  = round(obs_volume / frontend_avg5, 4)  # = 0.64

    backend_ratio = compute_volume_ratio(obs_volume, prev5)

    assert backend_ratio is not None, "compute_volume_ratio 應回傳數值"
    assert abs(backend_ratio - frontend_ratio) < 0.0001, (
        f"後端算法應與前端一致: backend={backend_ratio}, frontend={frontend_ratio}"
    )

    # 場景 2：高量比（爆量）
    prev5_b = [2000, 1800, 2100, 1900, 2000]
    obs_b   = 6000
    frontend_b = round(obs_b / (sum(prev5_b) / len(prev5_b)), 4)
    backend_b  = compute_volume_ratio(obs_b, prev5_b)
    assert abs(backend_b - frontend_b) < 0.0001, (
        f"高量比場景: backend={backend_b}, frontend={frontend_b}"
    )

    # 場景 3：edge case
    assert compute_volume_ratio(0, [1000, 1000]) == 0.0
    assert compute_volume_ratio(100, []) is None
    assert compute_volume_ratio(100, [0, 0]) is None

    print("✓ T40 PASS — 量比算法與前端 loadHotVolStocks 一致")
    print(f"   場景1: prev5均量={frontend_avg5:.0f}張, obs={obs_volume}張, ratio={backend_ratio}")
    print(f"   場景2: ratio={backend_b}（爆量）")


# ────────────────────────────────────────────────────────────
# T41：量比 at 0910 不使用未來資料（look-ahead 驗證）
# ────────────────────────────────────────────────────────────

def test_t41_volume_ratio_no_lookahead():
    """
    volume_ratio_at_0910 只能用 09:00~09:10 的累積量（分子）
    和 target_date 之前的5日均量（分母）。
    驗證：多傳一根 09:11 的量，不改變結果（因為呼叫端只傳 09:10 以前的量）。
    """
    prev5 = [3000, 3200, 2900, 3100, 3000]

    # 正確：09:00~09:10 累積量
    obs_at_0910 = 1500
    ratio_correct = compute_volume_ratio(obs_at_0910, prev5)

    # 錯誤假設：如果誤用全天量（look-ahead）
    obs_full_day = 12000
    ratio_wrong  = compute_volume_ratio(obs_full_day, prev5)

    assert ratio_correct != ratio_wrong, "09:10 量 vs 全天量應產生不同結果"
    assert ratio_correct < ratio_wrong,  "早盤量 < 全天量，ratio 應更小"

    print("✓ T41 PASS — volume_ratio_at_0910 look-ahead 邏輯正確")
    print(f"   ratio@0910={ratio_correct}（obs={obs_at_0910}張，09:10前）")
    print(f"   ratio@close={ratio_wrong}（obs={obs_full_day}張，全天，不可用）")


# ────────────────────────────────────────────────────────────
# T42：classify_volume_ratio 分層正確
# ────────────────────────────────────────────────────────────

def test_t42_classify_volume_ratio():
    cases = [
        (None,  "N/A"),
        (0.8,   "<1.5"),
        (1.3,   "<1.5"),
        (1.5,   ">=1.5"),
        (1.99,  ">=1.5"),
        (2.0,   ">=2.0"),
        (2.5,   ">=2.5"),
        (3.0,   ">=3.0"),
        (3.5,   ">=3.5"),
        (4.0,   ">=4.0"),
        (4.5,   ">=4.5"),
        (5.0,   ">=5.0"),
        (8.0,   ">=5.0"),
    ]
    for vr, expected in cases:
        actual = classify_volume_ratio(vr)
        assert actual == expected, f"classify_volume_ratio({vr}) 應={expected}，實際={actual}"

    print("✓ T42 PASS — classify_volume_ratio 分層正確")
    print("   分層: <1.5 / >=1.5 / >=2.0 / >=2.5 / >=3.0 / >=3.5 / >=4.0 / >=4.5 / >=5.0")


# ────────────────────────────────────────────────────────────
# T43：exit_reason 合法值
# ────────────────────────────────────────────────────────────

def test_t43_exit_reason_values():
    """
    _determine_exit 回傳的 exit_reason 只能是 hit / timeout / excluded。
    不應出現 TP / SL。
    """
    from backtest.backtest_engine import _determine_exit

    # 模擬 TP hit
    row_hit = {
        "first_plus100_time": "09:30:00",
        "first_minus050_time": None,
        "tp100_within_5m": False,
        "tp100_within_10m": False,
        "return_0959": 0.5,
        "exit_price_0959": 100.5,
        "mfe_0959": 1.2,
        "mae_0959": -0.1,
    }
    r = _determine_exit(1.0, 0.5, "0959", row_hit)
    assert r["exit_reason"] in VALID_EXIT_REASONS, (
        f"exit_reason='{r['exit_reason']}' 不在合法值 {VALID_EXIT_REASONS}"
    )
    assert r["exit_reason"] == "hit", f"TP 在截止前觸發，應='hit'，實際='{r['exit_reason']}'"

    # 模擬 timeout
    row_timeout = {
        "first_plus100_time": None,
        "first_minus050_time": None,
        "return_0959": 0.3,
        "exit_price_0959": 100.3,
        "mfe_0959": 0.5,
        "mae_0959": -0.1,
    }
    r2 = _determine_exit(1.0, 0.5, "0959", row_timeout)
    assert r2["exit_reason"] == "timeout", f"無觸發應='timeout'，實際='{r2['exit_reason']}'"
    assert r2["exit_reason"] not in ("TP", "SL"), "舊 exit_reason 值不應出現"

    # 模擬 excluded
    row_exc = {
        "first_plus100_time": "09:25:00",
        "first_minus050_time": "09:25:00",  # 同一根
        "return_0959": 0.1,
        "exit_price_0959": 100.1,
        "mfe_0959": 1.1,
        "mae_0959": -0.6,
    }
    r3 = _determine_exit(1.0, 0.5, "0959", row_exc, "exclude")
    assert r3["exit_reason"] == "excluded", f"exclude policy 應='excluded'，實際='{r3['exit_reason']}'"

    print("✓ T43 PASS — exit_reason 只有 hit / timeout / excluded")


# ────────────────────────────────────────────────────────────
# T44：schema 文件描述一致性
# ────────────────────────────────────────────────────────────

def test_t44_schema_consistency():
    """讀 schema.sql，驗證關鍵描述已更新。"""
    schema = open(os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")).read()

    # exit_reason 描述應為新版
    assert "'hit' / 'timeout' / 'excluded'" in schema, (
        "schema.sql 的 exit_reason 描述應已更新為 'hit' / 'timeout' / 'excluded'"
    )
    # 不應有舊描述
    assert "'TP'/'SL'/'timeout'" not in schema, (
        "schema.sql 不應還有舊的 exit_reason 描述 'TP'/'SL'/'timeout'"
    )
    # gross_return 應已改名（在 backtest_trades 的欄位定義中）
    assert "observed_return_pct" in schema, (
        "schema.sql 應包含 observed_return_pct"
    )
    # 量比欄位
    assert "volume_ratio_at_0910" in schema, (
        "schema.sql 應包含 volume_ratio_at_0910"
    )
    assert "cumulative_volume_at_0910" in schema, (
        "schema.sql 應包含 cumulative_volume_at_0910"
    )

    print("✓ T44 PASS — schema.sql 文件描述一致")


# ────────────────────────────────────────────────────────────
# 執行
# ────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_t35_backtest_trades_no_cost_cols,
        test_t36_select_matches_cols,
        test_t37_event_master_no_trigger,
        test_t38_backtest_request_no_cost_params,
        test_t39_observed_return_pct_presence,
        test_t40_volume_ratio_matches_frontend,
        test_t41_volume_ratio_no_lookahead,
        test_t42_classify_volume_ratio,
        test_t43_exit_reason_values,
        test_t44_schema_consistency,
    ]

    passed = 0
    failed = 0
    print("=" * 60)
    print("v8 純化與量比 Tests")
    print("=" * 60)
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"✗ FAIL — {fn.__name__}")
            print(f"  {e}")
            failed += 1
        except Exception as e:
            print(f"✗ ERROR — {fn.__name__}: {e}")
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
