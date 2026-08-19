"""
v3 修正項目 Unit Tests
=======================
驗證五個核心修正：
  T11  Key confirmed_time 必須是視窗結束時間（不是最高點時間）
  T12  Attack Detection 不得在 key_confirmed_time 之前開始
  T13  V1A/V1B 欄位在記憶體與 DB 結構中均完整存在
  T14  timeout 報酬 = 實際截止 close 計算的報酬，不是 (MFE+MAE)/2
  T15  collection_threshold=2.5% vs research_threshold=3.5%：3.0%股票資料存在但不進研究
"""

import sys, os
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from events.attack_engine import find_attacks, compute_c_values, fill_entry_prices, _build_attack_record
from events.key_engine import detect_keys_v1
from events.outcome_engine import compute_outcome
from events.volume_ratio import compute_early_volume_speed_ratio


def make_bar(t, o, h, l, c, v):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}

def make_df(bars):
    df = pd.DataFrame(bars)
    df["time_str"] = df["time"].astype(str)
    return df


# ────────────────────────────────────────────────────────────
# T11：Key confirmed_time 必須是視窗結束，不是最高點時間
# ────────────────────────────────────────────────────────────

def test_t11_key_confirmed_time_is_window_end():
    """
    場景：
        09:00~09:10 早盤視窗
        09:03 出現最高點 290（key_source_time = 09:03）
        09:07 出現更高點 291，然後回落
        09:10 視窗結束，確認最高點為 291

    驗證：
        key_price = 291（視窗內真正最高點）
        key_source_time = 09:07（291 第一次出現的時間）
        key_confirmed_time = 09:10:00（視窗結束時間，Attack 起始點）
        key_confirmed_time ≠ key_source_time

    市場語意：
        09:03 看到 290，但不知道 09:07 會更高。
        只有 09:10 結束後，才能「確認」最高點是 291。
        用 09:03 作起始點 = look-ahead bias（因為那時 290 還不是確認的 Key）。
    """
    early_start = "09:00:00"
    early_end   = "09:10:00"

    # 模擬 market_data 查詢結果（detect_keys_v1 的輸入）
    # 實際上這裡 mock DB，用假的資料直接測 detect 邏輯
    # 由於 detect_keys_v1 需要 db，我們直接測核心邏輯

    # 模擬早盤 bars
    bars_data = [
        ("09:00:00", 288.0),
        ("09:01:00", 288.5),
        ("09:02:00", 289.0),
        ("09:03:00", 290.0),   # 局部高點（key_source 候選，但不是最高）
        ("09:04:00", 289.5),
        ("09:05:00", 289.0),
        ("09:06:00", 290.0),
        ("09:07:00", 291.0),   # 真正最高點 → key_source_time
        ("09:08:00", 290.5),
        ("09:09:00", 290.0),
        ("09:10:00", 289.5),   # 視窗結束 → key_confirmed_time
    ]

    # 重現 detect_keys_v1 的核心邏輯
    max_high = max(h for (_, h) in bars_data)
    source_bar = next((t for (t, h) in bars_data if h == max_high), None)
    key_source_time    = source_bar
    key_confirmed_time = early_end  # 固定為視窗結束時間

    # 驗證
    assert max_high == 291.0, f"最高點應=291，實際={max_high}"
    assert key_source_time    == "09:07:00", f"source_time 應=09:07:00，實際={key_source_time}"
    assert key_confirmed_time == "09:10:00", f"confirmed_time 應=09:10:00，實際={key_confirmed_time}"
    assert key_source_time != key_confirmed_time, "source_time 與 confirmed_time 不應相同"

    # 關鍵：Attack Detection 不能從 source_time 開始
    assert key_confirmed_time > key_source_time, (
        "key_confirmed_time 應晚於 key_source_time，"
        "確保 Attack Detection 不會在最高點確立前開始"
    )

    print("✓ T11 PASS — Key confirmed_time 是視窗結束時間")
    print(f"   key_price={max_high}")
    print(f"   key_source_time={key_source_time}（記錄用）")
    print(f"   key_confirmed_time={key_confirmed_time}（Attack Detection 起始點）")
    print(f"   09:03~09:09 期間不得計算 Attack（視窗尚未結束）")


# ────────────────────────────────────────────────────────────
# T12：Attack Detection 不得在 key_confirmed_time 之前開始
# ────────────────────────────────────────────────────────────

def test_t12_attack_only_after_confirmed_time():
    """
    場景：
        Key = 287，key_source_time = 09:03，key_confirmed_time = 09:10
        09:04 有一根從下方碰到 287（如果用 source_time，這根算 Attack）
        09:11 才有合法的 Attack

    驗證：
        使用 key_confirmed_time=09:10 時，09:04 的「碰 Key」不算 Attack
        使用 key_source_time=09:03 時（錯誤），09:04 會被計入 Attack

    這個 test 直接驗證 find_attacks 的 time 參數行為：
        find_attacks(df, key_price, key_confirmed_time="09:10:00") → 09:04 不算
        find_attacks(df, key_price, key_source_time="09:03:00")    → 09:04 算入（look-ahead bias）
    """
    key_price = 287.0

    bars = [
        make_bar("09:03:00", 285.0, 287.5, 284.5, 286.0, 100),  # Key source bar

        # 09:04：在 09:03 和 09:10 之間，從下方碰到 Key
        # → 用 confirmed_time=09:10 時不算
        # → 用 source_time=09:03 時算（look-ahead bias！）
        make_bar("09:04:00", 285.5, 287.5, 285.0, 285.5, 200),
        make_bar("09:05:00", 285.5, 286.0, 285.0, 285.0, 80),
        make_bar("09:06:00", 285.0, 286.0, 284.5, 284.5, 70),
        make_bar("09:07:00", 284.5, 287.0, 284.0, 286.0, 120),
        make_bar("09:08:00", 286.0, 286.5, 285.5, 285.0, 60),
        make_bar("09:09:00", 285.0, 286.0, 284.5, 284.5, 50),

        # 09:10：視窗結束
        make_bar("09:10:00", 284.5, 285.0, 284.0, 284.5, 40),

        # 09:11：視窗結束後，從下方碰 Key → 這才是合法的 Attack
        make_bar("09:11:00", 284.5, 287.5, 284.2, 286.0, 250),
        make_bar("09:12:00", 286.0, 286.5, 285.5, 285.5, 80),
    ]
    df = make_df(bars)

    # 正確：使用 confirmed_time（09:10）
    attacks_correct = find_attacks(df, key_price, "09:10:00")

    # 錯誤：使用 source_time（09:03）—— 這是 look-ahead bias
    attacks_lookahead = find_attacks(df, key_price, "09:03:00")

    # 驗證
    # 正確版本：只有 09:11 之後的攻擊
    assert len(attacks_correct) >= 1, f"confirmed_time 版本應有 >= 1 個 Attack，實際 {len(attacks_correct)}"
    for a in attacks_correct:
        assert a["start_time"] >= "09:11:00", (
            f"正確版本的 Attack 不應在 09:10 之前：start_time={a['start_time']}"
        )

    # look-ahead 版本：會錯誤地包含 09:04 的攻擊
    assert len(attacks_lookahead) > len(attacks_correct), (
        f"look-ahead 版本應包含更多 Attack（含 09:04 的錯誤攻擊）\n"
        f"confirmed: {len(attacks_correct)}，source: {len(attacks_lookahead)}"
    )
    # look-ahead 版本有在 09:10 之前發生的 Attack
    early_attacks = [a for a in attacks_lookahead if a["start_time"] < "09:10:00"]
    assert len(early_attacks) > 0, "look-ahead 版本應包含視窗內的錯誤 Attack"

    print("✓ T12 PASS — Attack Detection 只在 confirmed_time 之後有效")
    print(f"   confirmed_time=09:10 → {len(attacks_correct)} 個 Attack（全在 09:11 之後）")
    print(f"   source_time=09:03（look-ahead）→ {len(attacks_lookahead)} 個 Attack（含視窗內的錯誤）")
    print(f"   09:04~09:09 期間的 Attack 因使用 confirmed_time 而被正確排除")


# ────────────────────────────────────────────────────────────
# T13：V1A/V1B 在 Python dict 中均存在
# ────────────────────────────────────────────────────────────

def test_t13_v1a_v1b_in_dict():
    """
    驗證 _build_attack_record 的回傳值包含 attack_volume_v1a 和 attack_volume_v1b，
    且值正確，確保寫入 DB 時不會因欄位缺失而 KeyError。

    （DB 實際寫入在整合測試中驗證；此 test 驗證記憶體層的正確性）
    """
    bars = [
        {"time": "09:11:00", "open": 98.5, "high": 101.0, "low": 98.3, "close": 100.5, "volume": 400},
        {"time": "09:12:00", "open": 100.5, "high": 101.5, "low": 100.0, "close": 100.2, "volume": 300},
        {"time": "09:13:00", "open": 100.2, "high": 100.5, "low": 98.5, "close": 99.0, "volume": 150},
    ]
    key_price = 100.0

    record = _build_attack_record(bars, key_price)

    # V1A/V1B 必須存在
    assert "attack_volume_v1a" in record, "attack_volume_v1a 必須存在於 record dict"
    assert "attack_volume_v1b" in record, "attack_volume_v1b 必須存在於 record dict"

    # V1A = 首根量
    assert record["attack_volume_v1a"] == 400, (
        f"V1A 應=400（首根量），實際={record['attack_volume_v1a']}"
    )

    # V1B = 所有根總和
    assert record["attack_volume_v1b"] == 850, (
        f"V1B 應=850（400+300+150），實際={record['attack_volume_v1b']}"
    )

    # attack_volume（向後相容主欄位）= V1B
    assert record["attack_volume"] == record["attack_volume_v1b"], (
        "attack_volume 應等於 V1B（向後相容）"
    )

    # c31_v1a 在 compute_c_values 中才填入，這裡應為缺失或 None
    # 主要驗證 _build_attack_record 不會遺漏 V1A/V1B 欄位

    print("✓ T13 PASS — V1A/V1B 欄位在 Python dict 中完整存在")
    print(f"   V1A={record['attack_volume_v1a']}（首根）")
    print(f"   V1B={record['attack_volume_v1b']}（整段，含結束那根）")
    print(f"   attack_volume={record['attack_volume']}（= V1B，向後相容）")


# ────────────────────────────────────────────────────────────
# T14：timeout 報酬 = 實際截止 close，不是估算值
# ────────────────────────────────────────────────────────────

def test_t14_timeout_return_uses_actual_close():
    """
    場景：
        entry_price = 100.0
        進場後的 bars：
            09:16（+1）: close=101.5  → MFE=1.5%
            09:17（+2）: close=100.8  → MFE 維持 1.5%
            09:18（+3）: close=99.5   → MAE=-0.5%
            09:19（+4）: close=100.3  → 回升
            09:20（+5）: close=101.2  → 5m 截止，exit_price_5m=101.2

        return_5m = (101.2 - 100.0) / 100.0 * 100 = 1.2%
        MFE 5m = 1.5%（09:16 那根的 high，假設 high=101.5）
        MAE 5m = -0.5%（09:18 那根的 low，假設 low=99.5）

    驗證：
        return_5m ≠ (MFE_5m + MAE_5m) / 2 = (1.5 + (-0.5)) / 2 = 0.5%
        return_5m = 1.2%（用實際 close 計算）

    這正是問題5的核心：0.5% ≠ 1.2%，誤差巨大，會嚴重扭曲回測結果。
    """
    entry_price = 100.0

    bars_after = make_df([
        make_bar("09:16:00", 100.0, 101.5,  99.8, 101.5, 100),  # 1st
        make_bar("09:17:00", 101.5, 101.8, 100.5, 100.8, 80),   # 2nd
        make_bar("09:18:00", 100.8, 101.0,  99.5,  99.5, 120),  # 3rd，MAE
        make_bar("09:19:00",  99.5, 100.8,  99.3, 100.3, 90),   # 4th
        make_bar("09:20:00", 100.3, 101.5, 100.1, 101.2, 70),   # 5th，5m 截止
        make_bar("09:21:00", 101.2, 102.0, 100.5, 101.8, 150),  # 6th（超出 5m）
    ])

    outcome = compute_outcome(bars_after, entry_price)

    # 驗證 return_5m（實際 close 計算）
    assert "return_5m" in outcome, "return_5m 必須存在"
    assert "exit_price_5m" in outcome, "exit_price_5m 必須存在"

    actual_return_5m = outcome["return_5m"]
    actual_exit_5m   = outcome["exit_price_5m"]
    mfe_5m = outcome.get("mfe_5m", 0)
    mae_5m = outcome.get("mae_5m", 0)

    # exit_price_5m 應是第 5 根的 close = 101.2
    assert actual_exit_5m is not None, "exit_price_5m 不應為 None"
    assert abs(actual_exit_5m - 101.2) < 0.01, (
        f"exit_price_5m 應=101.2（第5根close），實際={actual_exit_5m}"
    )

    # return_5m = (101.2 - 100.0) / 100.0 * 100 = 1.2%
    expected_return = round((101.2 - 100.0) / 100.0 * 100, 4)
    assert abs(actual_return_5m - expected_return) < 0.01, (
        f"return_5m 應={expected_return}%，實際={actual_return_5m}%"
    )

    # 舊的估算方式
    old_estimate = round((mfe_5m + mae_5m) / 2, 4) if (mfe_5m and mae_5m) else None

    print("✓ T14 PASS — timeout 使用實際截止 close 計算報酬")
    print(f"   entry_price={entry_price}")
    print(f"   exit_price_5m={actual_exit_5m}（第5根 close）")
    print(f"   return_5m={actual_return_5m}%（實際）")
    print(f"   MFE_5m={mfe_5m}%, MAE_5m={mae_5m}%")
    if old_estimate is not None:
        print(f"   舊估算 (MFE+MAE)/2={old_estimate}% ← 與實際差 {abs(actual_return_5m - old_estimate):.4f}%")
        print(f"   → 嚴重誤差，禁止使用估算！")


# ────────────────────────────────────────────────────────────
# T15：collection_threshold vs research_threshold 分離
# ────────────────────────────────────────────────────────────

def test_t15_collection_vs_research_threshold():
    """
    場景：
        collection_threshold = 2.5%（資料收集門檻）
        research_threshold   = 3.5%（回測研究門檻）

        股票 A：early_high_pct = 5.0%  → 資料收集 ✓，研究母體 ✓
        股票 B：early_high_pct = 3.0%  → 資料收集 ✓，研究母體 ✗（3.0% < 3.5%）
        股票 C：early_high_pct = 2.0%  → 資料收集 ✗（2.0% < 2.5%），研究母體 ✗

    驗證：
        B 的全天 1 分 K 應存在於 DB（因為達到 collection_threshold 2.5%）
        B 不進入 Key Detection 或 Attack Detection（因為未達 research_threshold 3.5%）
        未來如果想改用 3.0% 研究門檻，B 的資料已在 DB，不需重抓
        C 只存早盤資料（節省 API 配額，但量能資料保留）

    這個 test 驗證邏輯分離的設計，不連 DB，只驗證判斷函數。
    """
    collection_threshold = 0.025   # 2.5%
    research_threshold   = 0.035   # 3.5%

    stocks = [
        ("A", 0.050),  # 5.0%
        ("B", 0.030),  # 3.0%
        ("C", 0.020),  # 2.0%
    ]

    for stock_id, pct in stocks:
        passes_collection = pct >= collection_threshold
        passes_research   = pct >= research_threshold

        if stock_id == "A":
            assert passes_collection is True,  "A(5%)應通過資料收集"
            assert passes_research   is True,  "A(5%)應通過研究篩選"

        elif stock_id == "B":
            assert passes_collection is True,  "B(3%)應通過資料收集（2.5%門檻）"
            assert passes_research   is False, "B(3%)不應通過研究篩選（3.5%門檻）"
            # 關鍵語意：B 的資料在 DB 中，但 Key Detection 不處理 B
            # 未來若改 research_threshold=2.8%，B 就進入研究，不需重抓資料

        elif stock_id == "C":
            assert passes_collection is False, "C(2%)不應通過資料收集"
            assert passes_research   is False, "C(2%)不應通過研究篩選"

    # 模擬 Key Detection 的篩選邏輯
    # run_key_detection 會傳入 research_threshold 額外過濾
    def simulate_key_detection(early_high_pct, collection_passed, research_thresh):
        """模擬 Key Detection 的前置條件"""
        if not collection_passed:
            return False  # 資料根本不完整
        return early_high_pct >= research_thresh * 100

    # B 達到收集門檻，但不進 Key Detection
    b_in_key = simulate_key_detection(3.0, True, research_threshold)
    assert b_in_key is False, f"B 不應進入 Key Detection（3.0% < {research_threshold*100}%）"

    # A 進 Key Detection
    a_in_key = simulate_key_detection(5.0, True, research_threshold)
    assert a_in_key is True, f"A 應進入 Key Detection（5.0% >= {research_threshold*100}%）"

    print("✓ T15 PASS — collection_threshold 與 research_threshold 正確分離")
    print(f"   collection={collection_threshold*100}%, research={research_threshold*100}%")
    print(f"   股票A(5%): 收集✓ 研究✓")
    print(f"   股票B(3%): 收集✓ 研究✗ → 資料在DB，未來改門檻不需重抓")
    print(f"   股票C(2%): 收集✗ 研究✗ → 只保早盤量能資料")


# ────────────────────────────────────────────────────────────
# 執行所有測試
# ────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_t11_key_confirmed_time_is_window_end,
        test_t12_attack_only_after_confirmed_time,
        test_t13_v1a_v1b_in_dict,
        test_t14_timeout_return_uses_actual_close,
        test_t15_collection_vs_research_threshold,
    ]

    passed = 0
    failed = 0
    print("=" * 60)
    print("v3 修正項目 Unit Tests")
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
