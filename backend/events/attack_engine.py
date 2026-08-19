"""
Attack Engine
=============
核心引擎：辨識「買方由 Key 下方向上攻擊 Key」事件。

核心市場語意：
    Attack 不是「碰Key」，是「從 Key 下方往上攻 Key 的完整過程」。
    例如 Key = 287：
        285 → 286 → 287 → 288  ✓ Upward Attack（從下方往上穿越）
        290 → 289 → 288 → 287  ✗ 不算（由上往下回落到 Key）

多版本 Attack 判定（全部保存，不互斥，回測時再選）：
    is_touch:       high >= key_price（任何方向碰到 Key）
    is_upward:      由 Key 下方發起，high >= key_price（主版本）
    is_cross:       low < key_price AND high > key_price（真正穿越）
    is_close_above: 攻擊結束分鐘的 close >= key_price

Attack Volume 版本化（問題5修正）：
    V1A：首根碰 Key 的 bar 成交量（最保守，只算第一根確認接觸的量）
    V1B：整段攻擊區間（從發起到 close 跌回 Key 以下）所有 bar 成交量總和
         這是原有定義，但包含突破後繼續上漲的量，可能高估「吃掉 Key 賣壓」的量
    兩者都保存在 attack_events，讓 C 值可以用任一版本計算，不在此決定哪個「正確」。

進場候選價（問題4修正）：
    entry_at_trigger：NULL，1 分 K 無法確定「觸發那一刻」的成交價，
                      填 attack_high 會產生 look-ahead bias（使用了 Attack 結束後才知道的最高價）。
    entry_at_bar_close：攻擊結束那根 K 的 close（當下已知，可執行）
    entry_next_open：下一根 K 的 open（當下已知，可執行）
    entry_next_close：下一根 K 的 close（需等待，可執行但較晚）

Early window（問題6）：
    ATTACK_SEARCH_START 已改為由 key_created_time 動態決定，不寫死。
    search_end 預設 13:30，可由參數覆蓋。
"""

import logging
from datetime import date
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

# 搜尋上限（全天）
ATTACK_SEARCH_END = "13:30:00"


# ────────────────────────────────────────────────────────────
# 工具函數
# ────────────────────────────────────────────────────────────

def load_market_bars(db: Session, target_date: date, stock_id: str) -> pd.DataFrame:
    """從 DB 讀取指定股票當日完整 1 分 K，依時間排序。"""
    rows = db.execute(text("""
        SELECT time, open, high, low, close, volume
        FROM market_data
        WHERE date = :date AND stock_id = :stock_id
        ORDER BY time
    """), {"date": target_date, "stock_id": stock_id}).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time_str"] = df["time"].astype(str)
    return df


# ────────────────────────────────────────────────────────────
# Attack Detection 核心
# ────────────────────────────────────────────────────────────

def find_attacks(
    df: pd.DataFrame,
    key_price: float,
    key_created_time: str,
    search_end: str = ATTACK_SEARCH_END,
) -> list[dict]:
    """
    在 key_created_time 之後，找出所有 Upward Key Attack 事件。

    演算法：
        逐根掃描。當「上一根 close < key_price」且「本根 high >= key_price」，
        判定為一次 Upward Attack 的開始（從下方發起攻擊）。
        持續累積 bar，直到 close 回落 key_price 以下，Attack 結束。

    不算 Attack 的情形：
        - 上一根 close >= key_price（已在 Key 之上，不是從下方發起）
        - 本根 high < key_price（沒碰到 Key）

    Returns:
        list of attack dicts，已包含 attack_volume_v1a 與 attack_volume_v1b。
    """
    # 只搜尋 Key 確立後的資料
    df_search = df[
        (df["time_str"] > key_created_time) &
        (df["time_str"] <= search_end)
    ].copy().reset_index(drop=True)

    if df_search.empty:
        return []

    # 取 Key 確立那根的 close，作為初始「前一根 close」
    df_before = df[df["time_str"] <= key_created_time]
    prev_close = float(df_before.iloc[-1]["close"]) if not df_before.empty else key_price

    attacks = []
    in_attack = False
    attack_bars: list[dict] = []

    for i, row in df_search.iterrows():
        bar_open  = float(row["open"]  or 0)
        bar_high  = float(row["high"]  or 0)
        bar_low   = float(row["low"]   or 0)
        bar_close = float(row["close"] or 0)
        bar_vol   = int(row["volume"]  or 0)
        bar_time  = str(row["time"])
        is_last   = (i == len(df_search) - 1)

        if not in_attack:
            # 判斷是否開始一次 Upward Attack
            # 條件 A：上一根 close 在 Key 以下（必須從下方發起）
            # 條件 B：本根 high 碰到或突破 Key
            if prev_close < key_price and bar_high >= key_price:
                in_attack = True
                attack_bars = [_make_bar_dict(bar_time, bar_open, bar_high, bar_low, bar_close, bar_vol)]
                # 修正：Attack 開始那根可能同時是最後一根，或 close 已跌回 Key 以下
                # 必須在這裡做一次結束判斷，否則 Attack 永遠不會被 append
                if bar_close < key_price or is_last:
                    attack = _build_attack_record(attack_bars, key_price)
                    attacks.append(attack)
                    in_attack = False
                    attack_bars = []
        else:
            attack_bars.append(_make_bar_dict(bar_time, bar_open, bar_high, bar_low, bar_close, bar_vol))

            # 攻擊結束：close 回落 Key 以下，或已是最後一根
            if bar_close < key_price or is_last:
                attack = _build_attack_record(attack_bars, key_price)
                attacks.append(attack)
                in_attack = False
                attack_bars = []

        prev_close = bar_close

    return attacks


def _make_bar_dict(time, open_, high, low, close, vol) -> dict:
    return {"time": time, "open": open_, "high": high, "low": low, "close": close, "volume": vol}


def _build_attack_record(bars: list[dict], key_price: float) -> dict:
    """
    從一組 bar 組合出一次 Attack 的完整記錄。

    Attack Volume 版本（問題5）：
        attack_volume_v1a：僅首根碰 Key 的 bar 成交量
            → 最保守的「吃賣壓用量」估計
            → 首根 bar 中，price 從 key_price 以下推到 key_price，這根的量最能代表直接吃掉 Key 賣壓的量
        attack_volume_v1b：整段 bars 成交量總和（原有定義）
            → 包含突破後繼續上漲的量，可能高估
        兩者都保存，C 值計算時可選任一版本。

    進場候選價（問題4）：
        entry_at_trigger = NULL
            → attack_high 是整段 Attack 結束後才確定的最高價，不是即時已知的觸發價
            → 用 attack_high 作為進場價 = look-ahead bias
            → 1 分 K 無法還原「第一次碰到 key_price 那一刻」的成交價，故標 NULL
        entry_at_bar_close：攻擊結束那根 close（當下已知）
        entry_next_open / entry_next_close：由 fill_entry_prices 填入
    """
    start_bar = bars[0]
    end_bar   = bars[-1]

    attack_high = max(b["high"]  for b in bars)
    attack_low  = min(b["low"]   for b in bars)
    bars_used   = len(bars)

    # Attack Volume V1A：僅首根（最保守）
    attack_volume_v1a = bars[0]["volume"]

    # Attack Volume V1B：整段總和（含突破後繼續漲的量）
    attack_volume_v1b = sum(b["volume"] for b in bars)

    # 多版本判定（全部保存，不互斥）
    # is_upward = True 由呼叫端保證（find_attacks 已確認從下方發起）
    is_touch       = attack_high >= key_price
    is_upward      = True
    is_cross       = start_bar["low"] < key_price and attack_high > key_price
    is_close_above = float(end_bar["close"]) >= key_price

    return {
        # 時間
        "start_time":           start_bar["time"],
        "end_time":             end_bar["time"],
        "bars_used":            bars_used,
        # 價格
        "start_price":          start_bar["open"],
        "key_price":            key_price,
        "attack_high":          attack_high,
        "attack_low":           attack_low,
        # 量能（兩個版本，不截斷）
        "attack_volume":        attack_volume_v1b,   # DB 主欄位：V1B（向後相容）
        "attack_volume_v1a":    attack_volume_v1a,   # 首根量（最保守）
        "attack_volume_v1b":    attack_volume_v1b,   # 整段量
        # 多版本判定
        "is_touch":             is_touch,
        "is_upward":            is_upward,
        "is_cross":             is_cross,
        "is_close_above":       is_close_above,
        # 突破
        "crossed_key":          attack_high > key_price,
        "closed_above_key":     is_close_above,
        "attack_high_above_key": round(attack_high - key_price, 2) if attack_high > key_price else 0.0,
        # 進場候選（trigger 標 NULL，避免 look-ahead bias）
        "entry_at_trigger":     None,   # 1分K無法確定觸發成交價，故 NULL
        "entry_at_bar_close":   float(end_bar["close"]),
        "entry_next_open":      None,   # 由 fill_entry_prices 填入
        "entry_next_close":     None,   # 由 fill_entry_prices 填入
        # 原始 bar 資料（保存供未來版本的 Attack Volume 演算法使用）
        "_bars":                bars,   # 不寫入 DB，只在記憶體中使用
    }


# ────────────────────────────────────────────────────────────
# C 值計算（量比衰減率）
# ────────────────────────────────────────────────────────────

def compute_c_values(attacks: list[dict]) -> list[dict]:
    """
    計算 C 值（量比衰減率）並注入每筆 attack。

    提供兩組 C 值：
        c_vXXa：基於 attack_volume_v1a（首根量，最保守）
        c_vXXb：基於 attack_volume_v1b（整段量）

    均保存原始比值（不截斷、不做是/否判斷）。
    DB 欄位 c21/c31/c32/c41 使用 V1B（向後相容），
    V1A 版本另存於 c31_v1a 等欄位（schema 需配合新增，或暫存在 dict 供 export 使用）。
    """
    vols_b = {i + 1: a["attack_volume_v1b"] for i, a in enumerate(attacks)}
    vols_a = {i + 1: a["attack_volume_v1a"] for i, a in enumerate(attacks)}

    for i, attack in enumerate(attacks):
        n  = i + 1
        v1b = vols_b.get(1)
        v1a = vols_a.get(1)

        # V1B 版本（DB 主欄位）
        attack["c21"] = _safe_ratio(vols_b.get(2), v1b) if n == 2 else None
        attack["c31"] = _safe_ratio(vols_b.get(3), v1b) if n >= 3 else None
        attack["c32"] = _safe_ratio(vols_b.get(3), vols_b.get(2)) if n == 3 else None
        attack["c41"] = _safe_ratio(vols_b.get(4), v1b) if n >= 4 else None

        # V1A 版本（首根量）
        attack["c31_v1a"] = _safe_ratio(vols_a.get(3), v1a) if n >= 3 else None
        attack["c41_v1a"] = _safe_ratio(vols_a.get(4), v1a) if n >= 4 else None

    return attacks


def _safe_ratio(numerator, denominator) -> float | None:
    """安全除法，分母為 None/0 時回傳 None。"""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round(numerator / denominator, 4)


# ────────────────────────────────────────────────────────────
# 進場候選價填入
# ────────────────────────────────────────────────────────────

def fill_entry_prices(attacks: list[dict], df: pd.DataFrame) -> list[dict]:
    """
    填入 entry_next_open 和 entry_next_close。
    使用攻擊結束那根 bar（end_time）的「下一根 K」，這是當下已知、可執行的進場價。

    entry_at_trigger：已在 _build_attack_record 設為 NULL，此處不覆蓋。
    """
    # 建立 time_str → 在 df 中的 positional index 映射
    time_to_pos = {str(row["time"]): pos for pos, (_, row) in enumerate(df.iterrows())}

    for attack in attacks:
        end_time = attack["end_time"]
        end_pos  = time_to_pos.get(end_time)

        if end_pos is not None and end_pos + 1 < len(df):
            next_bar = df.iloc[end_pos + 1]
            attack["entry_next_open"]  = float(next_bar["open"]  or 0)
            attack["entry_next_close"] = float(next_bar["close"] or 0)
        # 若 end_time 是最後一根，next_open/next_close 保持 None

    return attacks


# ────────────────────────────────────────────────────────────
# 寫入 DB
# ────────────────────────────────────────────────────────────

def upsert_attacks(
    db: Session,
    key_id: int,
    date_: date,
    stock_id: str,
    attacks: list[dict],
    attack_version: str = "V1",
):
    """
    將所有 attack 寫入 DB。
    UNIQUE constraint：(key_id, attack_version, attack_number)

    注意：attack dict 中的 "_bars" key 是記憶體用途，不寫入 DB。
    """
    for i, attack in enumerate(attacks):
        attack_number = i + 1
        db.execute(text("""
            INSERT INTO attack_events (
                key_id, date, stock_id, attack_version, attack_number,
                start_time, end_time, bars_used,
                start_price, key_price, attack_high, attack_low,
                attack_volume, attack_volume_v1a, attack_volume_v1b,
                is_touch, is_upward, is_cross, is_close_above,
                crossed_key, closed_above_key, attack_high_above_key,
                c21, c31, c32, c41, c31_v1a, c41_v1a,
                entry_at_trigger, entry_at_bar_close, entry_next_open, entry_next_close
            ) VALUES (
                :key_id, :date, :stock_id, :attack_version, :attack_number,
                :start_time, :end_time, :bars_used,
                :start_price, :key_price, :attack_high, :attack_low,
                :attack_volume, :attack_volume_v1a, :attack_volume_v1b,
                :is_touch, :is_upward, :is_cross, :is_close_above,
                :crossed_key, :closed_above_key, :attack_high_above_key,
                :c21, :c31, :c32, :c41, :c31_v1a, :c41_v1a,
                :entry_at_trigger, :entry_at_bar_close, :entry_next_open, :entry_next_close
            )
            ON CONFLICT (key_id, attack_version, attack_number) DO UPDATE SET
                start_time             = EXCLUDED.start_time,
                end_time               = EXCLUDED.end_time,
                bars_used              = EXCLUDED.bars_used,
                attack_volume          = EXCLUDED.attack_volume,
                attack_volume_v1a      = EXCLUDED.attack_volume_v1a,
                attack_volume_v1b      = EXCLUDED.attack_volume_v1b,
                attack_high            = EXCLUDED.attack_high,
                attack_low             = EXCLUDED.attack_low,
                is_touch               = EXCLUDED.is_touch,
                is_upward              = EXCLUDED.is_upward,
                is_cross               = EXCLUDED.is_cross,
                is_close_above         = EXCLUDED.is_close_above,
                crossed_key            = EXCLUDED.crossed_key,
                closed_above_key       = EXCLUDED.closed_above_key,
                attack_high_above_key  = EXCLUDED.attack_high_above_key,
                c21                    = EXCLUDED.c21,
                c31                    = EXCLUDED.c31,
                c32                    = EXCLUDED.c32,
                c41                    = EXCLUDED.c41,
                c31_v1a                = EXCLUDED.c31_v1a,
                c41_v1a                = EXCLUDED.c41_v1a,
                entry_at_trigger       = EXCLUDED.entry_at_trigger,
                entry_at_bar_close     = EXCLUDED.entry_at_bar_close,
                entry_next_open        = EXCLUDED.entry_next_open,
                entry_next_close       = EXCLUDED.entry_next_close
        """), {
            "key_id":           key_id,
            "date":             date_,
            "stock_id":         stock_id,
            "attack_version":   attack_version,
            "attack_number":    attack_number,
            "start_time":       attack["start_time"],
            "end_time":         attack["end_time"],
            "bars_used":        attack["bars_used"],
            "start_price":      attack["start_price"],
            "key_price":        attack["key_price"],
            "attack_high":      attack["attack_high"],
            "attack_low":       attack["attack_low"],
            "attack_volume":    attack["attack_volume"],
            "attack_volume_v1a": attack["attack_volume_v1a"],
            "attack_volume_v1b": attack["attack_volume_v1b"],
            "is_touch":         attack["is_touch"],
            "is_upward":        attack["is_upward"],
            "is_cross":         attack["is_cross"],
            "is_close_above":   attack["is_close_above"],
            "crossed_key":      attack["crossed_key"],
            "closed_above_key": attack["closed_above_key"],
            "attack_high_above_key": attack["attack_high_above_key"],
            "c21":     attack.get("c21"),
            "c31":     attack.get("c31"),
            "c32":     attack.get("c32"),
            "c41":     attack.get("c41"),
            "c31_v1a": attack.get("c31_v1a"),
            "c41_v1a": attack.get("c41_v1a"),
            "entry_at_trigger":   attack["entry_at_trigger"],
            "entry_at_bar_close": attack["entry_at_bar_close"],
            "entry_next_open":    attack.get("entry_next_open"),
            "entry_next_close":   attack.get("entry_next_close"),
        })
    db.commit()


# ────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────

def run_attack_detection(
    db: Session,
    target_date: date,
    key_version: str = "V1",
    attack_version: str = "V1",
    search_end: str = ATTACK_SEARCH_END,
) -> dict:
    """
    對指定日期所有 Key 執行 Attack Detection。

    key_version：從哪個版本的 key_events 讀取 Key
    attack_version：寫入 attack_events 時使用的版本標記
    search_end：Attack 搜尋的時間上限（預設全天 13:30）
    """
    keys = db.execute(text("""
        SELECT key_id, stock_id, key_price,
               COALESCE(key_confirmed_time, key_created_time) AS key_confirmed_time,
               key_source_time
        FROM key_events
        WHERE date = :date AND key_version = :kv
    """), {"date": target_date, "kv": key_version}).fetchall()

    stats = {"total_keys": len(keys), "total_attacks": 0, "errors": 0}

    for (key_id, stock_id, key_price, key_confirmed_time, key_source_time) in keys:
        try:
            df = load_market_bars(db, target_date, stock_id)
            if df.empty:
                continue

            # 使用 key_confirmed_time（視窗結束後才能確認 Key）
            # 不使用 key_source_time（最高點第一次出現，有 look-ahead bias）
            attacks = find_attacks(df, float(key_price), str(key_confirmed_time), search_end)
            attacks = compute_c_values(attacks)
            attacks = fill_entry_prices(attacks, df)

            upsert_attacks(db, key_id, target_date, stock_id, attacks, attack_version)
            stats["total_attacks"] += len(attacks)

            logger.info(
                f"  {stock_id} | Key={key_price} | {len(attacks)} attacks | "
                f"V1A={[a['attack_volume_v1a'] for a in attacks]} "
                f"V1B={[a['attack_volume_v1b'] for a in attacks]}"
            )

        except Exception as e:
            logger.error(f"Attack detection failed for {stock_id} on {target_date}: {e}")
            stats["errors"] += 1

    logger.info(f"[AttackEngine] {target_date} (key={key_version}, attack={attack_version}): {stats}")
    return stats
