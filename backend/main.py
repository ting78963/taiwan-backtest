"""
Taiwan Backtest System — FastAPI Backend v2
==========================================
前端 RUN 只讀 DB，絕不重抓 FinMind。
資料缺失才補抓，Engine 升版才重建事件。
每次回測綁定 run_id + 完整參數快照 + engine_version。
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text

from db.database import get_db, init_db, get_current_engine_versions
from data.data_service import fetch_missing_dates
from data.inventory import get_missing_dates, get_inventory_summary
from events.event_manager import (
    get_dates_needing_events, clear_events_for_date,
    mark_event_run_done, get_event_coverage,
)
from events.key_engine import run_key_detection
from events.attack_engine import run_attack_detection
from events.outcome_engine import run_outcome_engine
from backtest.backtest_engine import create_run, run_backtest, generate_summary, update_run_status
from backtest.export_engine import export_event_master, export_backtest_trades, df_to_csv_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ─── 系統版本號 ───────────────────────────────────────────────
ENGINE_VERSION = "V1"   # 升版時修改這裡，並在 engine_versions 表新增記錄


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Taiwan Backtest System", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ─── 進度追蹤（in-memory，重啟後清除）────────────────────────
_task_status: dict[str, dict] = {}


def set_task(task_id: str, status: str, **kwargs):
    _task_status[task_id] = {"status": status, **kwargs}


# ════════════════════════════════════════════════════════════
# Schemas
# ════════════════════════════════════════════════════════════

class FetchRequest(BaseModel):
    date_from:             date
    date_to:               date
    collection_threshold:  float = Field(0.025, description=(
        "資料收集門檻（預設 2.5%）：達到此漲幅的股票保存全天 1 分 K。"
        "與 research_threshold 分離，確保未來調整研究門檻不需重抓歷史資料。"
    ))
    force_refetch:         bool  = Field(False, description="強制重抓已有資料（預設 False）")


class EventRequest(BaseModel):
    date_from:      date
    date_to:        date
    key_version:    str = "V1"
    attack_version: str = "V1"
    outcome_version:str = "V1"
    force_rebuild:  bool = Field(False, description="強制重建已有事件（預設 False，只跑缺少的）")


class BacktestRequest(BaseModel):
    run_name:            Optional[str]  = None
    date_from:           date
    date_to:             date
    attack_version:      str            = "V1"
    outcome_version:     str            = "V1"
    research_threshold:  Optional[float] = Field(0.035, description=(
        "回測篩選門檻（預設 3.5%）：只有 early_high_pct >= 此值的股票進入回測。"
        "資料層可能用 2.5% 收集，回測時再用 3.5% 篩選，兩者獨立。"
    ))
    intrabar_policy:     str  = Field("conservative", description=(
        "同一根 K 同時觸及 TP 與 SL 時的處理方式："
        "conservative（預設）= SL 先到；optimistic = TP 先到；exclude = 不計入回測。"
    ))
    # 研究達成率，不模擬損益
    min_volume_ratio: Optional[float] = Field(None, description=(
        "量比最低門檻（volume_ratio_at_0910）。None=不限。"
        "保存原始樣本，量比只在 RUN 時作為可調研究條件。"
    ))
    # 策略矩陣（None = 使用全部預設值）
    attack_definitions: Optional[list[str]]           = None
    attack_numbers:     Optional[list[int]]            = None
    entry_modes:        Optional[list[str]]            = None
    tp_levels:          Optional[list[float]]          = None
    sl_levels:          Optional[list[Optional[float]]]= None
    exit_times:         Optional[list[str]]            = None
    # 成本
    tax:                float = 0.0015


# ════════════════════════════════════════════════════════════
# Health & Overview
# ════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok", "engine_version": ENGINE_VERSION}


@app.get("/api/stats/overview")
def stats_overview(db: Session = Depends(get_db)):
    r = db.execute(text("""
        SELECT
            (SELECT COUNT(*) FROM data_inventory WHERE fetch_status='done')   AS data_days,
            (SELECT COUNT(*) FROM daily_context WHERE passes_price_filter) AS candidates,
            (SELECT COUNT(*) FROM key_events)    AS keys,
            (SELECT COUNT(*) FROM attack_events) AS attacks,
            (SELECT COUNT(*) FROM outcome_data)  AS outcomes,
            (SELECT COUNT(*) FROM backtest_runs WHERE status='done')  AS completed_runs,
            (SELECT COUNT(*) FROM backtest_trades) AS total_trades
    """)).fetchone()
    return dict(zip(["data_days","candidates","keys","attacks","outcomes","completed_runs","total_trades"], r))


@app.get("/api/inventory")
def get_inventory(
    date_from: date = Query(...), date_to: date = Query(...),
    db: Session = Depends(get_db)
):
    """查詢指定日期範圍的資料庫存狀況（哪些日期已有資料）"""
    ct = 0.025  # 預設 collection_threshold，與 FetchRequest 預設值一致
    summary = get_inventory_summary(db, date_from, date_to)
    missing = get_missing_dates(db, date_from, date_to, ct)
    return {**summary, "missing_count": len(missing), "missing_dates": [str(d) for d in missing[:30]]}


@app.get("/api/engine/versions")
def get_engine_versions(db: Session = Depends(get_db)):
    """查詢目前 engine 版本"""
    versions = get_current_engine_versions(db)
    return {"current": versions, "system_engine_version": ENGINE_VERSION}


# ════════════════════════════════════════════════════════════
# Step 1：抓取資料（只補缺日期）
# ════════════════════════════════════════════════════════════

@app.post("/api/fetch")
async def fetch_data(req: FetchRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # 先查庫存，不需要的話提前回應
    if not req.force_refetch:
        missing = get_missing_dates(db, req.date_from, req.date_to, req.collection_threshold)
        if not missing:
            return {
                "task_id": None,
                "message": f"✅ {req.date_from}~{req.date_to} 資料已完整，無需抓取",
                "missing_count": 0,
            }

    task_id = f"fetch_{req.date_from}_{req.date_to}"
    set_task(task_id, "running", message="開始補抓缺少的日期...")

    def run():
        try:
            result = fetch_missing_dates(
                db, req.date_from, req.date_to,
                req.collection_threshold,
            )
            set_task(task_id, "done", **result)
        except Exception as e:
            logger.exception(e)
            set_task(task_id, "error", message=str(e))

    background_tasks.add_task(run)
    return {"task_id": task_id, "message": "資料補抓已啟動（背景執行）"}


# ════════════════════════════════════════════════════════════
# Step 2：事件辨識（只跑缺少 / 版本不符的日期）
# ════════════════════════════════════════════════════════════

@app.post("/api/events/run")
async def run_events(req: EventRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    task_id = f"events_{req.date_from}_{req.date_to}_{req.attack_version}"
    set_task(task_id, "running", message="Event Engine 啟動中...")

    def run():
        try:
            if req.force_rebuild:
                # 強制重建：清除指定範圍的事件
                from datetime import timedelta
                curr = req.date_from
                while curr <= req.date_to:
                    clear_events_for_date(db, curr)
                    curr += timedelta(days=1)

            dates_needed = get_dates_needing_events(
                db, req.date_from, req.date_to,
                req.key_version, req.attack_version, req.outcome_version,
            )

            if not dates_needed:
                set_task(task_id, "done", message="所有日期的事件資料已是最新版本，無需重跑", dates_processed=0)
                return

            total_keys = 0
            total_attacks = 0
            for d in dates_needed:
                k = run_key_detection(db, d, req.key_version)
                a = run_attack_detection(db, d, req.key_version, req.attack_version)
                o = run_outcome_engine(db, d, req.attack_version, req.outcome_version)
                total_keys    += k.get("found", 0)
                total_attacks += a.get("total_attacks", 0)
                mark_event_run_done(
                    db, d,
                    req.key_version, req.attack_version, req.outcome_version,
                    k.get("found", 0), a.get("total_attacks", 0),
                )

            set_task(task_id, "done",
                     dates_processed=len(dates_needed),
                     total_keys=total_keys,
                     total_attacks=total_attacks)

        except Exception as e:
            logger.exception(e)
            set_task(task_id, "error", message=str(e))

    background_tasks.add_task(run)
    return {"task_id": task_id, "message": f"Event Engine 已啟動"}


@app.get("/api/events/coverage")
def event_coverage(date_from: date = Query(...), date_to: date = Query(...), db: Session = Depends(get_db)):
    """查詢事件辨識覆蓋狀況"""
    return get_event_coverage(db, date_from, date_to)


# ════════════════════════════════════════════════════════════
# Step 3：回測（完全讀 DB，不抓 FinMind）
# ════════════════════════════════════════════════════════════

@app.post("/api/backtest/run")
async def run_backtest_api(req: BacktestRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    versions = get_current_engine_versions(db)
    params = req.model_dump()
    params["date_from"] = str(req.date_from)
    params["date_to"]   = str(req.date_to)

    run_id = create_run(db, params, versions)
    task_id = f"backtest_{run_id}"
    set_task(task_id, "running", run_id=run_id)

    def run():
        try:
            result = run_backtest(db, run_id, params)
            set_task(task_id, "done", run_id=run_id, **result)
        except Exception as e:
            logger.exception(e)
            update_run_status(db, run_id, "error", error_msg=str(e))
            set_task(task_id, "error", run_id=run_id, message=str(e))

    background_tasks.add_task(run)
    return {"task_id": task_id, "run_id": run_id, "message": "回測已開始（背景執行）"}


@app.get("/api/backtest/runs")
def list_runs(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT run_id, run_name, engine_version, key_version, attack_version, outcome_version,
               date_from, date_to, status, total_combos, total_events, total_trades,
               created_at, finished_at
        FROM backtest_runs ORDER BY created_at DESC LIMIT 50
    """)).fetchall()
    cols = ["run_id","run_name","engine_version","key_version","attack_version","outcome_version",
            "date_from","date_to","status","total_combos","total_events","total_trades","created_at","finished_at"]
    return [dict(zip(cols, r)) for r in rows]


@app.get("/api/backtest/{run_id}/summary")
def get_summary(run_id: int, db: Session = Depends(get_db)):
    df = generate_summary(db, run_id)
    return df.to_dict(orient="records") if not df.empty else []


@app.get("/api/backtest/{run_id}/info")
def get_run_info(run_id: int, db: Session = Depends(get_db)):
    row = db.execute(text("""
        SELECT run_id, run_name, engine_version, key_version, attack_version, outcome_version,
               params, date_from, date_to, status, total_combos, total_events, total_trades,
               error_message, created_at, started_at, finished_at
        FROM backtest_runs WHERE run_id = :rid
    """), {"rid": run_id}).fetchone()
    if not row:
        raise HTTPException(404, "Run not found")
    cols = ["run_id","run_name","engine_version","key_version","attack_version","outcome_version",
            "params","date_from","date_to","status","total_combos","total_events","total_trades",
            "error_message","created_at","started_at","finished_at"]
    return dict(zip(cols, row))


# ════════════════════════════════════════════════════════════
# Task 進度查詢
# ════════════════════════════════════════════════════════════

@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    return _task_status.get(task_id, {"status": "not_found"})


# ════════════════════════════════════════════════════════════
# Event Master 查詢（前端顯示）
# ════════════════════════════════════════════════════════════

@app.get("/api/events/master")
def get_event_master(
    date_from: date = Query(...),
    date_to:   date = Query(...),
    attack_number:     Optional[int]   = None,
    attack_version:    str             = "V1",
    min_volume_ratio:  Optional[float] = None,
    min_attack_number: Optional[int]   = None,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    df = export_event_master(db, date_from, date_to, attack_version=attack_version)
    if attack_number:
        df = df[df["attack_number"] == attack_number]
    if min_attack_number:
        df = df[df["attack_number"] >= min_attack_number]
    if min_volume_ratio:
        df = df[df["volume_ratio"] >= min_volume_ratio]
    return df.head(limit).to_dict(orient="records")


# ════════════════════════════════════════════════════════════
# CSV 匯出
# ════════════════════════════════════════════════════════════

@app.get("/api/export/event_master")
def export_em(
    date_from: date = Query(...), date_to: date = Query(...),
    attack_version: str = "V1",
    db: Session = Depends(get_db)
):
    df = export_event_master(db, date_from, date_to, attack_version=attack_version)
    return StreamingResponse(iter([df_to_csv_bytes(df)]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=EVENT_MASTER_{date_from}_{date_to}.csv"})


@app.get("/api/export/backtest_trades/{run_id}")
def export_trades(run_id: int, db: Session = Depends(get_db)):
    df = export_backtest_trades(db, run_id)
    return StreamingResponse(iter([df_to_csv_bytes(df)]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=BACKTEST_TRADES_run{run_id}.csv"})


@app.get("/api/export/backtest_summary/{run_id}")
def export_summary(run_id: int, db: Session = Depends(get_db)):
    df = generate_summary(db, run_id)
    return StreamingResponse(iter([df_to_csv_bytes(df)]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=BACKTEST_SUMMARY_run{run_id}.csv"})
