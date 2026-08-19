"""
v9 Frontend/API Consistency Tests
===================================
T45  前端 startBacktest 不傳 commission / commission_dis / tax
T46  前端 startBacktest 傳入 min_volume_ratio（當有選量比門檻時）
T47  BacktestRequest 包含 min_volume_ratio 欄位
T48  min_volume_ratio → backtest SQL WHERE 條件完整傳遞
T49  前端 renderSummary 不含 avg_net_return / avg_gross_return
T50  前端不含 bt-comm / bt-dis UI 元素
T51  全專案 production code 無殘留成本欄位
"""

import sys, os, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "index.html")
BACKEND  = os.path.join(os.path.dirname(__file__), "..")

BANNED_PROD = [
    "avg_net_return", "avg_gross_return",
    "commission_cost", "tax_cost", "net_return",
    "bt-comm", "bt-dis",
]


def read_fe():
    return open(FRONTEND, encoding="utf-8").read()

def read_be(fname):
    return open(os.path.join(BACKEND, fname), encoding="utf-8").read()


# ────────────────────────────────────────────────────────────
# T45：前端不傳成本參數給 API
# ────────────────────────────────────────────────────────────

def test_t45_frontend_no_cost_in_body():
    fe = read_fe()
    # 找 startBacktest 函數體
    idx = fe.find("async function startBacktest()")
    fn_body = fe[idx:fe.find("\n}\n", idx)+3]

    for banned in ["commission:", "commission_dis:", "tax:"]:
        assert banned not in fn_body, (
            f"startBacktest 不應傳 '{banned}' 給 API"
        )
    print("✓ T45 PASS — startBacktest 不傳交易成本參數")


# ────────────────────────────────────────────────────────────
# T46：前端傳 min_volume_ratio
# ────────────────────────────────────────────────────────────

def test_t46_frontend_sends_min_volume_ratio():
    fe = read_fe()
    idx = fe.find("async function startBacktest()")
    fn_body = fe[idx:fe.find("\n}\n", idx)+3]
    assert "min_volume_ratio" in fn_body, (
        "startBacktest 應傳 min_volume_ratio 給 API"
    )
    assert "bt-min-vr" in fn_body, (
        "startBacktest 應讀取 #bt-min-vr 元素"
    )
    print("✓ T46 PASS — startBacktest 傳入 min_volume_ratio")


# ────────────────────────────────────────────────────────────
# T47：BacktestRequest 含 min_volume_ratio
# ────────────────────────────────────────────────────────────

def test_t47_backtest_request_has_min_vr():
    src = read_be("main.py")
    idx = src.find("class BacktestRequest")
    end = src.find("\nclass ", idx + 10)
    bt_src = src[idx:end] if end > 0 else src[idx:]
    assert "min_volume_ratio" in bt_src, (
        "BacktestRequest 應包含 min_volume_ratio 欄位"
    )
    print("✓ T47 PASS — BacktestRequest 含 min_volume_ratio")


# ────────────────────────────────────────────────────────────
# T48：min_volume_ratio 傳入 SQL WHERE
# ────────────────────────────────────────────────────────────

def test_t48_min_vr_in_sql():
    src = read_be("backtest/backtest_engine.py")
    assert "mvr" in src and "min_volume_ratio" in src, (
        "backtest_engine 應含 :mvr 佔位符和 min_volume_ratio"
    )
    assert "volume_ratio_at_0910" in src and ":mvr" in src, (
        "WHERE 條件應篩選 volume_ratio_at_0910 >= :mvr"
    )
    print("✓ T48 PASS — min_volume_ratio 完整傳入 SQL WHERE")


# ────────────────────────────────────────────────────────────
# T49：前端 renderSummary 不含舊損益欄位
# ────────────────────────────────────────────────────────────

def test_t49_frontend_summary_no_profit():
    fe = read_fe()
    idx = fe.find("function renderSummary(")
    fn_body = fe[idx:fe.find("\n}\n", idx)+3]

    for banned in ["avg_net_return", "avg_gross_return", "net_return"]:
        assert banned not in fn_body, (
            f"renderSummary 不應含 '{banned}'"
        )
    # 應含達成率欄位
    assert "hit_rate_pct" in fn_body, "renderSummary 應含 hit_rate_pct"
    print("✓ T49 PASS — renderSummary 無損益欄位，含達成率")


# ────────────────────────────────────────────────────────────
# T50：前端無 bt-comm / bt-dis UI 元素
# ────────────────────────────────────────────────────────────

def test_t50_frontend_no_cost_ui():
    fe = read_fe()
    assert "bt-comm" not in fe, "前端不應有 bt-comm 元素（手續費率）"
    assert "bt-dis"  not in fe, "前端不應有 bt-dis 元素（折數）"
    assert "bt-min-vr" in fe, "前端應有 bt-min-vr 元素（量比門檻）"
    print("✓ T50 PASS — 前端無手續費 UI，有量比門檻選單")


# ────────────────────────────────────────────────────────────
# T51：全 production code 無成本殘留
# ────────────────────────────────────────────────────────────

def test_t51_no_banned_in_production():
    """
    掃描所有 production 程式碼（.py + .html），確認無殘留成本欄位。
    排除 test files 和 comment-only 行。
    """
    scan_files = [
        ("frontend/index.html",          read_fe()),
        ("backtest/backtest_engine.py",  read_be("backtest/backtest_engine.py")),
        ("backtest/export_engine.py",    read_be("backtest/export_engine.py")),
        ("main.py",                      read_be("main.py")),
    ]

    violations = []
    for fname, src in scan_files:
        for line_no, line in enumerate(src.split("\n"), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*"):
                continue
            for banned in BANNED_PROD:
                if banned in line:
                    violations.append(f"{fname}:{line_no}: '{banned}' in '{stripped[:80]}'")

    assert not violations, (
        "Production code 含殘留成本欄位:\n" + "\n".join(violations)
    )
    print(f"✓ T51 PASS — 全 production code 無殘留成本欄位")
    print(f"   掃描: {[f for f,_ in scan_files]}")


# ────────────────────────────────────────────────────────────
# 執行
# ────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────
# T52：key_price = early_high_price，非昨收
# ────────────────────────────────────────────────────────────

def test_t52_key_price_is_early_high_not_prev_close():
    """
    key_price = MAX(high, 09:00~09:10)，就是那面牆。
    early_high_pct >= threshold 只是決定這面牆值不值得研究的門票。
    prev_close 不參與 Attack Detection。
    """
    from events.key_engine import detect_keys_v1
    from unittest.mock import MagicMock
    from datetime import date

    mock_rows = [
        ("09:00:00", 101.0),
        ("09:03:00", 105.0),   # 最高點
        ("09:05:00", 103.5),
        ("09:10:00", 102.0),   # 視窗最後一根，已回落
    ]
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchall.return_value = mock_rows

    key = detect_keys_v1(mock_db, date(2024, 1, 2), "2330",
                         early_start="09:00:00", early_end="09:10:00")

    assert key is not None
    assert key["key_price"] == 105.0, (
        f"key_price 應=105.0（最高實際成交價），實際={key['key_price']}"
    )
    assert key["key_source_time"]    == "09:03:00"
    assert key["key_confirmed_time"] == "09:10:00"

    print("✓ T52 PASS — key_price=105（最高實際成交價），source=09:03，confirmed=09:10")
    print(f"   early_high_pct=+5% 只是門票，牆是105本身")


# ────────────────────────────────────────────────────────────
# T53：price_pct_at_0910 已從 EVENT_MASTER 移除
# ────────────────────────────────────────────────────────────

def test_t53_price_pct_at_0910_removed():
    """price_pct_at_0910 與 early_high_pct 完全重複，已移除。"""
    from backtest.export_engine import EVENT_MASTER_COLS

    assert "price_pct_at_0910" not in EVENT_MASTER_COLS, (
        "price_pct_at_0910 應已移除（與 early_high_pct 重複）"
    )
    assert "early_high_pct"            in EVENT_MASTER_COLS
    assert "cumulative_volume_at_0910" in EVENT_MASTER_COLS
    assert "volume_ratio_at_0910"      in EVENT_MASTER_COLS

    print("✓ T53 PASS — price_pct_at_0910 已移除，early_high_pct 保留")


# ────────────────────────────────────────────────────────────
# 執行
# ────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_t45_frontend_no_cost_in_body,
        test_t46_frontend_sends_min_volume_ratio,
        test_t47_backtest_request_has_min_vr,
        test_t48_min_vr_in_sql,
        test_t49_frontend_summary_no_profit,
        test_t50_frontend_no_cost_ui,
        test_t51_no_banned_in_production,
        test_t52_key_price_is_early_high_not_prev_close,
        test_t53_price_pct_at_0910_removed,
    ]
    passed = failed = 0
    print("=" * 60)
    print("v9 Frontend/API Consistency Tests")
    print("=" * 60)
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"\u2717 FAIL \u2014 {fn.__name__}\n  {e}")
            failed += 1
        except Exception as e:
            print(f"\u2717 ERROR \u2014 {fn.__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"\u7d50\u679c\uff1a{passed} PASS / {failed} FAIL")
    print("=" * 60)
    return failed == 0

if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
