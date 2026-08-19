"""
Data Service
============
抓取 FinMind → 寫入 PostgreSQL。

核心邏輯：
  1. 先查 data_inventory，只補抓缺少或 collection_threshold 不足的日期
  2. 每日成功完成後，寫入 data_inventory
  3. 前端 RUN 回測時，完全不觸發此模組

量比計算（在 compute_and_upsert_daily_context 中）：
  使用 volume_ratio.compute_volume_ratio()，對應前端 loadHotVolStocks 邏輯：
    obs_volume       = 09:00~09:10 累積量（張）
    prev_5d_volumes  = target_date 之前最近5個有效交易日的日成交量

  保存欄位：
    cumulative_volume_at_0910  = 09:00~09:10 累積量（原始值，永久保存）
    （price_pct_at_0910 已移除：它與 early_high_pct 完全相同，保留一個即可）
    volume_ratio_at_0910       = cumulative_volume_at_0910 / 前5日均量
"""

import logging
import time
from datetime import date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text

from .finmind_fetcher import fetch_1min_kbar, fetch_batch_daily_context
from .inventory import mark_date_done, mark_date_error, get_missing_dates
from events.volume_ratio import compute_volume_ratio

logger = logging.getLogger(__name__)

EARLY_START   = "09:00:00"
EARLY_END     = "09:10:00"
EARLY_MINUTES = 11  # 09:00~09:10 共 11 根

DEFAULT_PRICE_THRESHOLD = 0.025   # collection_threshold 預設 2.5%


def _get_prev5_volumes(db: Session, stock_id: str, before_date: date) -> list[int]:
    """取 before_date 之前最近5個有效交易日的日成交量（張）。"""
    rows = db.execute(text("""
        SELECT SUM(volume) AS day_vol
        FROM market_data
        WHERE stock_id = :sid AND date < :d
        GROUP BY date
        ORDER BY date DESC
        LIMIT 5
    """), {"sid": stock_id, "d": before_date}).fetchall()
    return [int(r[0]) for r in rows if r[0] is not None and r[0] > 0]


def compute_and_upsert_daily_context(
    db: Session,
    stock_id: str,
    target_date: date,
    prev_close: float,
    prev_day_volume: int,
    price_threshold: float,
):
    """從 market_data 計算 daily_context 並寫入。"""
    result = db.execute(text("""
        SELECT high, volume, time FROM market_data
        WHERE date = :date AND stock_id = :sid
          AND time >= :s AND time <= :e
        ORDER BY time
    """), {"date": target_date, "sid": stock_id, "s": EARLY_START, "e": EARLY_END}).fetchall()

    if not result:
        return

    early_high   = max(r[0] for r in result if r[0])
    # cumulative_volume_at_0910：09:00~09:10 的累積量（原始值，永久保存）
    cumulative_volume_at_0910 = sum(r[1] for r in result if r[1])
    early_high_time = next((str(r[2]) for r in result if r[0] == early_high), None)
    early_high_pct  = round((early_high / prev_close - 1) * 100, 4) if prev_close else None

    # 量比：使用前5日均量（對應前端 loadHotVolStocks）
    prev5_vols = _get_prev5_volumes(db, stock_id, target_date)
    volume_ratio_at_0910 = compute_volume_ratio(cumulative_volume_at_0910, prev5_vols)

    # passes_price_filter：是否達到 collection_threshold（唯一篩選條件）
    passes_price = bool(early_high_pct is not None and early_high_pct >= price_threshold * 100)

    db.execute(text("""
        INSERT INTO daily_context
            (date, stock_id, prev_close, prev_day_volume,
             early_high_price, early_high_time, early_high_pct,
             early_volume, cumulative_volume_at_0910,
             volume_ratio_at_0910, volume_ratio,
             passes_price_filter, passes_volume_filter)
        VALUES (:date, :sid, :pc, :pdv, :ehp, :eht, :ehpct,
                :ev, :cv0910, :vr0910, :vr,
                :pp, TRUE)
        ON CONFLICT (date, stock_id) DO UPDATE SET
            prev_close                = EXCLUDED.prev_close,
            prev_day_volume           = EXCLUDED.prev_day_volume,
            early_high_price          = EXCLUDED.early_high_price,
            early_high_time           = EXCLUDED.early_high_time,
            early_high_pct            = EXCLUDED.early_high_pct,
            early_volume              = EXCLUDED.early_volume,
            cumulative_volume_at_0910 = EXCLUDED.cumulative_volume_at_0910,
            volume_ratio_at_0910      = EXCLUDED.volume_ratio_at_0910,
            volume_ratio              = EXCLUDED.volume_ratio,
            passes_price_filter       = EXCLUDED.passes_price_filter
    """), {
        "date": target_date, "sid": stock_id,
        "pc": prev_close, "pdv": prev_day_volume,
        "ehp": early_high, "eht": early_high_time,
        "ehpct": early_high_pct,
        "ev":    cumulative_volume_at_0910,   # 向後相容舊欄位
        "cv0910": cumulative_volume_at_0910,
        "vr0910": volume_ratio_at_0910,
        "vr":     volume_ratio_at_0910,       # volume_ratio 向後相容主欄位
        "pp": passes_price,
    })
    db.commit()


def _upsert_market_data(db: Session, rows: list[dict]):
    if not rows:
        return
    db.execute(text("""
        INSERT INTO market_data (date, stock_id, time, open, high, low, close, volume)
        SELECT unnest(:dates::date[]), unnest(:sids::varchar[]), unnest(:times::time[]),
               unnest(:opens::numeric[]), unnest(:highs::numeric[]), unnest(:lows::numeric[]),
               unnest(:closes::numeric[]), unnest(:vols::bigint[])
        ON CONFLICT (date, stock_id, time) DO NOTHING
    """), {
        "dates":  [r["date"]            for r in rows],
        "sids":   [r["stock_id"]        for r in rows],
        "times":  [str(r["time"])       for r in rows],
        "opens":  [r.get("open")        for r in rows],
        "highs":  [r.get("high")        for r in rows],
        "lows":   [r.get("low")         for r in rows],
        "closes": [r.get("close")       for r in rows],
        "vols":   [int(r.get("volume") or 0) for r in rows],
    })
    db.commit()


def fetch_and_store_single_date(
    db: Session,
    target_date: date,
    price_threshold: float = DEFAULT_PRICE_THRESHOLD,
    progress_callback=None,
) -> dict:
    """抓取單一日期，存入 DB，計算量比等 context。"""
    logger.info(f"[DataService] 抓取 {target_date}")
    stats = {"fetched": 0, "skipped": 0, "errors": 0}

    import pandas as pd
    df_context = fetch_batch_daily_context(target_date)
    if df_context is None or df_context.empty:
        mark_date_error(db, target_date, "無法取得昨收資料")
        return {"total": 0, **stats}

    total = len(df_context)
    for i, row in df_context.iterrows():
        stock_id        = str(row["stock_id"])
        prev_close      = float(row.get("prev_close") or 0)
        prev_day_volume = int(row.get("prev_day_volume") or 0)

        if progress_callback:
            progress_callback(i, total, stock_id)
        if prev_close <= 0:
            stats["skipped"] += 1
            continue

        try:
            df = fetch_1min_kbar(stock_id, target_date)
            time.sleep(0.12)
            if df is None or df.empty:
                stats["skipped"] += 1
                continue

            df_e = df[(df["time"].astype(str) >= EARLY_START) & (df["time"].astype(str) <= EARLY_END)]
            if df_e.empty:
                stats["skipped"] += 1
                continue

            early_high_pct = (df_e["high"].max() / prev_close - 1) * 100
            passes_price   = early_high_pct >= price_threshold * 100

            target_df = df if passes_price else df_e
            rows_to_insert = [
                {"date": r["date"], "stock_id": stock_id, "time": r["time"],
                 "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
                 "close": r.get("close"), "volume": r.get("volume", 0)}
                for _, r in target_df.iterrows()
            ]
            _upsert_market_data(db, rows_to_insert)
            compute_and_upsert_daily_context(db, stock_id, target_date, prev_close, prev_day_volume, price_threshold)

            if passes_price:
                stats["fetched"] += 1
                logger.info(f"  ✓ {stock_id} +{early_high_pct:.1f}%")
            else:
                stats["skipped"] += 1

        except Exception as e:
            logger.error(f"  ✗ {stock_id}: {e}")
            stats["errors"] += 1

    mark_date_done(db, target_date, stats["fetched"], stats["skipped"], stats["errors"], price_threshold)
    logger.info(f"[DataService] {target_date}: {stats}")
    return {"total": total, **stats}


def fetch_missing_dates(
    db: Session,
    date_from: date,
    date_to: date,
    price_threshold: float = DEFAULT_PRICE_THRESHOLD,
    progress_callback=None,
) -> dict:
    """只抓缺少的日期。"""
    missing = get_missing_dates(db, date_from, date_to, price_threshold)
    if not missing:
        logger.info(f"[DataService] {date_from}~{date_to} 全部已有，跳過")
        return {"missing_dates": 0, "fetched_dates": 0, "results": []}

    logger.info(f"[DataService] 缺少 {len(missing)} 日，開始補抓")
    results = []
    for d in missing:
        r = fetch_and_store_single_date(db, d, price_threshold, progress_callback)
        results.append({"date": str(d), **r})
    return {"missing_dates": len(missing), "fetched_dates": len(results), "results": results}
