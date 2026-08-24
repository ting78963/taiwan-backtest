"""
Data Service
============
兩階段抓取流程：

Stage 1（1 次 request）：
  全市場當日日線 → 篩出漲幅 >= 4% AND 成交量 >= 4000 張的 candidates

Stage 2（candidates × 2 次）：
  逐支取昨收 + 前5日量（TaiwanStockPrice，傳 data_id）
  逐支取 1 分 K（TaiwanStockKBar，傳 data_id）

Request 數：1 + candidates × 2（約 61~201 次）
"""

import logging
import time
from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import text

from .finmind_fetcher import fetch_candidates, fetch_prev_context, fetch_1min_kbar
from .inventory import mark_date_done, mark_date_error, get_missing_dates
from events.volume_ratio import compute_volume_ratio

logger = logging.getLogger(__name__)

EARLY_START = "09:00:00"
EARLY_END   = "09:10:00"

DEFAULT_PRICE_THRESHOLD  = 0.04
DEFAULT_MIN_VOLUME_ZHANG = 4000


def _upsert_market_data(db: Session, rows: list[dict]):
    if not rows:
        return
    db.execute(text("""
        INSERT INTO market_data
            (date, stock_id, time, open, high, low, close, volume)
        SELECT
            unnest(CAST(:dates AS date[])), unnest(CAST(:sids AS varchar[])),
            unnest(CAST(:times AS time[])),
            unnest(CAST(:opens AS numeric[])), unnest(CAST(:highs AS numeric[])),
            unnest(CAST(:lows AS numeric[])), unnest(CAST(:closes AS numeric[])),
            unnest(CAST(:vols AS bigint[]))
        ON CONFLICT (date, stock_id, time) DO NOTHING
    """), {
        "dates":  [r["date"]               for r in rows],
        "sids":   [r["stock_id"]           for r in rows],
        "times":  [str(r["time"])          for r in rows],
        "opens":  [r.get("open")           for r in rows],
        "highs":  [r.get("high")           for r in rows],
        "lows":   [r.get("low")            for r in rows],
        "closes": [r.get("close")          for r in rows],
        "vols":   [int(r.get("volume") or 0) for r in rows],
    })
    db.commit()


def _upsert_daily_context(
    db, stock_id, target_date,
    prev_close, prev_day_volume, prev5_volumes,
    early_high, early_high_time, early_high_pct,
    cumulative_vol, volume_ratio,
    price_threshold: float = 0.04,
):
    db.execute(text("""
        INSERT INTO daily_context (
            date, stock_id, prev_close, prev_day_volume,
            early_high_price, early_high_time, early_high_pct,
            early_volume, cumulative_volume_at_0910,
            volume_ratio_at_0910, volume_ratio,
            passes_price_filter, passes_volume_filter
        ) VALUES (
            :date, :sid, :pc, :pdv,
            :ehp, :eht, :ehpct,
            :cv, :cv, :vr, :vr,
            :pp, TRUE
        )
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
        "ehp": early_high, "eht": early_high_time, "ehpct": early_high_pct,
        "cv": cumulative_vol, "vr": volume_ratio,
        "pp": early_high_pct is not None and early_high_pct >= price_threshold * 100,
    })
    db.commit()


def fetch_and_store_single_date(
    db: Session,
    target_date: date,
    price_threshold: float = DEFAULT_PRICE_THRESHOLD,
    min_volume_zhang: int  = DEFAULT_MIN_VOLUME_ZHANG,
) -> dict:
    logger.info(f"[FETCH DATE START] date={target_date} threshold={price_threshold*100:.0f}% min_vol={min_volume_zhang}張")
    stats = {"fetched": 0, "skipped": 0, "errors": 0, "candidates": 0}

    # Stage 1：全市場日線篩選（1 次 request）
    logger.info(f"[FETCH STEP] date={target_date} step=market_screen")
    candidates_df = fetch_candidates(
        target_date,
        min_change_pct   = price_threshold * 100,
        min_volume_zhang = min_volume_zhang,
    )

    if candidates_df is None:
        logger.error(f"[FETCH ERROR] date={target_date} step=market_screen error=API失敗")
        mark_date_error(db, target_date, "全市場日線 API 失敗")
        return {"total": 0, **stats}

    if candidates_df.empty:
        logger.info(f"[FETCH STEP] date={target_date} step=no_candidates")
        mark_date_done(db, target_date, 0, 0, 0, price_threshold)
        return {"total": 0, **stats}

    total = len(candidates_df)
    stats["candidates"] = total
    logger.info(f"[FETCH STEP] date={target_date} step=candidates_found count={total}")

    # Stage 2：逐支 candidate 抓昨收 + KBar
    for _, row in candidates_df.iterrows():
        stock_id = str(row["stock_id"])

        try:
            # 2a：逐支取昨收 + 前5日量（TaiwanStockPrice 傳 data_id）
            logger.info(f"[FETCH STEP] date={target_date} step=prev_context stock_id={stock_id}")
            ctx = fetch_prev_context(stock_id, target_date)
            time.sleep(0.1)

            if ctx is None or ctx["prev_close"] <= 0:
                stats["skipped"] += 1
                continue

            prev_close      = ctx["prev_close"]
            prev_day_volume = ctx["prev_day_volume"]
            prev5_volumes   = ctx["prev5_day_volumes"]

            # 2b：取 1 分 K
            logger.info(f"[FETCH STEP] date={target_date} step=kbar stock_id={stock_id}")
            df = fetch_1min_kbar(stock_id, target_date)
            time.sleep(0.1)

            if df is None or df.empty:
                stats["skipped"] += 1
                continue

            # 早盤資料
            df_e = df[
                (df["time"].astype(str) >= EARLY_START) &
                (df["time"].astype(str) <= EARLY_END)
            ]

            if df_e.empty:
                early_high      = prev_close
                early_high_time = None
                early_high_pct  = 0.0
                cumulative_vol  = 0
            else:
                early_high      = float(df_e["high"].max())
                early_high_time = str(df_e.loc[df_e["high"].idxmax(), "time"])
                early_high_pct  = round((early_high / prev_close - 1) * 100, 4) if prev_close else 0
                cumulative_vol  = int(df_e["volume"].sum())

            volume_ratio = compute_volume_ratio(cumulative_vol, prev5_volumes)

            # 寫入 market_data（全天）
            rows_to_insert = [
                {"date": r["date"], "stock_id": stock_id, "time": r["time"],
                 "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
                 "close": r.get("close"), "volume": r.get("volume", 0)}
                for _, r in df.iterrows()
            ]
            _upsert_market_data(db, rows_to_insert)

            # 寫入 daily_context
            _upsert_daily_context(
                db, stock_id, target_date,
                prev_close, prev_day_volume, prev5_volumes,
                early_high, early_high_time, early_high_pct,
                cumulative_vol, volume_ratio,
                price_threshold,
            )

            stats["fetched"] += 1
            logger.info(
                f"[FETCH CANDIDATE DONE] date={target_date} stock_id={stock_id} "
                f"prev_close={prev_close} early_high={early_high} pct={early_high_pct:.1f}% vr={volume_ratio}"
            )

        except Exception as e:
            logger.error(f"[FETCH ERROR] date={target_date} stock_id={stock_id} error={e}")
            stats["errors"] += 1
            continue

    mark_date_done(db, target_date, stats["fetched"], stats["skipped"], stats["errors"], price_threshold)
    logger.info(f"[FETCH DATE DONE] date={target_date} candidates={stats['candidates']} fetched={stats['fetched']}")
    return {"total": total, **stats}


def fetch_missing_dates(
    db: Session,
    date_from: date,
    date_to: date,
    price_threshold: float = DEFAULT_PRICE_THRESHOLD,
    min_volume_zhang: int  = DEFAULT_MIN_VOLUME_ZHANG,
) -> dict:
    logger.info(f"[FETCH MISSING DATES] from={date_from} to={date_to}")
    missing = get_missing_dates(db, date_from, date_to, price_threshold)

    if not missing:
        logger.info("[FETCH MISSING DATES] count=0")
        return {"missing_dates": 0, "fetched_dates": 0, "results": []}

    logger.info(f"[FETCH MISSING DATES] count={len(missing)} dates={[str(d) for d in missing]}")
    results = []
    for d in missing:
        r = fetch_and_store_single_date(db, d, price_threshold, min_volume_zhang)
        results.append({"date": str(d), **r})

    logger.info(f"[FETCH TASK COMPLETED] results={results}")
    return {"missing_dates": len(missing), "fetched_dates": len(results), "results": results}
