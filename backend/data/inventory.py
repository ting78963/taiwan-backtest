"""
Data Inventory
==============
管理「哪些日期的原始資料已存在 DB」。

前端「抓取資料」按下前，系統先呼叫 missing_dates()：
  - 已完整抓取 (fetch_status='done') → 跳過
  - 尚未存在 / partial / error → 加入待抓清單

這樣確保：
  1. 不重複抓 FinMind（節省 API 配額）
  2. 失敗的日期可以補抓
  3. 前端 RUN 回測時，底層資料一定完整
"""

import logging
from datetime import date, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)


def get_fetched_dates(
    db: Session,
    date_from: date,
    date_to: date,
    requested_collection_threshold: float = 0.025,
) -> set[date]:
    """
    回傳指定範圍內「資料已完整、且收集門檻足夠」的日期集合。

    判斷條件（兩者同時成立才能跳過）：
        1. fetch_status = 'done'
        2. collection_threshold <= requested_collection_threshold
           → 當時抓取所用的門檻 <= 現在要求的門檻，表示現在需要的股票範圍已包含在舊資料中

    反向情境（必須重抓）：
        舊資料：collection_threshold = 3.5%（只存了漲幅 >= 3.5% 的股票）
        現在要求：requested = 2.5%（需要漲幅 >= 2.5% 的股票）
        → 2.5%~3.5% 的股票全天 K 不在 DB 中，必須重抓

    正向情境（可以跳過）：
        舊資料：collection_threshold = 2.5%（已存了漲幅 >= 2.5% 的股票）
        現在要求：requested = 3.5%
        → 3.5% 的股票一定在舊資料中（因為 3.5% >= 2.5%），可以跳過
    """
    rows = db.execute(text("""
        SELECT date FROM data_inventory
        WHERE date >= :df AND date <= :dt
          AND fetch_status = 'done'
          AND (
            collection_threshold IS NULL                          -- 舊格式相容（NULL = 視為已覆蓋）
            OR collection_threshold <= :ct
          )
    """), {"df": date_from, "dt": date_to, "ct": requested_collection_threshold}).fetchall()
    return {r[0] for r in rows}


def get_missing_dates(
    db: Session,
    date_from: date,
    date_to: date,
    requested_collection_threshold: float = 0.025,
) -> list[date]:
    """
    回傳指定範圍內「需要補抓」的交易日清單。

    跳過條件：fetch_status='done' AND collection_threshold <= requested（見 get_fetched_dates）
    必須補抓：未抓取、抓取失敗、或舊資料收集門檻高於現在要求
    """
    fetched = get_fetched_dates(db, date_from, date_to, requested_collection_threshold)
    missing = []
    current = date_from
    while current <= date_to:
        if current.weekday() < 5 and current not in fetched:
            missing.append(current)
        current += timedelta(days=1)
    return missing


def mark_date_done(
    db: Session,
    target_date: date,
    stocks_fetched: int = 0,
    stocks_skipped: int = 0,
    stocks_error: int = 0,
    collection_threshold: float = 0.025,
):
    """標記某日期抓取完成，記錄使用的 collection_threshold"""
    db.execute(text("""
        INSERT INTO data_inventory
            (date, fetch_status, stocks_fetched, stocks_skipped, stocks_error,
             collection_threshold, fetched_at)
        VALUES
            (:date, 'done', :fetched, :skipped, :error,
             :ct, NOW())
        ON CONFLICT (date) DO UPDATE SET
            fetch_status         = 'done',
            stocks_fetched       = EXCLUDED.stocks_fetched,
            stocks_skipped       = EXCLUDED.stocks_skipped,
            stocks_error         = EXCLUDED.stocks_error,
            collection_threshold = EXCLUDED.collection_threshold,
            fetched_at           = NOW()
    """), {
        "date": target_date, "fetched": stocks_fetched,
        "skipped": stocks_skipped, "error": stocks_error,
        "ct": collection_threshold,
    })
    db.commit()


def mark_date_error(db: Session, target_date: date, reason: str = ""):
    """標記某日期抓取失敗"""
    db.execute(text("""
        INSERT INTO data_inventory (date, fetch_status)
        VALUES (:date, 'error')
        ON CONFLICT (date) DO UPDATE SET fetch_status = 'error', fetched_at = NOW()
    """), {"date": target_date})
    db.commit()
    logger.warning(f"[Inventory] {target_date} marked as error: {reason}")


def get_inventory_summary(db: Session, date_from: date, date_to: date) -> dict:
    """回傳指定範圍的庫存摘要（供前端顯示）"""
    rows = db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE fetch_status = 'done')    AS done,
            COUNT(*) FILTER (WHERE fetch_status = 'partial') AS partial,
            COUNT(*) FILTER (WHERE fetch_status = 'error')   AS error,
            SUM(stocks_fetched) AS total_stocks,
            MIN(date) AS earliest,
            MAX(date) AS latest
        FROM data_inventory
        WHERE date >= :df AND date <= :dt
    """), {"df": date_from, "dt": date_to}).fetchone()

    return {
        "done": int(rows[0] or 0),
        "partial": int(rows[1] or 0),
        "error": int(rows[2] or 0),
        "total_stocks": int(rows[3] or 0),
        "earliest": str(rows[4]) if rows[4] else None,
        "latest": str(rows[5]) if rows[5] else None,
    }
