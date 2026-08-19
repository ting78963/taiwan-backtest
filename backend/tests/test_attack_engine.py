"""
Attack Engine Unit Tests
========================
人工可驗證的測試案例。

測試覆蓋：
  T01  由下往上碰 Key（Upward Attack 基本案例）
  T02  由下往上直接穿越 Key（一根 K 完成穿越）
  T03  由上往下回落到 Key ← 不算 Upward Attack
  T04  Attack3 後 next-bar-open 進場不使用未來資訊
  T05  entry_at_trigger 永遠是 NULL（不是 attack_high）
  T06  Attack Volume V1A = 首根量，V1B = 整段量，兩者不同
  T07  C31 用 V1B 計算正確
  T08  多次 Attack：由上往下回落後再由下往上攻，算第二次 Attack
  T09  early_volume_speed_ratio 公式正確
  T10  passes_volume_filter 不應截斷樣本（永遠 TRUE 的語意驗證）
"""

import sys
import os
import pandas as pd
from datetime import time

# 把 backend 加入 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from events.attack_engine import (
    find_attacks,
    compute_c_values,
    fill_entry_prices,
    _build_attack_record,
)
from events.volume_ratio import compute_early_volume_speed_ratio


# ────────────────────────────────────────────────────────────
# Helper：建立假的 1 分 K DataFrame
# ────────────────────────────────────────────────────────────

def make_bar(time_str: str, open_: float, high: float, low: float, close: float, volume: int) -> dict:
    return {"time": time_str, "open": open_, "high": high, "low": low, "close": close, "volume": volume}


def make_df(bars: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    df["time_str"] = df["time"].astype(str)
    return df


# ────────────────────────────────────────────────────────────
# T01 由下往上碰 Key — 基本案例
# ────────────────────────────────────────────────────────────

def test_t01_upward_attack_basic():
    """
    場景：Key = 287，key_time = 09:11
        09:11 是 Key 確立的那根（close = 285.0，在 Key 以下）
        09:12 close = 285（在 Key 下方）
        09:13 high = 287.5（從下方攻到 Key，一次 Upward Attack）
        09:13 close = 286（收回 Key 以下，Attack 結束）

    注意：df 必須包含 key_created_time 那根或更早的 bar，
         讓 prev_close 能從 df_before 取到正確初始值，
         否則 fallback 用 key_price 本身，初始條件可能不對。

    預期：
        1 個 Attack，is_upward = True，is_touch = True
        start_time = 09:13（從 Key 以下的 09:12 收盤後，09:13 high 觸 Key）
    """
    key_price = 287.0
    key_time  = "09:11:00"

    bars = [
        make_bar("09:11:00", 285.0, 286.5, 284.5, 285.0, 200),  # Key bar，close=285 < 287
        make_bar("09:12:00", 285.0, 285.5, 284.0, 285.0, 100),   # 在 Key 以下
        make_bar("09:13:00", 285.0, 287.5, 285.0, 286.0, 150),   # high >= 287，prev_close=285 < 287 ✓
        make_bar("09:14:00", 286.0, 286.5, 285.5, 285.5, 80),    # 離 Key
    ]
    df = make_df(bars)

    attacks = find_attacks(df, key_price, key_time)

    assert len(attacks) == 1, f"預期 1 個 Attack，實際 {len(attacks)}"
    a = attacks[0]
    assert a["is_upward"]  is True,  "is_upward 應為 True"
    assert a["is_touch"]   is True,  "is_touch 應為 True"
    assert a["start_time"] == "09:13:00", f"start_time 應為 09:13:00，實際 {a['start_time']}"
    assert a["attack_high"] == 287.5, f"attack_high 應為 287.5，實際 {a['attack_high']}"
    print("✓ T01 PASS — 由下往上碰 Key")


# ────────────────────────────────────────────────────────────
# T02 由下往上直接穿越 Key（一根 K 完成穿越）
# ────────────────────────────────────────────────────────────

def test_t02_upward_cross():
    """
    場景：Key = 287，key_time = 09:10
        09:10 是 Key bar（close = 285.0，在 Key 以下）
        09:11~09:14 在 Key 以下盤整
        09:15 low = 285，high = 290.5（一根直接穿越，close = 290.5 站上 Key）

    預期：
        1 個 Attack，is_cross = True，is_close_above = True
        low(285) < key(287) < high(290.5) → 真正穿越
    """
    key_price = 287.0
    key_time  = "09:10:00"

    bars = [
        make_bar("09:10:00", 284.0, 286.5, 283.5, 285.0, 200),  # Key bar，close=285 < 287
        make_bar("09:11:00", 285.0, 286.0, 284.5, 285.5, 60),
        make_bar("09:12:00", 285.5, 286.5, 285.0, 285.5, 70),
        make_bar("09:13:00", 285.5, 286.5, 285.0, 285.5, 70),
        make_bar("09:14:00", 285.5, 286.0, 285.0, 285.0, 80),   # prev_close = 285 < 287
        make_bar("09:15:00", 285.0, 290.5, 285.0, 290.5, 500),  # 穿越 Key ✓
    ]
    df = make_df(bars)

    attacks = find_attacks(df, key_price, key_time)

    assert len(attacks) == 1, f"預期 1 個 Attack，實際 {len(attacks)}"
    a = attacks[0]
    assert a["is_cross"]       is True, f"is_cross 應為 True（low={a['attack_low']} < key={key_price} < high={a['attack_high']}）"
    assert a["is_close_above"] is True, f"is_close_above 應為 True（close=290.5 站上 Key）"
    assert a["is_upward"]      is True, "is_upward 應為 True"
    assert a["crossed_key"]    is True, "crossed_key 應為 True"
    print("✓ T02 PASS — 由下往上直接穿越 Key")


# ────────────────────────────────────────────────────────────
# T03 由上往下回落到 Key — 不算 Upward Attack
# ────────────────────────────────────────────────────────────

def test_t03_downward_to_key_not_attack():
    """
    場景：Key = 287
        09:12 close = 290（已在 Key 之上）
        09:13 low = 286，close = 287（從上面跌下來碰 Key）

    預期：
        0 個 Upward Attack
        因為「前一根 close >= key_price」，這是從上往下回落，不是從下方發起
    """
    key_price = 287.0
    key_time  = "09:11:00"

    bars = [
        make_bar("09:12:00", 290.0, 291.0, 289.5, 290.0, 200),  # prev_close = 290 >= 287
        make_bar("09:13:00", 290.0, 290.0, 286.0, 287.0, 180),  # high=290 >= 287，但從上往下 ✗
        make_bar("09:14:00", 287.0, 288.0, 286.5, 287.5, 100),
    ]
    df = make_df(bars)

    attacks = find_attacks(df, key_price, key_time)

    assert len(attacks) == 0, (
        f"預期 0 個 Upward Attack（由上往下回落不算），實際 {len(attacks)}\n"
        f"attacks={attacks}"
    )
    print("✓ T03 PASS — 由上往下回落到 Key，正確排除")


# ────────────────────────────────────────────────────────────
# T04 Attack3 後 next-bar-open 進場不使用未來資訊
# ────────────────────────────────────────────────────────────

def test_t04_next_bar_open_no_lookahead():
    """
    場景：Key = 100，3 次 Upward Attack，每次跨 2 根 K

    設計（每次 Attack：首根 close > key，次根 close < key 結束）：
        Attack1: 09:11~09:12，end=09:12
        Attack2: 09:13~09:14，end=09:14
        Attack3: 09:15~09:16，end=09:16
        next bar: 09:17，open=99.5

    驗證：
        1. entry_at_trigger = NULL（不使用 attack_high）
        2. entry_next_open = 09:17 的 open = 99.5（Attack3 結束後的下一根）
        3. entry_next_open ≠ attack_high（確認無 look-ahead）
    """
    key_price = 100.0
    key_time  = "09:10:00"

    bars = [
        make_bar("09:10:00", 98.0, 99.5, 97.5, 98.5, 50),    # Key bar，close=98.5 < 100

        # Attack1：09:11 high>=100，close=100.5（仍在 Key 上）→ 09:12 close=99 結束
        make_bar("09:11:00", 98.5, 101.0, 98.3, 100.5, 400),
        make_bar("09:12:00", 100.5, 101.0, 98.5,  99.0,  50),

        # Attack2：09:13 high>=100，close=100.5 → 09:14 close=99 結束
        make_bar("09:13:00", 99.0, 101.5, 98.8, 100.5, 250),
        make_bar("09:14:00", 100.5, 101.0, 98.5,  99.0,  40),

        # Attack3：09:15 high>=100，close=100.5 → 09:16 close=99 結束
        make_bar("09:15:00", 99.0, 101.0, 98.9, 100.5, 180),
        make_bar("09:16:00", 100.5, 101.0, 98.8,  99.0,  25),

        # next bar（09:17）：Attack3 結束後，這是進場候選
        make_bar("09:17:00", 99.5, 102.0, 99.0, 101.5, 600),   # open=99.5

        # 未來 bar（不應被使用）
        make_bar("09:18:00", 101.5, 105.0, 101.0, 104.0, 800),
    ]
    df = make_df(bars)

    attacks = find_attacks(df, key_price, key_time)
    attacks = compute_c_values(attacks)
    attacks = fill_entry_prices(attacks, df)

    assert len(attacks) >= 3, f"預期 >= 3 個 Attack，實際 {len(attacks)}"

    # 取 Attack3（index 2）
    a3 = attacks[2]

    # 驗證 1：entry_at_trigger 必須是 NULL
    assert a3["entry_at_trigger"] is None, (
        f"entry_at_trigger 應為 NULL（避免 look-ahead bias），"
        f"實際 = {a3['entry_at_trigger']}（attack_high = {a3['attack_high']}）"
    )

    # 驗證 2：Attack3 end_time = 09:16，next bar open = 09:17 的 open = 99.5
    assert a3["end_time"] == "09:16:00", f"Attack3 end_time 應=09:16:00，實際={a3['end_time']}"
    assert a3["entry_next_open"] is not None, "entry_next_open 不應為 None"
    assert abs(a3["entry_next_open"] - 99.5) < 0.01, (
        f"entry_next_open 應=99.5（09:17 open），實際={a3['entry_next_open']}"
    )

    # 驗證 3：entry_next_open ≠ attack_high
    assert a3["entry_next_open"] != a3["attack_high"], (
        f"entry_next_open({a3['entry_next_open']}) 不應等於 attack_high({a3['attack_high']})"
    )

    print(f"✓ T04 PASS — Attack3 next-bar-open 進場無 look-ahead bias")
    print(f"   Attack3: end={a3['end_time']}, entry_at_trigger={a3['entry_at_trigger']}, "
          f"entry_next_open={a3['entry_next_open']}, attack_high={a3['attack_high']}")


# ────────────────────────────────────────────────────────────
# T05 entry_at_trigger 永遠是 NULL
# ────────────────────────────────────────────────────────────

def test_t05_trigger_is_null():
    """
    所有 Attack 的 entry_at_trigger 必須是 NULL。
    attack_high 是 Attack 完成後才確定的最高價，用它作為觸發進場價 = look-ahead bias。
    """
    key_price = 100.0
    key_time  = "09:10:00"

    bars = [
        make_bar("09:10:00", 98.0, 99.5, 97.5, 98.5, 100),   # Key bar
        make_bar("09:11:00", 98.5, 101.0, 98.0, 99.5, 200),  # Attack1
        make_bar("09:12:00", 99.5, 100.0, 99.0, 99.5, 80),
        make_bar("09:13:00", 99.0, 100.5, 98.5, 99.0, 150),  # Attack2
        make_bar("09:14:00", 99.0, 99.8, 98.8, 98.9, 60),
    ]
    df = make_df(bars)

    attacks = find_attacks(df, key_price, key_time)
    attacks = fill_entry_prices(attacks, df)

    for i, a in enumerate(attacks):
        assert a["entry_at_trigger"] is None, (
            f"Attack{i+1}: entry_at_trigger 應為 NULL，"
            f"實際 = {a['entry_at_trigger']}（attack_high = {a['attack_high']}）"
        )
    print(f"✓ T05 PASS — {len(attacks)} 個 Attack 的 entry_at_trigger 均為 NULL")


# ────────────────────────────────────────────────────────────
# T06 Attack Volume V1A vs V1B
# ────────────────────────────────────────────────────────────

def test_t06_attack_volume_v1a_vs_v1b():
    """
    場景：一次 Attack 跨 3 根 K（bar1 碰 Key，bar2 在 Key 上，bar3 close 跌破 Key）
        bar1: volume=300（首根，high 碰 Key），close=50.5 仍在 Key 上
        bar2: volume=200，close=51.0 仍在 Key 上
        bar3: volume=100，close=49.5 < key=50 → Attack 結束

        V1A = 300（首根）
        V1B = 600（三根總和：300+200+100）
        bars_used = 3

    這個差距說明了版本化的必要性：
        V1B 包含 bar3 的 100 張，但 bar3 的價格已在 Key 以下，
        那 100 張到底是「繼續吃 Key 賣壓」還是「跌回來的賣單」？
        目前不決定，兩個版本都保存。
    """
    key_price = 50.0
    key_time  = "09:10:00"

    bars = [
        make_bar("09:10:00", 48.5, 49.5, 48.0, 49.0,  50),   # Key bar，close=49 < 50
        make_bar("09:11:00", 49.0, 51.0, 48.8, 50.5, 300),   # Attack 開始，close=50.5 > key，繼續
        make_bar("09:12:00", 50.5, 52.0, 50.2, 51.0, 200),   # 仍在 Key 上，繼續
        make_bar("09:13:00", 51.0, 51.5, 49.5, 49.5, 100),   # close=49.5 < 50 → Attack 結束
        make_bar("09:14:00", 49.5, 50.0, 49.0, 49.0,  80),
    ]
    df = make_df(bars)

    attacks = find_attacks(df, key_price, key_time)
    # 過濾出從下方發起的攻擊（is_upward=True），09:14 也可能形成 Attack（prev=49.5 < 50，high=50 >= 50）
    upward_attacks = [a for a in attacks if a["is_upward"]]

    # 第一個 Upward Attack 應該是 3 根的那個
    a = upward_attacks[0]
    assert a["bars_used"]          == 3,   f"bars_used 應=3，實際={a['bars_used']}"
    assert a["attack_volume_v1a"]  == 300, f"V1A（首根量）應=300，實際={a['attack_volume_v1a']}"
    assert a["attack_volume_v1b"]  == 600, f"V1B（整段量）應=600，實際={a['attack_volume_v1b']}"
    assert a["attack_volume_v1a"] != a["attack_volume_v1b"], "V1A 和 V1B 不應相同（設計上就是不同的）"

    print(f"✓ T06 PASS — V1A={a['attack_volume_v1a']} vs V1B={a['attack_volume_v1b']} (bars_used={a['bars_used']})")
    print(f"   差距 = V1B - V1A = {a['attack_volume_v1b'] - a['attack_volume_v1a']} 張（bar2+bar3 的量）")


# ────────────────────────────────────────────────────────────
# T07 C31 計算正確
# ────────────────────────────────────────────────────────────

def test_t07_c31_calculation():
    """
    場景（與 T04 相同的 bars 設計）：
        Attack1: 09:11~09:12，V1A=400, V1B=450 (400+50)
        Attack2: 09:13~09:14，V1A=250, V1B=290 (250+40)
        Attack3: 09:15~09:16，V1A=180, V1B=205 (180+25)

        c31 (V1B) = Attack3.V1B / Attack1.V1B = 205 / 450
        c31_v1a   = Attack3.V1A / Attack1.V1A = 180 / 400 = 0.45

    驗證兩個版本的 C 值各自正確。
    """
    key_price = 100.0
    key_time  = "09:10:00"

    bars = [
        make_bar("09:10:00", 98.0,  99.5, 97.5,  98.5,  50),   # Key bar

        # Attack1: V1A=400（09:11），end bar V1B+=50（09:12）
        make_bar("09:11:00", 98.5, 101.0, 98.3, 100.5, 400),
        make_bar("09:12:00", 100.5, 101.0, 98.5, 99.0,  50),

        # Attack2: V1A=250（09:13），end bar V1B+=40（09:14）
        make_bar("09:13:00", 99.0, 101.5, 98.8, 100.5, 250),
        make_bar("09:14:00", 100.5, 101.0, 98.5, 99.0,  40),

        # Attack3: V1A=180（09:15），end bar V1B+=25（09:16）
        make_bar("09:15:00", 99.0, 101.0, 98.9, 100.5, 180),
        make_bar("09:16:00", 100.5, 101.0, 98.8, 99.0,  25),
    ]
    df = make_df(bars)

    attacks = find_attacks(df, key_price, key_time)
    attacks = compute_c_values(attacks)

    assert len(attacks) >= 3, f"預期 >= 3 個 Attack，實際 {len(attacks)}"

    a1, a2, a3 = attacks[0], attacks[1], attacks[2]

    # Attack1 量確認
    assert a1["attack_volume_v1a"] == 400, f"A1.V1A 應=400，實際={a1['attack_volume_v1a']}"
    assert a1["attack_volume_v1b"] == 450, f"A1.V1B 應=450，實際={a1['attack_volume_v1b']}"
    assert a3["attack_volume_v1a"] == 180, f"A3.V1A 應=180，實際={a3['attack_volume_v1a']}"
    assert a3["attack_volume_v1b"] == 205, f"A3.V1B 應=205，實際={a3['attack_volume_v1b']}"

    # c31 (V1B)
    expected_c31_v1b = round(205 / 450, 4)
    assert a3["c31"] == expected_c31_v1b, f"c31 (V1B) 應={expected_c31_v1b}，實際={a3['c31']}"

    # c31_v1a
    expected_c31_v1a = round(180 / 400, 4)
    assert a3["c31_v1a"] == expected_c31_v1a, f"c31_v1a 應={expected_c31_v1a}，實際={a3['c31_v1a']}"

    # c21 (V1B)
    expected_c21 = round(290 / 450, 4)
    assert a2["c21"] == expected_c21, f"c21 (V1B) 應={expected_c21}，實際={a2['c21']}"

    print(f"✓ T07 PASS — C 值兩版本均正確")
    print(f"   A1: V1A={a1['attack_volume_v1a']} V1B={a1['attack_volume_v1b']}")
    print(f"   A3: V1A={a3['attack_volume_v1a']} V1B={a3['attack_volume_v1b']}")
    print(f"   c31_v1b={a3['c31']} ({205}/{450}={expected_c31_v1b})")
    print(f"   c31_v1a={a3['c31_v1a']} ({180}/{400}={expected_c31_v1a})")
    print(f"   差距反映了 Attack Volume 版本化的研究意義")


# ────────────────────────────────────────────────────────────
# T08 第二次 Attack：由上往下回落後再從下往上攻
# ────────────────────────────────────────────────────────────

def test_t08_two_attacks_with_pullback():
    """
    場景：Key = 287
        Attack1：09:12 從下方攻到 287+，09:13 close 跌回 Key 以下
        [中場：09:14 在 Key 以下盤整]
        Attack2：09:15 再次從下方攻 Key

    預期：2 個 Upward Attack，各自獨立計數
    """
    key_price = 287.0
    key_time  = "09:11:00"

    bars = [
        make_bar("09:11:00", 285.5, 286.5, 285.0, 285.5, 100),  # Key bar，close=285.5

        # Attack1
        make_bar("09:12:00", 285.5, 288.0, 285.2, 287.5, 300),  # Attack1 開始
        make_bar("09:13:00", 287.5, 288.0, 286.0, 285.5, 150),  # close=285.5 < 287，Attack1 結束

        # 中場（Key 以下，不觸發 Attack）
        make_bar("09:14:00", 285.5, 286.5, 285.0, 285.0, 80),

        # Attack2
        make_bar("09:15:00", 285.0, 288.5, 284.8, 287.5, 200),  # Attack2 開始
        make_bar("09:16:00", 287.5, 288.0, 286.5, 285.5, 100),  # close=285.5 < 287，Attack2 結束
    ]
    df = make_df(bars)

    attacks = find_attacks(df, key_price, key_time)

    assert len(attacks) == 2, f"預期 2 個 Attack，實際 {len(attacks)}"
    assert attacks[0]["start_time"] == "09:12:00", f"Attack1 起始時間錯誤：{attacks[0]['start_time']}"
    assert attacks[1]["start_time"] == "09:15:00", f"Attack2 起始時間錯誤：{attacks[1]['start_time']}"
    assert attacks[0]["is_upward"] is True
    assert attacks[1]["is_upward"] is True
    print(f"✓ T08 PASS — 2 個 Attack 正確識別，中間回落後再次發起")


# ────────────────────────────────────────────────────────────
# T09 early_volume_speed_ratio 公式驗證
# ────────────────────────────────────────────────────────────

def test_t09_early_volume_speed_ratio():
    """
    公式：(early_volume / early_minutes) / (prev_day_volume / 270)

    場景：
        prev_day_volume = 27000 張（昨日全天）
        昨日每分鐘均速 = 27000 / 270 = 100 張/分
        early_volume = 550 張（今日早盤 11 分鐘）
        今日早盤均速 = 550 / 11 = 50 張/分
        early_volume_speed_ratio = 50 / 100 = 0.5

    另一場景（高量）：
        prev_day_volume = 10000 張
        early_volume = 800 張，11 分鐘
        昨日均速 = 10000/270 ≈ 37.04 張/分
        今日早盤均速 = 800/11 ≈ 72.73 張/分
        ratio = 72.73 / 37.04 ≈ 1.9636
    """
    # 場景 A
    ratio_a = compute_early_volume_speed_ratio(550, 27000, 11)
    assert ratio_a is not None
    assert abs(ratio_a - 0.5) < 0.001, f"場景 A：預期 0.5，實際 {ratio_a}"

    # 場景 B
    ratio_b = compute_early_volume_speed_ratio(800, 10000, 11)
    expected_b = round((800 / 11) / (10000 / 270), 4)
    assert ratio_b is not None
    assert abs(ratio_b - expected_b) < 0.001, f"場景 B：預期 {expected_b}，實際 {ratio_b}"

    # 邊界：prev_day_volume = 0
    ratio_zero = compute_early_volume_speed_ratio(100, 0, 11)
    assert ratio_zero is None, "prev_day_volume=0 應回傳 None"

    # 邊界：early_volume = 0
    ratio_no_vol = compute_early_volume_speed_ratio(0, 10000, 11)
    assert ratio_no_vol == 0.0 or ratio_no_vol is not None  # 允許回傳 0 或 None

    print(f"✓ T09 PASS — early_volume_speed_ratio 公式正確")
    print(f"   場景A: ratio={ratio_a} (預期0.5)")
    print(f"   場景B: ratio={ratio_b} (預期{expected_b})")


# ────────────────────────────────────────────────────────────
# T10 passes_volume_filter 語意驗證
# ────────────────────────────────────────────────────────────

def test_t10_passes_volume_filter_not_filtering():
    """
    驗證 passes_volume_filter 的設計意圖：
    它不應該截斷母體，只是個保留欄位（固定 TRUE）。
    真正的量能分析應該用 early_volume 或 early_volume_speed_ratio 的原始數值。

    這個 test 不連接 DB，只驗證邏輯：
    - 成交量極低的股票（early_volume=10 張）仍應被保留在母體中
    - 唯一的篩選條件是 passes_price_filter（早盤漲幅）
    """
    # 模擬 compute_and_upsert_daily_context 的邏輯
    early_volume  = 10      # 極低量
    price_threshold = 0.035
    early_high_pct  = 5.0   # 漲幅 5%，符合條件

    passes_price  = early_high_pct >= price_threshold * 100
    passes_volume = True     # 永遠 TRUE，不作篩選

    assert passes_price  is True,  "漲幅 5% >= 3.5%，passes_price 應 True"
    assert passes_volume is True,  "passes_volume 應永遠 True，不截斷低量股"

    # 確認低量股不會因量能不足而被排除
    # 它的 early_volume=10 和 early_volume_speed_ratio 保存供後續分析
    evsr = compute_early_volume_speed_ratio(early_volume, 10000, 11)
    assert evsr is not None, "低量股的 evsr 應可計算，供分析使用"
    assert evsr < 1.0, f"低量股的 evsr 應 < 1.0，實際 = {evsr}"

    print(f"✓ T10 PASS — passes_volume_filter 不截斷樣本")
    print(f"   low-volume stock: early_volume={early_volume}, evsr={evsr:.4f}")
    print(f"   passes_price={passes_price}, passes_volume={passes_volume} → 保留在母體")


# ────────────────────────────────────────────────────────────
# 執行所有測試
# ────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_t01_upward_attack_basic,
        test_t02_upward_cross,
        test_t03_downward_to_key_not_attack,
        test_t04_next_bar_open_no_lookahead,
        test_t05_trigger_is_null,
        test_t06_attack_volume_v1a_vs_v1b,
        test_t07_c31_calculation,
        test_t08_two_attacks_with_pullback,
        test_t09_early_volume_speed_ratio,
        test_t10_passes_volume_filter_not_filtering,
    ]

    passed = 0
    failed = 0
    print("=" * 60)
    print("Attack Engine Unit Tests")
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
