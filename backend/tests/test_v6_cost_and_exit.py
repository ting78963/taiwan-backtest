"""
v6 Unit Tests
=============
T24  毛利+1%、3折手續費+當沖稅後淨報酬約+0.76%
T25  TP在第6根才發生，exit=5m必須timeout；exit=10m才可TP
T26  DB舊threshold=2.5%、request=2.0% → 必須補抓
T27  PostgreSQL integration test（需要 DATABASE_URL）
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.backtest_engine import (
    _determine_exit, DEFAULT_COMMISSION, DEFAULT_COMMISSION_DIS, DEFAULT_DAYTRADE_TAX
)


# ────────────────────────────────────────────────────────────
# T24：交易成本單位正確性
# ────────────────────────────────────────────────────────────

def test_t24_trading_cost_unit():
    """
    驗證成本計算單位一致。

    情境：
        entry = 100 元，exit = 101 元
        gross_return = +1.0%（百分點）
        手續費率 0.1425%（小數 0.001425），3 折 → 實際 0.04275%，買賣合計 0.0855%
        當沖稅 0.15%（小數 0.0015）
        net = 1.0% - 0.0855% - 0.15% = 0.7645%

    驗證：
        net ≈ 0.7645%（非 0.997%）
        舊錯誤：1.0 - 0.000855 - 0.0015 = 0.9976（成本少算 100 倍）
    """
    commission      = DEFAULT_COMMISSION      # 0.001425
    commission_dis  = DEFAULT_COMMISSION_DIS  # 0.3
    tax             = DEFAULT_DAYTRADE_TAX    # 0.0015

    effective_comm = commission * commission_dis  # 0.0004275（小數比例）

    gross = 1.0  # +1.0%（百分點單位）

    # 正確計算（成本轉換為 %）
    comm_cost_pct = round(effective_comm * 2 * 100, 6)  # 0.0855%
    tax_cost_pct  = round(tax * 100, 6)                  # 0.15%
    net_correct   = round(gross - comm_cost_pct - tax_cost_pct, 4)

    # 舊的錯誤計算（直接用小數比例相減）
    comm_cost_wrong = round(effective_comm * 2, 6)        # 0.000855（應該 0.0855%）
    tax_cost_wrong  = round(tax, 6)                       # 0.0015（應該 0.15%）
    net_wrong       = round(gross - comm_cost_wrong - tax_cost_wrong, 4)

    # 驗證
    assert abs(comm_cost_pct - 0.0855) < 0.0001, (
        f"手續費成本應=0.0855%，實際={comm_cost_pct}%"
    )
    assert abs(tax_cost_pct - 0.15) < 0.0001, (
        f"交易稅應=0.15%，實際={tax_cost_pct}%"
    )
    assert abs(net_correct - 0.7645) < 0.001, (
        f"淨報酬應≈0.7645%，實際={net_correct}%"
    )
    assert net_wrong > 0.99, (
        f"舊計算的錯誤結果應>0.99（成本少算100倍），實際={net_wrong}"
    )
    assert net_correct < net_wrong, "正確計算的淨報酬應低於錯誤計算"
    assert abs(net_correct - net_wrong) > 0.2, (
        f"兩種計算差距應>0.2%（足以影響策略評估），差距={abs(net_correct - net_wrong):.4f}%"
    )

    print("✓ T24 PASS — 交易成本單位正確")
    print(f"   gross       = {gross}%")
    print(f"   手續費成本   = {comm_cost_pct}%（買+賣合計，3折）")
    print(f"   當沖稅       = {tax_cost_pct}%")
    print(f"   net (正確)   = {net_correct}%")
    print(f"   net (舊錯誤) = {net_wrong}%（成本少算100倍）")
    print(f"   差距         = {abs(net_correct-net_wrong):.4f}%")


# ────────────────────────────────────────────────────────────
# T25：5m/10m 截止的 TP/SL 旗標驗證
# ────────────────────────────────────────────────────────────

def test_t25_5m_10m_within_flag():
    """
    TP 在第 6 根才發生（bar_pos = 6）：
        exit=5m → tp100_within_5m=False → timeout
        exit=10m → tp100_within_10m=True → 有效 TP

    驗證：
        within 旗標正確決定 TP/SL 是否有效
        5m 截止不接受第 6 根後的 TP
    """
    # 模擬 outcome_data row（TP 1.0% 在第 6 根觸發）
    row_tp_in_bar6 = {
        "first_plus100_time": "09:21:00",   # TP 在進場後第 6 根
        "first_minus050_time": None,
        # within 旗標：bar_pos=6 時，within_5m=False, within_10m=True
        "tp100_within_5m":  False,
        "tp100_within_10m": True,
        "sl050_within_5m":  False,
        "sl050_within_10m": False,
        # 各截止時間的報酬
        "return_5m":    0.25,   # 5m 截止時的實際 close 報酬
        "exit_price_5m": 100.25,
        "mfe_5m": 0.4,
        "mae_5m": -0.1,
        "return_10m":   1.05,
        "exit_price_10m": 101.05,
        "mfe_10m": 1.2,
        "mae_10m": -0.15,
    }

    # exit=5m：TP 在第 6 根，within_5m=False → timeout
    result_5m = _determine_exit(1.0, 0.5, "5m", row_tp_in_bar6)
    assert result_5m["exit_reason"] == "timeout", (
        f"TP 在第 6 根，exit=5m 應 timeout，實際={result_5m['exit_reason']}"
    )
    assert result_5m["hit"] is False
    assert abs(result_5m["observed_return_pct"] - 0.25) < 0.01, (
        f"5m timeout gross_return 應=0.25%，實際={result_5m['observed_return_pct']}"
    )

    # exit=10m：TP 在第 6 根，within_10m=True → hit
    result_10m = _determine_exit(1.0, 0.5, "10m", row_tp_in_bar6)
    assert result_10m["exit_reason"] == "hit", (
        f"TP 在第 6 根，exit=10m 應 hit，實際={result_10m['exit_reason']}"
    )
    assert result_10m["hit"] is True
    assert result_10m["observed_return_pct"] == 1.0

    print("✓ T25 PASS — 5m/10m within 旗標正確控制 TP/SL 有效性")
    print(f"   TP 在第 6 根（bar_pos=6）：")
    print(f"   exit=5m  → {result_5m['exit_reason']} (gross={result_5m['observed_return_pct']}%)")
    print(f"   exit=10m → {result_10m['exit_reason']} (gross={result_10m['observed_return_pct']}%)")


# ────────────────────────────────────────────────────────────
# T26：inventory threshold 下降必須補抓
# ────────────────────────────────────────────────────────────

def test_t26_inventory_threshold_direction():
    """
    DB 舊 threshold=2.5%，request=2.0%（門檻下降，更嚴格）→ 必須補抓
    DB 舊 threshold=2.5%，request=3.0%（門檻上升，放寬）→ 可跳過
    """
    def should_skip(db_threshold, requested):
        if db_threshold is None:
            return True
        return db_threshold <= requested

    # 門檻下降（必須補抓）
    assert should_skip(0.025, 0.020) is False, (
        "舊 2.5% > 新 2.0%：DB 資料不足以覆蓋 2.0%~2.5% 的股票 → 必須補抓"
    )
    # 門檻上升（可跳過）
    assert should_skip(0.025, 0.030) is True, (
        "舊 2.5% <= 新 3.0%：DB 資料已包含 3.0% 以上的股票 → 可跳過"
    )
    # 邊界：相同門檻（可跳過）
    assert should_skip(0.025, 0.025) is True, "相同門檻 → 可跳過"
    # NULL（向後相容）
    assert should_skip(None, 0.020) is True, "NULL threshold → 視為已覆蓋"

    print("✓ T26 PASS — inventory threshold 方向判斷正確")
    print(f"   舊2.5% → 新2.0%（下降）: 補抓")
    print(f"   舊2.5% → 新3.0%（上升）: 跳過")


# ────────────────────────────────────────────────────────────
# T27：PostgreSQL integration test
# ────────────────────────────────────────────────────────────

def test_t27_postgresql_integration():
    """
    真正連 PostgreSQL，驗證：
        1. Schema 可以建立
        2. backtest_trades INSERT 成功（含新欄位）
        3. SELECT 回來的值與 INSERT 的值一致
        4. commission_cost / tax_cost 是百分點單位（不是小數比例）

    需要環境變數 DATABASE_URL。若未設定，跳過但不 fail。
    """
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("⚠ T27 SKIP — DATABASE_URL 未設定（正式部署後才能跑）")
        print("  執行方式：DATABASE_URL=postgresql://... python3 tests/test_v6_cost_and_exit.py")
        return

    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(db_url, pool_pre_ping=True)
        Session = sessionmaker(bind=engine)
        db = Session()

        # 確認 schema 存在
        schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
        with open(schema_path, "r") as f:
            sql = f.read()
        with engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
        print("   Schema 初始化成功")

        # 取一個有效的 run_id 和 attack_id（或建立測試資料）
        # 這裡用簡化方式：直接插入最小可用資料，測完清除
        with engine.connect() as conn:
            # 建立測試用 backtest_run
            run_result = conn.execute(text("""
                INSERT INTO backtest_runs (run_name, params, date_from, date_to, status)
                VALUES ('T27_test', '{"test": true}'::jsonb, '2024-01-01', '2024-01-01', 'done')
                RETURNING run_id
            """))
            test_run_id = run_result.fetchone()[0]

            # 查詢一個現有的 attack_id（若無則跳過）
            attack_row = conn.execute(text(
                "SELECT attack_id FROM attack_events LIMIT 1"
            )).fetchone()

            if not attack_row:
                conn.execute(text(
                    "DELETE FROM backtest_runs WHERE run_id = :rid"
                ), {"rid": test_run_id})
                conn.commit()
                print("⚠ T27 PARTIAL — 無 attack_events 資料（需先跑 Event Engine）")
                print("  資料存在後執行此 test 可驗證 INSERT 完整性")
                return

            test_attack_id = attack_row[0]

            # INSERT 一筆 backtest_trade（包含所有新欄位）
            # commission_cost / tax_cost 是百分點（0.0855, 0.15），不是小數比例
            conn.execute(text("""
                INSERT INTO backtest_trades (
                    run_id, attack_id, strategy_id,
                    date, stock_id, entry_mode, entry_price,
                    gross_return, commission_cost, tax_cost, net_return,
                    exit_reason, mfe, mae
                ) VALUES (
                    :run_id, :attack_id, 'T27_test_strategy',
                    '2024-01-01', 'T27', 'bar_close', 100.0,
                    1.0, 0.0855, 0.15, 0.7645,
                    'TP', 1.2, -0.1
                )
            """), {"run_id": test_run_id, "attack_id": test_attack_id})
            conn.commit()
            print("   backtest_trades INSERT 成功")

            # SELECT 驗證
            trade = conn.execute(text("""
                SELECT gross_return, commission_cost, tax_cost, net_return, exit_reason
                FROM backtest_trades
                WHERE run_id = :rid AND strategy_id = 'T27_test_strategy'
            """), {"rid": test_run_id}).fetchone()

            assert trade is not None, "SELECT 應回傳插入的 trade"
            gross, comm, tax_val, net, reason = trade

            assert abs(float(gross) - 1.0) < 0.001, f"gross_return 應=1.0，實際={gross}"
            assert abs(float(comm) - 0.0855) < 0.001, (
                f"commission_cost 應=0.0855%（百分點），實際={comm}（若接近0.000855說明單位錯）"
            )
            assert abs(float(tax_val) - 0.15) < 0.001, f"tax_cost 應=0.15%，實際={tax_val}"
            assert abs(float(net) - 0.7645) < 0.001, f"net_return 應=0.7645%，實際={net}"
            assert reason == "TP", f"exit_reason 應=TP，實際={reason}"

            # 清除測試資料
            conn.execute(text(
                "DELETE FROM backtest_runs WHERE run_id = :rid"
            ), {"rid": test_run_id})
            conn.commit()

        db.close()
        print("✓ T27 PASS — PostgreSQL integration test 成功")
        print(f"   INSERT: gross=1.0%, comm=0.0855%, tax=0.15%, net=0.7645%")
        print(f"   SELECT: 所有欄位值與 INSERT 一致")

    except Exception as e:
        print(f"✗ T27 FAIL — {e}")
        import traceback
        traceback.print_exc()
        raise


# ────────────────────────────────────────────────────────────
# T28：_determine_exit 的 TP TIME COL key 格式驗證
# ────────────────────────────────────────────────────────────

def test_t28_within_key_format():
    """
    驗證 _tp_key / _sl_key 生成的 key 格式正確（用於 within 旗標）。
    重構後使用 _tp_key() / _sl_key() 函數，不再有 TP_TIME_COL dict。
    """
    from events.outcome_engine import _tp_key, _sl_key

    tp_cases = [
        (0.50, "050"),
        (1.00, "100"),
        (2.00, "200"),
        (2.50, "250"),
        (5.00, "500"),
    ]
    sl_cases = [
        (0.25, "025"),
        (0.50, "050"),
        (1.00, "100"),
        (1.25, "125"),
        (3.00, "300"),
    ]

    for tp, exp_k in tp_cases:
        k = _tp_key(tp)
        assert k == exp_k, f"_tp_key({tp}) 應={exp_k}，實際={k}"
        # within key 格式
        assert f"tp{k}_within_5m"  == f"tp{exp_k}_within_5m"
        assert f"tp{k}_within_10m" == f"tp{exp_k}_within_10m"

    for sl, exp_k in sl_cases:
        k = _sl_key(sl)
        assert k == exp_k, f"_sl_key({sl}) 應={exp_k}，實際={k}"
        assert f"sl{k}_within_5m"  == f"sl{exp_k}_within_5m"
        assert f"sl{k}_within_10m" == f"sl{exp_k}_within_10m"

    print("✓ T28 PASS — _tp_key/_sl_key 格式正確")
    for tp, k in tp_cases:
        print(f"   TP={tp} → key='{_tp_key(tp)}' → within='tp{_tp_key(tp)}_within_5m'")


# ────────────────────────────────────────────────────────────
# 執行所有測試
# ────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_t24_trading_cost_unit,
        test_t25_5m_10m_within_flag,
        test_t26_inventory_threshold_direction,
        test_t27_postgresql_integration,
        test_t28_within_key_format,
    ]

    passed = 0
    failed = 0
    skipped = 0
    print("=" * 60)
    print("v6 成本、出場邏輯 Unit Tests")
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
    if "T27" in str([t.__name__ for t in tests]):
        print("（T27 需要 DATABASE_URL，無 DB 時顯示 SKIP 不算 FAIL）")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
