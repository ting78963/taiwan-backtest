"""
Key Detection Engine
====================
找出 Key Price = 09:00~09:10 早盤視窗內的最高實際成交價。

核心定義：
    key_price = early_high_price = MAX(high, 09:00~09:10)

    這就是那面「牆」：09:10 之後，市場如何一次又一次回去撞這個價格，
    就是我們研究的 Upward Key Attack。

    early_high_pct >= threshold（如 4%）只決定這面牆值不值得研究，
    不是牆本身。prev_close 不參與 Attack Detection。

時間區分（避免 look-ahead bias）：
    key_source_time    = early_high_price 第一次出現的 K 棒時間（記錄用）
    key_confirmed_time = 09:10（視窗結束，Attack Detection 的起始點）

    09:10 結束後才能確認最高點，才能開始辨識後續的 Attack。
    不能用 key_source_time 當起始點（那時視窗未結束，可能還有更高的 K 棒）。

例子：
    prev_close = 100
    09:04 high = 105 → key_source_time = 09:04
    09:10 視窗結束，確認 key_price = 105，key_confirmed_time = 09:10
    early_high_pct = +5%，達到門檻 → 進入研究母體
    09:11 起觀察市場是否回來攻 105
"""

import logging
from datetime import date

from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)


def detect_keys_v1(
    db: Session,
    target_date: date,
    stock_id: str,
    early_start: str = "09:00:00",
    early_end: str   = "09:10:00",
) -> dict | None:
    """
    V1 Key Detection：早盤視窗最高價作為 Key。

    Returns:
        dict 含 key_source_time 與 key_confirmed_time，或 None（無資料）
    """
    rows = db.execute(text("""
        SELECT time, high
        FROM market_data
        WHERE date = :date AND stock_id = :sid
          AND time >= :s AND time <= :e
        ORDER BY time
    """), {"date": target_date, "sid": stock_id, "s": early_start, "e": early_end}).fetchall()

    if not rows:
        return None

    # 找整個視窗的最高點及其「第一次出現」的時間
    max_high   = max(r[1] for r in rows if r[1] is not None)
    source_bar = next((r for r in rows if r[1] == max_high), None)

    if not max_high or not source_bar:
        return None

    key_source_time    = str(source_bar[0])   # 最高點第一次出現（記錄用）
    key_confirmed_time = early_end             # 視窗結束時間（Attack Detection 起始點）

    return {
        "date":              target_date,
        "stock_id":          stock_id,
        "key_version":       "V1",
        "key_price":         float(max_high),
        "key_source_time":   key_source_time,
        "key_confirmed_time": key_confirmed_time,
        "key_created_time":  key_confirmed_time,  # 向後相容
        "key_high":          float(max_high),
        "key_low":           float(max_high),
        "detection_basis":   (
            f"早盤最高價 {max_high} @ {key_source_time}（source），"
            f"視窗結束確認 @ {key_confirmed_time}（confirmed）"
        ),
    }


def upsert_key(db: Session, key: dict):
    """寫入 key_events，重複則更新。"""
    result = db.execute(text("""
        INSERT INTO key_events
            (date, stock_id, key_version, key_price,
             key_source_time, key_confirmed_time, key_created_time,
             key_high, key_low, detection_basis)
        VALUES
            (:date, :stock_id, :key_version, :key_price,
             :key_source_time, :key_confirmed_time, :key_created_time,
             :key_high, :key_low, :detection_basis)
        ON CONFLICT (date, stock_id, key_version, key_price) DO UPDATE SET
            key_source_time    = EXCLUDED.key_source_time,
            key_confirmed_time = EXCLUDED.key_confirmed_time,
            key_created_time   = EXCLUDED.key_created_time,
            key_high           = EXCLUDED.key_high,
            key_low            = EXCLUDED.key_low,
            detection_basis    = EXCLUDED.detection_basis
        RETURNING key_id
    """), key)
    key_id = result.fetchone()[0]
    db.commit()
    return key_id


def run_key_detection(
    db: Session,
    target_date: date,
    key_version: str  = "V1",
    early_start: str  = "09:00:00",
    early_end: str    = "09:10:00",
    research_threshold: float = None,  # None = 只用 collection 門檻（passes_price_filter）
) -> dict:
    """
    對指定日期所有符合條件的股票執行 Key Detection。

    候選條件：passes_price_filter = TRUE（達到資料收集門檻）
    research_threshold：若指定，額外過濾 early_high_pct >= 該門檻
        → 資料收集門檻(collection)可能是 2.5%，研究門檻可能是 3.5%
        → 這樣「2.5%~3.5%」的股票資料保留但不進 Key Detection
    """
    if research_threshold is not None:
        candidates = db.execute(text("""
            SELECT stock_id FROM daily_context
            WHERE date = :date
              AND passes_price_filter = TRUE
              AND early_high_pct >= :rt
        """), {"date": target_date, "rt": research_threshold * 100}).fetchall()
    else:
        candidates = db.execute(text("""
            SELECT stock_id FROM daily_context
            WHERE date = :date
              AND passes_price_filter = TRUE
        """), {"date": target_date}).fetchall()

    stats = {"total": len(candidates), "found": 0, "skipped": 0}

    for (stock_id,) in candidates:
        try:
            key = detect_keys_v1(db, target_date, stock_id, early_start, early_end)
            if key:
                upsert_key(db, key)
                stats["found"] += 1
                logger.debug(
                    f"  {stock_id} | Key={key['key_price']} "
                    f"source={key['key_source_time']} confirmed={key['key_confirmed_time']}"
                )
            else:
                stats["skipped"] += 1
        except Exception as e:
            logger.error(f"Key detection failed for {stock_id} on {target_date}: {e}")
            stats["skipped"] += 1

    logger.info(f"[KeyEngine] {target_date} {key_version}: {stats}")
    return stats
