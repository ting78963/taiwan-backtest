"""
Taiwan Backtest System — FastAPI Backend
"""

import logging
import os
import time as time_module
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from db.database import get_db, init_db, SessionLocal, get_current_engine_versions
from data.data_service import fetch_missing_dates
from data.inventory import get_missing_dates, get_inventory_summary
from events.event_manager import (
    get_dates_needing_events, clear_events_for_date,
    mark_event_run_done, get_event_coverage,
)
from events.key_engine import run_key_detection
from events.attack_engine import run_attack_detection
from events.outcome_engine import run_outcome_engine
from backtest.backtest_engine import (
    create_run, run_backtest, generate_summary, update_run_status
)
from backtest.export_engine import (
    export_event_master, export_backtest_trades, df_to_csv_bytes
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 啟動時把卡住的 running task 清為 stale（重啟後舊 task 不再污染）
    for k in list(task_progress.keys()):
        if task_progress[k].get("status") == "running":
            task_progress[k]["status"] = "stale"
            task_progress[k]["message"] = "Service restarted, task interrupted"
    yield


app = FastAPI(title="Taiwan Backtest System", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory task registry ──────────────────────────────────────────
task_progress: dict = {}

def set_task(task_id: str, status: str, **kwargs):
    task_progress[task_id] = {"status": status, **kwargs}


# ── Schemas ──────────────────────────────────────────────────────────
class FetchRequest(BaseModel):
    date_from:             date
    date_to:               date
    collection_threshold:  float = Field(0.025, description=(
        "資料收集門檻（預設 2.5%）"
    ))
    min_volume_zhang:      int   = Field(4000, description="早盤最低成交量粗篩（張，預設4000）")
    force_refetch:         bool  = Field(False)
    # 研究達成率，不模擬損益
    min_volume_ratio:     Optional[float] = Field(None, description=(
        "量比最低門檻（volume_ratio_at_0910）。None=不限。"
    ))


class EventRequest(BaseModel):
    date_from:       date
    date_to:         date
    key_version:     str = "V1"
    attack_version:  str = "V1"
    outcome_version: str = "V1"
    force_rerun:     bool = False
    research_threshold: Optional[float] = Field(None)


class AnalyzeRequest(BaseModel):
    """
    條件探索器：五項研究條件 → 即時 aggregate → 10:00 前達成率矩陣。
    直接 JOIN attack_events + outcome_data + daily_context，不需先跑 backtest_trades。
    """
    date_from:          date
    date_to:            date
    entry_mode:         str   = "next_open"   # next_open（預設）/ bar_close / next_close
    # 五項研究條件（全部 Optional，None = 不篩選）
    min_attack_number:  Optional[int]   = None   # Attack 次數 >= N
    min_attack_volume:  Optional[int]   = None   # Attack Volume V1B >= N 張
    min_c31:            Optional[float] = None   # C31 >= X
    min_volume_ratio:   Optional[float] = None   # volume_ratio_at_0910 >= X
    min_early_high_pct: Optional[float] = None   # early_high_pct >= X%
    attack_version:     str   = "V1"
    outcome_version:    str   = "V1"


class BacktestRequest(BaseModel):
    run_name:            Optional[str]  = None
    date_from:           date
    date_to:             date
    attack_version:      str            = "V1"
    outcome_version:     str            = "V1"
    research_threshold:  Optional[float] = Field(0.035)
    intrabar_policy:     str  = Field("conservative")
    # 研究達成率，不模擬損益
    min_volume_ratio:    Optional[float] = Field(None)
    attack_definitions:  Optional[list[str]]   = None
    attack_numbers:      Optional[list[int]]   = None
    entry_modes:         Optional[list[str]]   = None
    tp_levels:           Optional[list[float]] = None
    sl_levels:           Optional[list[Optional[float]]] = None
    exit_times:          Optional[list[str]]   = None


# ── Health ───────────────────────────────────────────────────────────
@app.get("/debug/finmind_cols")
def debug_finmind_cols(target_date: str = "2026-08-18", db=Depends(get_db)):
    """臨時 debug：查看 FinMind TaiwanStockPrice 的實際欄位名稱。"""
    from data.finmind_fetcher import _request, FINMIND_TOKEN
    from datetime import date as date_cls, timedelta
    import requests as req_mod
    d = date_cls.fromisoformat(target_date)
    start = (d - timedelta(days=3)).strftime("%Y-%m-%d")
    end   = d.strftime("%Y-%m-%d")
    df = _request("TaiwanStockPrice", {"start_date": start, "end_date": end})
    if df is None or df.empty:
        return {"error": "empty", "columns": []}
    # 只回傳第一筆資料和欄位名稱
    return {
        "columns": list(df.columns),
        "sample": df.head(2).to_dict(orient="records"),
        "shape": list(df.shape),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Stats overview ───────────────────────────────────────────────────
@app.get("/api/stats/overview")
def stats_overview(db=Depends(get_db)):
    from sqlalchemy import text
    row = db.execute(text("""
        SELECT
            (SELECT COUNT(DISTINCT date) FROM market_data)                        AS data_days,
            (SELECT COUNT(*) FROM daily_context WHERE passes_price_filter)        AS candidates,
            (SELECT COUNT(*) FROM key_events)                                     AS keys,
            (SELECT COUNT(*) FROM attack_events)                                  AS attacks,
            (SELECT COUNT(*) FROM outcome_data)                                   AS outcomes,
            (SELECT COUNT(*) FROM backtest_runs WHERE status='done')              AS completed_runs,
            (SELECT COUNT(*) FROM backtest_trades)                                AS total_trades
    """)).fetchone()
    cols = ["data_days","candidates","keys","attacks","outcomes","completed_runs","total_trades"]
    return dict(zip(cols, row))


# ── Step 1: Fetch ─────────────────────────────────────────────────────
@app.post("/api/fetch")
async def fetch_data(req: FetchRequest, background_tasks: BackgroundTasks, db=Depends(get_db)):
    if not req.force_refetch:
        missing = get_missing_dates(db, req.date_from, req.date_to, req.collection_threshold)
        if not missing:
            return {
                "task_id": None,
                "message": f"✅ {req.date_from}~{req.date_to} 資料已完整，無需抓取",
                "missing_count": 0,
            }

    # 每次用 timestamp 確保 task_id 唯一，避免舊 running task 污染
    ts = int(time_module.time())
    task_id = f"fetch_{req.date_from}_{req.date_to}_{ts}"
    set_task(task_id, "running", message="開始補抓缺少的日期...")
    logger.info(f"[FETCH TASK START] task_id={task_id} start={req.date_from} end={req.date_to} threshold={req.collection_threshold}")

    def run():
        # ✅ 關鍵修正：background task 自己建立獨立 Session，不用 endpoint 的 db
        db2 = SessionLocal()
        try:
            logger.info(f"[FETCH TASK] task_id={task_id} background thread started")
            result = fetch_missing_dates(
                db2, req.date_from, req.date_to,
                req.collection_threshold,
                req.min_volume_zhang,
            )
            logger.info(f"[FETCH TASK COMPLETED] task_id={task_id} result={result}")
            set_task(task_id, "done", **result)
        except Exception as e:
            logger.exception(f"[FETCH ERROR] task_id={task_id} error={e}")
            set_task(task_id, "error", message=str(e))
        finally:
            db2.close()

    background_tasks.add_task(run)
    return {"task_id": task_id, "message": "資料補抓已啟動（背景執行）"}


# ── Step 2: Event Engine ─────────────────────────────────────────────
@app.post("/api/events/run")
async def run_events(req: EventRequest, background_tasks: BackgroundTasks, db=Depends(get_db)):
    ts = int(time_module.time())
    task_id = f"events_{req.date_from}_{req.date_to}_{ts}"
    set_task(task_id, "running", message="Event Engine 啟動中...")
    logger.info(f"[EVENT TASK START] task_id={task_id}")

    def run():
        db2 = SessionLocal()
        try:
            from datetime import timedelta
            versions = dict(key=req.key_version, attack=req.attack_version, outcome=req.outcome_version)
            dates_needed = get_dates_needing_events(
                db2, req.date_from, req.date_to, versions, req.force_rerun
            )
            if not dates_needed:
                logger.info(f"[EVENT TASK] task_id={task_id} 所有日期事件資料已是最新版本")
                set_task(task_id, "done", message="所有日期的事件資料已是最新版本，無需重跑", dates_processed=0)
                return

            stats_all = []
            for d in dates_needed:
                logger.info(f"[EVENT DATE START] task_id={task_id} date={d}")
                if req.force_rerun:
                    clear_events_for_date(db2, d, req.key_version, req.attack_version, req.outcome_version)
                k = run_key_detection(db2, d, req.key_version,
                                      research_threshold=req.research_threshold)
                a = run_attack_detection(db2, d, req.key_version, req.attack_version)
                o = run_outcome_engine(db2, d, req.attack_version, req.outcome_version)
                mark_event_run_done(db2, d, req.key_version, req.attack_version, req.outcome_version,
                                    k.get("found", 0), a.get("total_attacks", 0))
                stats_all.append({"date": str(d), "keys": k, "attacks": a, "outcomes": o})
                logger.info(f"[EVENT DATE DONE] date={d} keys={k.get('found',0)} attacks={a.get('total_attacks',0)}")

            set_task(task_id, "done", dates_processed=len(stats_all), results=stats_all)
            logger.info(f"[EVENT TASK COMPLETED] task_id={task_id} processed={len(stats_all)}")
        except Exception as e:
            logger.exception(f"[EVENT ERROR] task_id={task_id} error={e}")
            set_task(task_id, "error", message=str(e))
        finally:
            db2.close()

    background_tasks.add_task(run)
    return {"task_id": task_id, "message": "Event Engine 已啟動"}


# ── Step 3: Backtest ──────────────────────────────────────────────────
@app.post("/api/backtest/run")
async def run_backtest_api(req: BacktestRequest, background_tasks: BackgroundTasks, db=Depends(get_db)):
    versions = get_current_engine_versions(db)
    run_id = create_run(db, req.model_dump(), versions)

    ts = int(time_module.time())
    task_id = f"backtest_{run_id}_{ts}"
    set_task(task_id, "running", run_id=run_id)
    logger.info(f"[BACKTEST TASK START] task_id={task_id} run_id={run_id}")

    def run():
        db2 = SessionLocal()
        try:
            update_run_status(db2, run_id, "running")
            result = run_backtest(db2, run_id, req.date_from, req.date_to, req.model_dump())
            set_task(task_id, "done", run_id=run_id, **result)
            logger.info(f"[BACKTEST TASK COMPLETED] task_id={task_id} run_id={run_id}")
        except Exception as e:
            logger.exception(f"[BACKTEST ERROR] task_id={task_id} run_id={run_id} error={e}")
            update_run_status(db2, run_id, "error", error_msg=str(e))
            set_task(task_id, "error", run_id=run_id, message=str(e))
        finally:
            db2.close()

    background_tasks.add_task(run)
    return {"task_id": task_id, "run_id": run_id, "message": "回測已開始"}


# ── Task polling ──────────────────────────────────────────────────────
@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    return task_progress.get(task_id, {"status": "not_found"})


# ── Backtest runs list ────────────────────────────────────────────────
@app.get("/api/backtest/runs")
def list_runs(db=Depends(get_db)):
    from sqlalchemy import text
    rows = db.execute(text("""
        SELECT run_id, run_name, date_from, date_to, status,
               total_trades, engine_version, key_version, attack_version, outcome_version,
               created_at
        FROM backtest_runs ORDER BY created_at DESC LIMIT 50
    """)).fetchall()
    cols = ["run_id","run_name","date_from","date_to","status",
            "total_trades","engine_version","key_version","attack_version","outcome_version","created_at"]
    return [dict(zip(cols, r)) for r in rows]


# ── Summary ───────────────────────────────────────────────────────────
@app.get("/api/backtest/{run_id}/summary")
def get_summary(run_id: int, db=Depends(get_db)):
    df = generate_summary(db, run_id)
    return df.to_dict(orient="records") if not df.empty else []


# ── Inventory ─────────────────────────────────────────────────────────
@app.get("/api/inventory")
def get_inventory(date_from: date, date_to: date, db=Depends(get_db)):
    ct = 0.025
    summary = get_inventory_summary(db, date_from, date_to)
    missing = get_missing_dates(db, date_from, date_to, ct)
    return {**summary, "missing_count": len(missing), "missing_dates": [str(d) for d in missing[:30]]}


# ── Event coverage ────────────────────────────────────────────────────
@app.get("/api/events/coverage")
def get_coverage(date_from: date, date_to: date, db=Depends(get_db)):
    return get_event_coverage(db, date_from, date_to)


# ── Event Master ──────────────────────────────────────────────────────
@app.get("/api/events/master")
def get_event_master(
    date_from: date, date_to: date,
    attack_number: Optional[int] = None,
    min_volume_ratio: Optional[float] = None,
    db=Depends(get_db),
):
    df = export_event_master(db, date_from, date_to)
    if attack_number:
        df = df[df["attack_number"] == attack_number]
    if min_volume_ratio:
        df = df[df["volume_ratio_at_0910"] >= min_volume_ratio]
    return df.head(500).to_dict(orient="records")


# ── 條件探索器 ────────────────────────────────────────────────────────
@app.post("/api/analyze")
def analyze(req: AnalyzeRequest, db=Depends(get_db)):
    """
    五項研究條件 → 10:00 前各級 TP 達成率矩陣。
    直接 JOIN attack_events + outcome_data + daily_context，即時 aggregate。
    不需要先跑 Backtest RUN。
    entry_mode 必須指定，避免同一 attack 三列重複計算。
    TP 達成：first_plusXXX_time IS NOT NULL AND <= '09:59:00'
    """
    TP_COLS = [
        ("050", "+0.5%"), ("075", "+0.75%"), ("100", "+1.0%"),
        ("125", "+1.25%"), ("150", "+1.5%"), ("200", "+2.0%"),
        ("250", "+2.5%"), ("300", "+3.0%"), ("400", "+4.0%"), ("500", "+5.0%"),
    ]
    CUTOFF = "09:59:00"

    tp_select = ", ".join([
        f"SUM(CASE WHEN od.first_plus{k}_time IS NOT NULL AND CAST(od.first_plus{k}_time AS TIME) <= '{CUTOFF}' THEN 1 ELSE 0 END) AS hit_{k}"
        for k, _ in TP_COLS
    ])

    filters = [
        "ae.date >= :df", "ae.date <= :dt",
        "ae.attack_version = :av",
        "od.outcome_version = :ov",
        "od.entry_mode = :em",
    ]
    params = {
        "df": req.date_from, "dt": req.date_to,
        "av": req.attack_version, "ov": req.outcome_version,
        "em": req.entry_mode,
    }

    if req.min_attack_number  is not None:
        filters.append("ae.attack_number >= :min_atk_num")
        params["min_atk_num"] = req.min_attack_number
    if req.min_attack_volume  is not None:
        filters.append("ae.attack_volume_v1b >= :min_atk_vol")
        params["min_atk_vol"] = req.min_attack_volume
    if req.min_c31            is not None:
        filters.append("ae.c31 >= :min_c31")
        params["min_c31"] = req.min_c31
    if req.min_volume_ratio   is not None:
        filters.append("COALESCE(dc.volume_ratio_at_0910, dc.volume_ratio) >= :min_vr")
        params["min_vr"] = req.min_volume_ratio
    if req.min_early_high_pct is not None:
        filters.append("dc.early_high_pct >= :min_ehpct")
        params["min_ehpct"] = req.min_early_high_pct

    where_clause = " AND ".join(filters)

    sql = text(f"""
        SELECT
            COUNT(*)                AS sample_count,
            COUNT(DISTINCT ae.date) AS trading_day_count,
            {tp_select}
        FROM attack_events ae
        JOIN outcome_data od
            ON ae.attack_id = od.attack_id
           AND od.outcome_version = :ov
           AND od.entry_mode = :em
        JOIN daily_context dc
            ON ae.date = dc.date AND ae.stock_id = dc.stock_id
        WHERE {where_clause}
    """)

    row = db.execute(sql, params).fetchone()
    if not row:
        return {"sample_count": 0, "trading_day_count": 0, "matrix": []}

    sample_count      = int(row[0])
    trading_day_count = int(row[1])

    matrix = []
    for i, (k, label) in enumerate(TP_COLS):
        hit = int(row[2 + i])
        matrix.append({
            "tp_label":     label,
            "hit_count":    hit,
            "sample_count": sample_count,
            "hit_rate_pct": round(hit / sample_count * 100, 1) if sample_count else 0,
        })

    return {
        "sample_count":        sample_count,
        "trading_day_count":   trading_day_count,
        "avg_signals_per_day": round(sample_count / trading_day_count, 1) if trading_day_count else 0,
        "entry_mode":          req.entry_mode,
        "cutoff":              "09:59",
        "matrix":              matrix,
        "filters_applied": {
            "min_attack_number":  req.min_attack_number,
            "min_attack_volume":  req.min_attack_volume,
            "min_c31":            req.min_c31,
            "min_volume_ratio":   req.min_volume_ratio,
            "min_early_high_pct": req.min_early_high_pct,
        },
    }


# ── CSV exports ───────────────────────────────────────────────────────
@app.get("/api/export/event_master")
def export_em(date_from: date, date_to: date, db=Depends(get_db)):
    df = export_event_master(db, date_from, date_to)
    return StreamingResponse(
        iter([df_to_csv_bytes(df)]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=EVENT_MASTER_{date_from}_{date_to}.csv"}
    )


@app.get("/api/export/backtest_trades/{run_id}")
def export_trades(run_id: int, db=Depends(get_db)):
    df = export_backtest_trades(db, run_id)
    return StreamingResponse(
        iter([df_to_csv_bytes(df)]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=BACKTEST_TRADES_run{run_id}.csv"}
    )


@app.get("/api/export/backtest_summary/{run_id}")
def export_summary(run_id: int, db=Depends(get_db)):
    df = generate_summary(db, run_id)
    return StreamingResponse(
        iter([df_to_csv_bytes(df)]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=BACKTEST_SUMMARY_run{run_id}.csv"}
    )
