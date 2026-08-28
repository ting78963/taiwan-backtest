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
from sqlalchemy import text

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
    # 上界（exclusive）：factor < max_x
    max_attack_number:  Optional[int]   = None   # attack_number < N
    max_attack_volume:  Optional[int]   = None   # attack_volume_v1b < N
    max_c31:            Optional[float] = None   # c31 < X
    max_volume_ratio:   Optional[float] = None   # volume_ratio < X
    max_early_high_pct: Optional[float] = None   # early_high_pct < X%
    # Attack 時段篩選（ae.end_time 的研究 filter，與 cutoff 無關）
    attack_time_from: Optional[str] = None  # ae.end_time >= HH:MM:SS (inclusive)
    attack_time_to:   Optional[str] = None  # ae.end_time <  HH:MM:SS (exclusive)
    # 估量增縮（第六因子）
    min_estimated_vg: Optional[float] = None   # estimated_volume_growth_at_attack >= X%
    max_estimated_vg: Optional[float] = None   # estimated_volume_growth_at_attack < X%
    # C31 NULL 處理：True = 只計 c31 IS NOT NULL 的樣本
    require_c31_not_null: bool = False
    attack_version:     str   = "V1"
    outcome_version:    str   = "V1"
    cutoff:             str   = "09:59"   # 截止時間：09:59 / 10:29 / 10:59 / 11:29 / 11:59 / 12:29 / 12:59 / 13:30


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
    max_volume_ratio:    Optional[float] = Field(None)   # VR < X（<0.5 用）
    max_early_high_pct:  Optional[float] = Field(None)   # early_high_pct < X%
    attack_time_from:    Optional[str]   = Field(None)   # ae.end_time >= HH:MM:SS
    attack_time_to:      Optional[str]   = Field(None)   # ae.end_time <  HH:MM:SS
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
            if req.force_rerun:
                # 強制重建：不論已完成幾天，全部 data_dates 都重新處理
                from events.event_manager import get_all_data_dates
                dates_needed = get_all_data_dates(db2, req.date_from, req.date_to)
                logger.info(f"[EVENT TASK] force_rerun=True → dates_needed={len(dates_needed)} 天（全部 data_dates）")
            else:
                dates_needed = get_dates_needing_events(
                    db2, req.date_from, req.date_to,
                    req.key_version, req.attack_version, req.outcome_version
                )
                logger.info(f"[EVENT TASK] force_rerun=False → dates_needed={len(dates_needed)} 天")
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


# ── 清除指定日期資料 ──────────────────────────────────────────────────────
@app.delete("/api/clear_date")
def clear_date(target_date: date, db=Depends(get_db)):
    """清除指定日期的所有資料，讓該日期可以重新抓取。"""
    try:
        db.execute(text("DELETE FROM event_runs WHERE date = :d"), {"d": target_date})
        db.execute(text("""
            DELETE FROM outcome_data WHERE attack_id IN
            (SELECT attack_id FROM attack_events WHERE date = :d)
        """), {"d": target_date})
        db.execute(text("DELETE FROM attack_events WHERE date = :d"), {"d": target_date})
        db.execute(text("DELETE FROM key_events WHERE date = :d"),    {"d": target_date})
        db.execute(text("DELETE FROM daily_context WHERE date = :d"), {"d": target_date})
        db.execute(text("DELETE FROM market_data WHERE date = :d"),   {"d": target_date})
        db.execute(text("DELETE FROM data_inventory WHERE date = :d"),{"d": target_date})
        db.commit()
        return {"status": "ok", "cleared": str(target_date)}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ── 清除全部研究資料 ──────────────────────────────────────────────────
@app.delete("/api/clear_all")
def clear_all(db=Depends(get_db)):
    """
    清除全部歷史研究資料，保留 schema 和 engine_versions。
    全部在同一 transaction：全部成功才 COMMIT，任一失敗 ROLLBACK。
    """
    try:
        # 依 FK 順序刪除：leaf → root
        db.execute(text("DELETE FROM backtest_trades"))
        db.execute(text("DELETE FROM backtest_runs"))
        db.execute(text("DELETE FROM outcome_data"))
        db.execute(text("DELETE FROM attack_events"))
        db.execute(text("DELETE FROM key_events"))
        db.execute(text("DELETE FROM daily_context"))
        db.execute(text("DELETE FROM market_data"))
        db.execute(text("DELETE FROM event_runs"))
        db.execute(text("DELETE FROM data_inventory"))
        db.commit()
        logger.info("[CLEAR ALL] 全部研究資料已清除")
        return {"status": "ok", "message": "全部研究資料已清除，schema 保留"}
    except Exception as e:
        db.rollback()
        logger.error(f"[CLEAR ALL ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"清除失敗，已 ROLLBACK：{e}")


# ── 已抓取日期狀態 ─────────────────────────────────────────────────────
@app.get("/api/stats/dates")
def stats_dates(db=Depends(get_db)):
    """
    回傳每個已抓取日期的狀態。
    JOIN data_inventory + event_runs，不新增 DB table。
    """
    rows = db.execute(text("""
        SELECT
            di.date,
            di.collection_threshold,
            di.stocks_fetched,
            di.fetch_status,
            di.fetched_at,
            CASE WHEN er.date IS NOT NULL THEN TRUE ELSE FALSE END AS event_done,
            COALESCE(er.keys_found, 0)    AS keys_found,
            COALESCE(er.attacks_found, 0) AS attacks_found
        FROM data_inventory di
        LEFT JOIN event_runs er
            ON di.date = er.date
           AND er.key_version = 'V1'
           AND er.attack_version = 'V1'
           AND er.outcome_version = 'V1'
        ORDER BY di.date DESC
    """)).fetchall()

    return {
        "dates": [
            {
                "date":                 str(r[0]),
                "collection_threshold": float(r[1]) if r[1] else None,
                "stocks_fetched":       int(r[2]) if r[2] else 0,
                "fetch_status":         r[3],
                "fetched_at":           str(r[4]) if r[4] else None,
                "event_done":           bool(r[5]),
                "keys_found":           int(r[6]),
                "attacks_found":        int(r[7]),
            }
            for r in rows
        ]
    }


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
    # cutoff 參數化：前端傳 "09:59"，這裡補秒數
    # 合法 cutoff 值白名單（HH:MM → HH:MM:SS）
    # 不在白名單的值直接 422，不讓任意字串進 SQL
    CUTOFF_MAP = {
        "09:59": "09:59:00",
        "10:29": "10:29:00",
        "10:59": "10:59:00",
        "11:29": "11:29:00",
        "11:59": "11:59:00",
        "12:29": "12:29:00",
        "12:59": "12:59:00",
        "13:30": "13:30:00",
    }
    if req.cutoff not in CUTOFF_MAP:
        raise HTTPException(
            status_code=422,
            detail=f"cutoff '{req.cutoff}' 不在合法值清單：{list(CUTOFF_MAP.keys())}"
        )
    CUTOFF = CUTOFF_MAP[req.cutoff]

    # TP hit：first_plusXXX_time 在 cutoff 前觸及
    tp_select = ", ".join([
        f"SUM(CASE WHEN od.first_plus{k}_time IS NOT NULL "
        f"AND CAST(od.first_plus{k}_time AS TIME) <= CAST(:cutoff AS TIME) THEN 1 ELSE 0 END) AS hit_{k}"
        for k, _ in TP_COLS
    ])

    filters = [
        "ae.date >= :df", "ae.date <= :dt",
        "ae.attack_version = :av",
        "od.outcome_version = :ov",
        "od.entry_mode = :em",
        # denominator 修正：只計 entry_time <= cutoff 的樣本
        # entry_time > cutoff 的 Attack 根本沒機會在 cutoff 前達標，不得計入分母
        "CAST(od.entry_time AS TIME) <= CAST(:cutoff AS TIME)",
    ]
    params = {
        "df": req.date_from, "dt": req.date_to,
        "av": req.attack_version, "ov": req.outcome_version,
        "em": req.entry_mode,
        "cutoff": CUTOFF,
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
        filters.append("ae.volume_ratio_at_attack >= :min_vr")
        params["min_vr"] = req.min_volume_ratio
    if req.min_early_high_pct is not None:
        filters.append("dc.early_high_pct >= :min_ehpct")
        params["min_ehpct"] = req.min_early_high_pct
    # max 條件（exclusive upper bound：factor < max）
    if req.max_attack_number  is not None:
        filters.append("ae.attack_number < :max_atk_num")
        params["max_atk_num"] = req.max_attack_number
    if req.max_attack_volume  is not None:
        filters.append("ae.attack_volume_v1b < :max_atk_vol")
        params["max_atk_vol"] = req.max_attack_volume
    if req.max_c31            is not None:
        filters.append("ae.c31 < :max_c31")
        params["max_c31"] = req.max_c31
    if req.max_volume_ratio   is not None:
        filters.append("ae.volume_ratio_at_attack < :max_vr")
        params["max_vr"] = req.max_volume_ratio
    if req.max_early_high_pct is not None:
        filters.append("dc.early_high_pct < :max_ehpct")
        params["max_ehpct"] = req.max_early_high_pct
    # Attack 時段 filter（half-open interval [from, to)，以 ae.end_time 為準）
    VALID_ATTACK_TIMES = {"09:00:00", "09:30:00", "10:00:00", None}
    if req.attack_time_from is not None:
        if req.attack_time_from not in VALID_ATTACK_TIMES:
            raise HTTPException(status_code=422, detail=f"attack_time_from 不合法：{req.attack_time_from}")
        filters.append("CAST(ae.end_time AS TIME) >= CAST(:atk_from AS TIME)")
        params["atk_from"] = req.attack_time_from
    if req.attack_time_to is not None:
        if req.attack_time_to not in VALID_ATTACK_TIMES:
            raise HTTPException(status_code=422, detail=f"attack_time_to 不合法：{req.attack_time_to}")
        filters.append("CAST(ae.end_time AS TIME) < CAST(:atk_to AS TIME)")
        params["atk_to"] = req.attack_time_to
    # 估量增縮 filter
    if req.min_estimated_vg is not None:
        filters.append("ae.estimated_volume_growth_at_attack >= :min_evg")
        params["min_evg"] = req.min_estimated_vg
    if req.max_estimated_vg is not None:
        filters.append("ae.estimated_volume_growth_at_attack < :max_evg")
        params["max_evg"] = req.max_estimated_vg
    # C31 NULL 過濾：True = 分母只計 c31 IS NOT NULL 的樣本
    if req.require_c31_not_null:
        filters.append("ae.c31 IS NOT NULL")

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
        hit = int(row[2 + i] or 0)  # SUM() 在零樣本時回傳 NULL，轉為 0
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
        "cutoff":              req.cutoff,
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
