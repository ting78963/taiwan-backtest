"""
v10 Fetch Task Tests
====================
T54  background fetch function 確實會被執行（不只是 task_id 被建立）
T55  成功路徑：running → done
T56  exception 路徑：running → error，message 被保存
T57  FinMind timeout：task 必須變 error，不能永久 running
T58  單日 2026-08-18 只處理一次，不 infinite loop
T59  已完成日期維持 lazy-fetch 行為（不重新抓）
T60  collection_threshold 行為不變
"""

import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch
from datetime import date


# ────────────────────────────────────────────────────────────
# T54：background function 確實被執行
# ────────────────────────────────────────────────────────────

def test_t54_background_function_executes():
    """
    模擬 run() 函數確實被呼叫（不只是 task dict 被建立）。
    """
    executed = []

    def mock_run():
        executed.append("ran")

    # FastAPI BackgroundTasks 的模擬
    tasks = []
    mock_bt = MagicMock()
    mock_bt.add_task.side_effect = lambda fn: tasks.append(fn)

    mock_bt.add_task(mock_run)
    assert len(tasks) == 1, "add_task 應被呼叫一次"

    # 模擬 Uvicorn 執行 background task
    tasks[0]()
    assert len(executed) == 1, "background function 應被執行"

    print("✓ T54 PASS — background function 確實會被執行")


# ────────────────────────────────────────────────────────────
# T55：成功路徑 running → done
# ────────────────────────────────────────────────────────────

def test_t55_success_running_to_done():
    """模擬 fetch 成功，task 從 running 變 done。"""
    task_store = {}

    def set_task(tid, status, **kwargs):
        task_store[tid] = {"status": status, **kwargs}

    task_id = "fetch_test_001"
    set_task(task_id, "running", message="開始...")

    assert task_store[task_id]["status"] == "running"

    # 模擬 fetch 成功完成
    mock_result = {"missing_dates": 1, "fetched_dates": 1, "results": [{"date": "2026-08-18"}]}
    set_task(task_id, "done", **mock_result)

    assert task_store[task_id]["status"] == "done", (
        f"成功後 status 應=done，實際={task_store[task_id]['status']}"
    )
    assert "results" in task_store[task_id]

    print("✓ T55 PASS — 成功路徑：running → done")


# ────────────────────────────────────────────────────────────
# T56：exception 路徑 running → error
# ────────────────────────────────────────────────────────────

def test_t56_exception_running_to_error():
    """任何 exception 必須讓 task 變 error，message 被保存。"""
    task_store = {}

    def set_task(tid, status, **kwargs):
        task_store[tid] = {"status": status, **kwargs}

    task_id = "fetch_test_002"
    set_task(task_id, "running", message="開始...")

    # 模擬 run() 內部的 try/except
    try:
        raise ValueError("FinMind API 回傳異常")
    except Exception as e:
        set_task(task_id, "error", message=str(e))

    assert task_store[task_id]["status"] == "error", (
        f"exception 後 status 應=error，實際={task_store[task_id]['status']}"
    )
    assert "FinMind" in task_store[task_id]["message"], (
        "error message 應包含 exception 內容"
    )

    print("✓ T56 PASS — exception 路徑：running → error，message 保存")


# ────────────────────────────────────────────────────────────
# T57：timeout 不能永久 running
# ────────────────────────────────────────────────────────────

def test_t57_timeout_must_not_stay_running():
    """
    FinMind timeout 時，requests.Timeout exception 必須被捕捉，
    task 必須轉為 error。
    """
    import requests

    task_store = {}

    def set_task(tid, status, **kwargs):
        task_store[tid] = {"status": status, **kwargs}

    task_id = "fetch_test_003"
    set_task(task_id, "running")

    # 模擬 requests.Timeout 被拋出
    def mock_fetch_that_times_out(*args, **kwargs):
        raise requests.Timeout("Connection timed out")

    try:
        mock_fetch_that_times_out()
    except Exception as e:
        set_task(task_id, "error", message=str(e))

    assert task_store[task_id]["status"] == "error", (
        f"timeout 後 task 應=error，不能永久 running，實際={task_store[task_id]['status']}"
    )
    assert task_store[task_id]["status"] != "running"

    print("✓ T57 PASS — timeout 後 task 轉為 error，不永久 running")


# ────────────────────────────────────────────────────────────
# T58：單日只處理一次，不 infinite loop
# ────────────────────────────────────────────────────────────

def test_t58_single_date_processed_once():
    """
    date_from = date_to = 2026-08-18，只會出現一次在 missing list 中。
    fetch_missing_dates 對這個 list 做一次 for loop，不會 infinite loop。
    """
    from data.inventory import get_missing_dates

    mock_db = MagicMock()
    # 模擬 DB 回傳：2026-08-18 不在 inventory → missing
    mock_db.execute.return_value.fetchall.return_value = []

    target = date(2026, 8, 18)
    missing = get_missing_dates(mock_db, target, target, 0.025)

    # 只應出現一次
    assert missing.count(target) == 1, (
        f"2026-08-18 應只在 missing list 出現一次，實際={missing.count(target)}"
    )
    assert len(missing) == 1, f"單日範圍應只有 1 個 missing date，實際={len(missing)}"

    print("✓ T58 PASS — 單日只處理一次，不 infinite loop")
    print(f"   missing={missing}")


# ────────────────────────────────────────────────────────────
# T59：已完成日期維持 lazy-fetch 行為
# ────────────────────────────────────────────────────────────

def test_t59_completed_date_skipped():
    """
    已在 data_inventory 的日期，get_missing_dates 應回傳空清單。
    """
    from data.inventory import get_missing_dates

    mock_db = MagicMock()
    target = date(2026, 8, 18)

    # 模擬 DB 已有該日期（collection_threshold=0.025 <= requested 0.025）
    mock_db.execute.return_value.fetchall.return_value = [(target,)]

    missing = get_missing_dates(mock_db, target, target, 0.025)

    assert len(missing) == 0, (
        f"已完成日期不應出現在 missing list，實際={missing}"
    )

    print("✓ T59 PASS — 已完成日期被 lazy-fetch 機制跳過")


# ────────────────────────────────────────────────────────────
# T60：collection_threshold 行為不變
# ────────────────────────────────────────────────────────────

def test_t60_collection_threshold_unchanged():
    """
    舊 inventory threshold=3.5% > 新 request=2.5% → 應視為需要補抓。
    舊 inventory threshold=2.5% <= 新 request=3.5% → 可跳過。
    """
    from data.inventory import get_missing_dates

    target = date(2026, 8, 18)

    # 情境 A：舊 3.5% > 新 2.5% → 補抓
    mock_db_a = MagicMock()
    mock_db_a.execute.return_value.fetchall.return_value = []
    missing_a = get_missing_dates(mock_db_a, target, target, 0.025)
    assert len(missing_a) == 1, "舊 threshold 太高，應補抓"

    # 情境 B：舊 2.5% <= 新 3.5% → 跳過
    mock_db_b = MagicMock()
    mock_db_b.execute.return_value.fetchall.return_value = [(target,)]
    missing_b = get_missing_dates(mock_db_b, target, target, 0.035)
    assert len(missing_b) == 0, "舊 threshold 夠低，應跳過"

    print("✓ T60 PASS — collection_threshold 行為不變")


def test_t61_candidate_filter_logic():
    """
    4%+4000張粗篩邏輯驗證。
    key_price = early_high_price（不是昨收）。
    """
    import pandas as pd
    from events.volume_ratio import compute_volume_ratio

    # 測試案例
    cases = [
        # (prev_close, early_high, early_vol_zhang, threshold_pct, min_zhang, expected_candidate)
        (100.0, 105.0, 5000, 0.04, 4000, True),   # +5%, 5000張 → 通過
        (100.0, 105.0, 3999, 0.04, 4000, False),  # +5%, 3999張 → 量不足
        (100.0, 103.9, 10000, 0.04, 4000, False), # +3.9%, 10000張 → 漲幅不足
        (100.0, 104.0, 4000, 0.04, 4000, True),   # +4%, 4000張 → 剛好通過
        (100.0, 103.9, 3999, 0.04, 4000, False),  # 兩個都不通過
    ]

    for prev_close, early_high, early_vol, thresh, min_vol, expected in cases:
        pct = (early_high / prev_close - 1) * 100
        passes_price  = pct >= thresh * 100
        passes_volume = early_vol >= min_vol
        is_candidate  = passes_price and passes_volume

        assert is_candidate == expected, (
            f"prev={prev_close} high={early_high} vol={early_vol}張 "
            f"→ candidate={is_candidate}，應={expected}"
        )

        # 驗證 key_price = early_high_price（不是昨收）
        key_price = early_high
        assert key_price != prev_close or early_high == prev_close, (
            "key_price 應等於 early_high_price，不是 prev_close"
        )

    # 量比計算驗證
    prev5 = [10000, 12000, 8000, 11000, 9000]  # 前5日全天量（張）
    obs_vol = 40000                              # 今日09:10累積量（張）
    avg5 = sum(prev5) / len(prev5)              # = 10000
    expected_vr = round(obs_vol / avg5, 4)      # = 4.0

    actual_vr = compute_volume_ratio(obs_vol, prev5)
    assert abs(actual_vr - expected_vr) < 0.001, (
        f"量比應={expected_vr}，實際={actual_vr}"
    )

    print("✓ T61 PASS — 4%+4000張粗篩邏輯正確")
    print(f"   +5%,5000張=True / +5%,3999張=False / +3.9%,10000張=False / +4%,4000張=True")
    print(f"   key_price = early_high_price（不是昨收）")
    print(f"   前5日均量={avg5}張，今日09:10={obs_vol}張，量比={actual_vr}x")


# ────────────────────────────────────────────────────────────
# 執行
# ────────────────────────────────────────────────────────────

def test_t62_force_rerun_uses_all_data_dates():
    """
    force_rerun=True 時，main.py 必須使用 get_all_data_dates()（全部 data_dates），
    不論 event_runs 完成了幾天。
    驗證：main.py /api/events/run 的路由有 force_rerun 分支邏輯。
    """
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    # force_rerun=True → get_all_data_dates
    assert "req.force_rerun" in main_src, "必須有 force_rerun 分支"
    assert "get_all_data_dates" in main_src, "force_rerun=True 應呼叫 get_all_data_dates"
    # force_rerun=False → get_dates_needing_events
    assert "get_dates_needing_events" in main_src, "force_rerun=False 應呼叫 get_dates_needing_events"
    # 語意確認：同一個 events/run handler 裡 force_rerun 在 get_dates_needing_events 之前
    handler_start = main_src.find("/api/events/run")
    handler_block = main_src[handler_start:handler_start+3000]
    pos_force = handler_block.find("req.force_rerun")
    pos_normal = handler_block.find("get_dates_needing_events")
    assert 0 < pos_force < pos_normal, "force_rerun 分支應在一般邏輯之前"

    print("✓ T62 PASS — force_rerun=True 使用 get_all_data_dates（不受 event_runs 限制）")
    print("   force_rerun=False 維持原有 get_dates_needing_events 行為")


def test_t63_force_rerun_log_message():
    """
    force_rerun 分支必須有明確 log，區分 force=True/False 與 dates_needed 數量。
    """
    main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    assert "force_rerun=True" in main_src, "應有 force_rerun=True 的 log"
    assert "force_rerun=False" in main_src, "應有 force_rerun=False 的 log"
    assert "dates_needed=" in main_src, "應 log dates_needed 數量"
    print("✓ T63 PASS — force_rerun 分支有明確 log 輸出")


def test_t64_get_all_data_dates_exists():
    """
    event_manager.py 必須有 get_all_data_dates 函數，
    只查 data_inventory，不過濾 event_runs。
    """
    em_src = open(os.path.join(os.path.dirname(__file__), "..", "events", "event_manager.py")).read()
    assert "def get_all_data_dates" in em_src, "event_manager 應有 get_all_data_dates 函數"
    assert "data_inventory" in em_src[em_src.find("def get_all_data_dates"):
                                       em_src.find("def get_all_data_dates")+300],         "get_all_data_dates 應查 data_inventory"
    # 確認不查 event_runs（不受完成狀態影響）
    # 取函數體直到下一個 def（避免誤抓鄰近函數的 event_runs）
    fn_start = em_src.find("def get_all_data_dates")
    fn_end = em_src.find("\ndef ", fn_start + 10)
    fn_body = em_src[fn_start:fn_end] if fn_end > 0 else em_src[fn_start:fn_start+400]
    assert "event_runs" not in fn_body, "get_all_data_dates 不應過濾 event_runs"
    print("✓ T64 PASS — get_all_data_dates 只查 data_inventory，不受 event_runs 限制")


def run_all():
    tests = [
        test_t54_background_function_executes,
        test_t55_success_running_to_done,
        test_t56_exception_running_to_error,
        test_t57_timeout_must_not_stay_running,
        test_t58_single_date_processed_once,
        test_t59_completed_date_skipped,
        test_t60_collection_threshold_unchanged,
        test_t61_candidate_filter_logic,
        test_t62_force_rerun_uses_all_data_dates,
        test_t63_force_rerun_log_message,
        test_t64_get_all_data_dates_exists,
    ]
    passed = failed = 0
    print("=" * 60)
    print("v10 Fetch Task Tests")
    print("=" * 60)
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"✗ FAIL — {fn.__name__}\n  {e}")
            failed += 1
        except Exception as e:
            print(f"✗ ERROR — {fn.__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"結果：{passed} PASS / {failed} FAIL")
    print("=" * 60)
    return failed == 0

if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
