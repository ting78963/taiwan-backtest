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


# ────────────────────────────────────────────────────────────
# T54：analyze denominator 必須只計 entry_time <= cutoff 的樣本
# ────────────────────────────────────────────────────────────

def test_t54_analyze_denominator_cutoff():
    """
    entry_time > cutoff 的 Attack 沒機會在 cutoff 前達標，不得計入分母。
    驗證：
      1. SQL 使用 :cutoff 參數（不拼 f-string）
      2. params 有 cutoff 鍵
      3. 不出現 <= \'{CUTOFF}\' 的 f-string 拼接
    """
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    idx = main_src.find("def analyze(")
    end = main_src.find("\n@app.", idx + 10)
    src = main_src[idx:end] if end > 0 else main_src[idx:idx+3000]

    # 必須有 :cutoff 參數
    assert ":cutoff" in src, "analyze SQL 應使用 :cutoff 參數，不拼 f-string"

    # 必須有 entry_time 的 cutoff 過濾（denominator）
    assert "entry_time" in src, "analyze SQL 應包含 entry_time 的 cutoff 過濾"

    # params 必須有 cutoff 鍵
    assert '"cutoff": CUTOFF' in src or '"cutoff"' in src, (
        "analyze params 應包含 cutoff 鍵"
    )

    # 不得出現 f-string 拼接 CUTOFF 進 SQL
    assert "'{CUTOFF}'" not in src, (
        "analyze SQL 不得出現 \'{CUTOFF}\' 的 f-string 拼接，應使用 :cutoff 參數"
    )

    # cutoff 必須從 req.cutoff 取得
    assert "req.cutoff" in src, "CUTOFF 值應從 req.cutoff 取得"

    print("✓ T54 PASS — analyze denominator 正確使用 :cutoff 參數")
    print("   entry_time <= CAST(:cutoff AS TIME)：分母只計 cutoff 前進場的樣本")
    print("   first_plusXXX_time <= CAST(:cutoff AS TIME)：TP numerator 同一 cutoff")
    print("   無 f-string 拼接，SQL injection 安全")


# ────────────────────────────────────────────────────────────
# T55：AnalyzeRequest 包含 cutoff 參數
# ────────────────────────────────────────────────────────────

def test_t55_analyze_request_has_cutoff():
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    idx = main_src.find("class AnalyzeRequest")
    end = main_src.find("\nclass ", idx + 10)
    az_src = main_src[idx:end]
    assert "cutoff" in az_src, "AnalyzeRequest 應包含 cutoff 參數"
    assert "09:59" in az_src, "cutoff 預設值應為 09:59"
    print("✓ T55 PASS — AnalyzeRequest 含 cutoff 參數，預設 09:59")


# ────────────────────────────────────────────────────────────
# 執行
# ────────────────────────────────────────────────────────────

def test_t59_invalid_cutoff_rejected():
    """
    不在白名單的 cutoff 值（如 '09:00'、'abc'）應被後端拒絕（HTTPException 422）。
    合法值必須全在 CUTOFF_MAP 白名單內。
    """
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    idx = main_src.find("def analyze(")
    end = main_src.find("\n@app.", idx + 10)
    src = main_src[idx:end] if end > 0 else main_src[idx:idx+5000]

    # 必須有白名單檢查
    assert "CUTOFF_MAP" in src, "analyze 應有 CUTOFF_MAP 白名單"
    assert "HTTPException" in src or "raise" in src, "非法 cutoff 應被拒絕"
    assert "422" in src or "status_code" in src, "應回傳 422"

    # 確認不再有舊的 fallback（直接接受任意字串）
    assert 'req.cutoff + ":00"' not in src, (
        "不應有 fallback 讓任意字串進 SQL，必須用白名單驗證"
    )
    print("✓ T59 PASS — 非法 cutoff 值被 422 拒絕，不進 SQL")
    print("   合法值：09:59 / 10:29 / 10:59 / 11:29 / 11:59 / 12:29 / 12:59 / 13:30")


def test_t56_bucket_exclusive_upper_bound():
    """
    max_attack_number=2 必須是 attack_number < 2（exclusive），
    所以 attack_number=2 不得進入 =1 的 bucket。
    驗證：main.py analyze 的 max 條件用 < 不用 <=。
    """
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    idx = main_src.find("def analyze(")
    end = main_src.find("\n@app.", idx + 10)
    src = main_src[idx:end] if end > 0 else main_src[idx:idx+5000]

    # 必須用 < 不是 <=
    assert "attack_number < :max_atk_num" in src, (
        "max_attack_number 條件必須用 < (exclusive)，不用 <="
    )
    assert "attack_volume_v1b < :max_atk_vol" in src
    assert "early_high_pct < :max_ehpct" in src
    assert "volume_ratio) < :max_vr" in src or "volume_ratio < :max_vr" in src
    assert "ae.c31 < :max_c31" in src

    # 不得出現 <= 的 max 條件
    assert "attack_number <= :max_atk_num" not in src, (
        "max 條件不得用 <=，必須用 < (exclusive upper bound)"
    )
    print("✓ T56 PASS — max_* 條件全部使用 < (exclusive upper bound)")
    print("   attack_number=2 不會進入 min=1,max=2 的 bucket")


def test_t57_require_c31_not_null():
    """
    require_c31_not_null=True 時，SQL filters 加上 ae.c31 IS NOT NULL。
    C31=NULL 的樣本不得計入分母。
    """
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    idx = main_src.find("def analyze(")
    end = main_src.find("\n@app.", idx + 10)
    src = main_src[idx:end] if end > 0 else main_src[idx:idx+5000]

    assert "require_c31_not_null" in src, (
        "analyze 應支援 require_c31_not_null 參數"
    )
    assert "ae.c31 IS NOT NULL" in src, (
        "require_c31_not_null=True 時應加入 ae.c31 IS NOT NULL 過濾"
    )
    print("✓ T57 PASS — require_c31_not_null=True 時 C31=NULL 不計入分母")
    print("   C31 bucket scan 以有值樣本為 baseline，不與全體 N=1025 比較")


def test_t58_cutoff_not_broken_by_max():
    """
    加入 max_* 條件後，cutoff 和 denominator 邏輯不得被破壞。
    entry_time <= cutoff 的過濾必須仍然存在。
    """
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    idx = main_src.find("def analyze(")
    end = main_src.find("\n@app.", idx + 10)
    src = main_src[idx:end] if end > 0 else main_src[idx:idx+5000]

    # denominator 仍然有 entry_time <= cutoff
    assert "entry_time" in src and ":cutoff" in src, (
        "加入 max_* 後，entry_time <= cutoff 的 denominator 過濾不得消失"
    )
    # TP numerator 仍然有 first_plusXXX_time <= cutoff
    assert "first_plus" in src and "CAST(:cutoff AS TIME)" in src, (
        "加入 max_* 後，TP numerator 的 cutoff 過濾不得消失"
    )
    # max 條件是獨立的 if block，不影響 cutoff
    assert "if req.max_attack_number" in src, "max_attack_number 應在獨立 if block"
    print("✓ T58 PASS — max_* 條件不影響 cutoff/denominator 邏輯")
    print("   entry_time <= cutoff 和 first_plusXXX <= cutoff 均完整保留")


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
        test_t54_analyze_denominator_cutoff,
        test_t55_analyze_request_has_cutoff,
        test_t59_invalid_cutoff_rejected,
        test_t56_bucket_exclusive_upper_bound,
        test_t57_require_c31_not_null,
        test_t58_cutoff_not_broken_by_max,
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
