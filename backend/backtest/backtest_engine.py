"""
Backtest Engine
===============
研究目標：
    買進訊號出現後，價格達成各 TP 目標的機率（hit rate）。
    不模擬實際損益，不計算交易成本。

核心設計：
    每個 Attack Event × Entry Mode × TP Level × Exit Cutoff = 一筆觀察記錄
    output: hit（bool）+ first_hit_time + exit_reason（hit/timeout）
    SL 資料全部保留供後續「洗盤容忍度」分析，但不作為強制停損

Summary 主要排序指標：
    09:59 前 +1.0% 達成率（hit_rate_plus100_0959）

進場模式（移除 trigger）：
    bar_close / next_open / next_close

Attack Version 篩選：
    touch / upward / cross / close_confirm

TP targets：
    +0.5% / +0.75% / +1.0% / +1.25% / +1.5% / +2.0% / +2.5% / +3.0% / +4.0% / +5.0%

Exit cutoffs（觀察截止）：
    0959 / 1030 / 1130 / close
    （5m/10m 用 within boolean 處理，不作為獨立截止）
"""

import itertools
import logging
from datetime import date
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import text

from events.outcome_engine import TP_LEVELS, SL_LEVELS, _tp_key, _sl_key

logger = logging.getLogger(__name__)

# ── 預設策略矩陣 ──────────────────────────────────────────────────────
DEFAULT_COMMISSION     = 0.001425
DEFAULT_COMMISSION_DIS = 0.3
DEFAULT_DAYTRADE_TAX   = 0.0015

DEFAULT_ENTRY_MODES    = ["bar_close", "next_open", "next_close"]   # trigger 已移除
DEFAULT_TP_LEVELS      = [0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00, 4.00, 5.00]
DEFAULT_SL_LEVELS      = [None, 0.25, 0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00]
DEFAULT_ATTACK_NUMBERS = [2, 3, 4]
DEFAULT_EXIT_TIMES     = ["0959", "1030", "1130", "close"]

ATK_DEF_COL = {
    "touch":        "is_touch",
    "upward":       "is_upward",
    "cross":        "is_cross",
    "close_confirm":"is_close_above",
}

# 截止時間字串映射（5m/10m = None，用 within boolean）
CUTOFF_TIME_MAP = {
    "5m":    None,
    "10m":   None,
    "0959":  "09:59:00",
    "1030":  "10:30:00",
    "1130":  "11:30:00",
    "close": "13:30:00",
}

# outcome_data 欄位映射
def _tp_time_col(tp):
    return f"first_plus{_tp_key(tp)}_time"

def _sl_time_col(sl):
    return f"first_minus{_sl_key(sl)}_time"

def _return_col(label):
    return f"return_{label}"

def _exit_price_col(label):
    return f"exit_price_{label}"

def _mfe_col(label):
    return f"mfe_{label}"

def _mae_col(label):
    return f"mae_{label}"


# ── run management ────────────────────────────────────────────────────

def create_run(db: Session, params: dict, versions: dict) -> int:
    result = db.execute(text("""
        INSERT INTO backtest_runs
            (run_name, engine_version, key_version, attack_version, outcome_version,
             params, date_from, date_to, research_threshold, status)
        VALUES (:name, :ev, :kv, :av, :ov,
                CAST(:params AS jsonb), :df, :dt, :rt, 'pending')
        RETURNING run_id
    """), {
        "name":   params.get("run_name") or f"{params.get('date_from')}~{params.get('date_to')}",
        "ev":     versions.get("engine",  "V1"),
        "kv":     versions.get("key",     "V1"),
        "av":     versions.get("attack",  "V1"),
        "ov":     versions.get("outcome", "V1"),
        "params": __import__("json").dumps(params, default=str),
        "df":     params["date_from"],
        "dt":     params["date_to"],
        "rt":     params.get("research_threshold", 0.035),
    })
    run_id = result.fetchone()[0]
    db.commit()
    return run_id


def update_run_status(db: Session, run_id: int, status: str,
                      total_combos=None, total_events=None,
                      total_trades=None, error_msg=None):
    db.execute(text("""
        UPDATE backtest_runs SET
            status        = :status,
            total_combos  = COALESCE(:combos, total_combos),
            total_events  = COALESCE(:events, total_events),
            total_trades  = COALESCE(:trades, total_trades),
            error_message = COALESCE(:err, error_message),
            finished_at   = CASE WHEN :status IN ('done','error') THEN NOW() ELSE finished_at END,
            started_at    = CASE WHEN :status = 'running' THEN NOW() ELSE started_at END
        WHERE run_id = :rid
    """), {"status": status, "combos": total_combos, "events": total_events,
           "trades": total_trades, "err": error_msg, "rid": run_id})
    db.commit()


# ── 核心出場判斷 ──────────────────────────────────────────────────────

def _determine_exit(
    tp: Optional[float],
    sl: Optional[float],
    exit_time_label: str,
    row: dict,
    intrabar_policy: str = "conservative",
) -> dict:
    """
    判斷 TP 是否在截止時間前達成。

    研究語意：
        TP 達成 = 價格在觀察期內觸及目標漲幅
        SL 不強制停損，但記錄「若持有，先觸及 TP 還是先觸及 SL」

    Returns:
        exit_reason: 'hit' / 'timeout' / 'excluded'（同根 TP+SL 且 policy=exclude）
        hit: bool，TP 是否在截止前達成
        tp_hit_time: TP 首次觸及時間（可能在截止後，保存供分析）
        sl_hit_time: SL 首次觸及時間
        same_bar_ambiguous: TP 與 SL 在同一根觸發
        observed_return_pct: 截止時的實際 close 報酬（timeout 用）
    """
    raw_tp_time = row.get(_tp_time_col(tp)) if tp else None
    raw_sl_time = row.get(_sl_time_col(sl)) if sl else None

    strategy_cutoff = CUTOFF_TIME_MAP.get(exit_time_label)

    # 過濾截止時間後的觸發
    if strategy_cutoff is not None:
        tp_time = raw_tp_time if (raw_tp_time and str(raw_tp_time) <= strategy_cutoff) else None
        sl_time = raw_sl_time if (raw_sl_time and str(raw_sl_time) <= strategy_cutoff) else None
    else:
        # 5m/10m：用 within boolean
        n_bars = 5 if exit_time_label == "5m" else 10
        suffix = f"_within_{n_bars}m"
        if tp:
            tp_k   = _tp_key(tp)
            tp_time = raw_tp_time if row.get(f"tp{tp_k}{suffix}", False) else None
        else:
            tp_time = None
        if sl:
            sl_k   = _sl_key(sl)
            sl_time = raw_sl_time if row.get(f"sl{sl_k}{suffix}", False) else None
        else:
            sl_time = None

    actual_return     = row.get(_return_col(exit_time_label))
    actual_exit_price = row.get(_exit_price_col(exit_time_label))
    mfe = float(row.get(_mfe_col(exit_time_label)) or 0)
    mae = float(row.get(_mae_col(exit_time_label)) or 0)

    same_bar_ambiguous = False

    if tp_time and sl_time:
        tp_t, sl_t = str(tp_time), str(sl_time)
        if tp_t == sl_t:
            same_bar_ambiguous = True
            if intrabar_policy == "exclude":
                return {
                    "exit_reason": "excluded", "hit": None,
                    "observed_return_pct": None, "exit_price": actual_exit_price,
                    "mfe": mfe, "mae": mae,
                    "tp_hit_time": tp_time, "sl_hit_time": sl_time,
                    "same_bar_ambiguous": True,
                }
            tp_hit_first = (intrabar_policy == "optimistic")
        else:
            tp_hit_first = (tp_t < sl_t)
    elif tp_time:
        tp_hit_first = True
    elif sl_time:
        tp_hit_first = False
    else:
        tp_hit_first = None

    # TP 達成（hit）
    if tp_hit_first is True:
        return {
            "exit_reason": "hit", "hit": True,
            "observed_return_pct": tp,
            "exit_price": None,
            "mfe": mfe, "mae": mae,
            "tp_hit_time": tp_time, "sl_hit_time": raw_sl_time,
            "same_bar_ambiguous": same_bar_ambiguous,
        }

    # SL 先觸及（但不強制停損，記錄為 timeout，hit=False）
    if tp_hit_first is False:
        return {
            "exit_reason": "timeout", "hit": False,
            "observed_return_pct": float(actual_return or 0),
            "exit_price": actual_exit_price,
            "mfe": mfe, "mae": mae,
            "tp_hit_time": raw_tp_time, "sl_hit_time": sl_time,
            "same_bar_ambiguous": same_bar_ambiguous,
        }

    # 觀察期結束，未觸及 TP 也未觸及 SL
    return {
        "exit_reason": "timeout", "hit": False,
        "observed_return_pct": float(actual_return or 0),
        "exit_price": actual_exit_price,
        "mfe": mfe, "mae": mae,
        "tp_hit_time": raw_tp_time, "sl_hit_time": raw_sl_time,
        "same_bar_ambiguous": False,
    }


# ── SELECT col_names（與 SQL 完全對應）────────────────────────────────

def _build_select_and_cols() -> tuple[str, list[str]]:
    """動態生成 SELECT 子句和 col_names，確保兩者永遠同步。"""
    fixed_select = """
        ae.attack_id, ae.date, ae.stock_id,
        ae.attack_version, ae.attack_number, ae.key_price,
        ae.attack_volume, ae.attack_volume_v1a, ae.attack_volume_v1b,
        ae.is_touch, ae.is_upward, ae.is_cross, ae.is_close_above,
        ae.c21, ae.c31, ae.c32, ae.c41, ae.c31_v1a, ae.c41_v1a,
        ae.entry_at_bar_close,
        ae.entry_next_open, ae.entry_next_close,
        dc.prev_close, dc.early_high_pct,
            COALESCE(dc.volume_ratio_at_0910, dc.volume_ratio) AS volume_ratio_at_0910,
            ae.volume_ratio_at_attack,
        od.entry_mode, od.outcome_version,
        od.exit_price_5m,   od.return_5m,   od.mfe_5m,   od.mae_5m,
        od.exit_price_10m,  od.return_10m,  od.mfe_10m,  od.mae_10m,
        od.exit_price_0959, od.return_0959, od.mfe_0959, od.mae_0959,
        od.exit_price_1030, od.return_1030, od.mfe_1030, od.mae_1030,
        od.exit_price_1130, od.return_1130, od.mfe_1130, od.mae_1130,
        od.exit_price_close,od.return_close,od.mfe_close,od.mae_close"""

    fixed_cols = [
        "attack_id","date","stock_id","attack_version","attack_number","key_price",
        "attack_volume","attack_volume_v1a","attack_volume_v1b",
        "is_touch","is_upward","is_cross","is_close_above",
        "c21","c31","c32","c41","c31_v1a","c41_v1a",
        "entry_at_bar_close","entry_next_open","entry_next_close",
        "prev_close","early_high_pct","volume_ratio_at_0910","volume_ratio_at_attack",
        "entry_mode","outcome_version",
        "exit_price_5m","return_5m","mfe_5m","mae_5m",
        "exit_price_10m","return_10m","mfe_10m","mae_10m",
        "exit_price_0959","return_0959","mfe_0959","mae_0959",
        "exit_price_1030","return_1030","mfe_1030","mae_1030",
        "exit_price_1130","return_1130","mfe_1130","mae_1130",
        "exit_price_close","return_close","mfe_close","mae_close",
    ]

    # TP time + within
    tp_select_parts, tp_cols = [], []
    for tp in TP_LEVELS:
        k = _tp_key(tp)
        tp_select_parts.append(f"od.first_plus{k}_time")
        tp_select_parts.append(f"od.tp{k}_within_5m")
        tp_select_parts.append(f"od.tp{k}_within_10m")
        tp_cols += [f"first_plus{k}_time", f"tp{k}_within_5m", f"tp{k}_within_10m"]

    # SL time + within
    sl_select_parts, sl_cols = [], []
    for sl in SL_LEVELS:
        k = _sl_key(sl)
        sl_select_parts.append(f"od.first_minus{k}_time")
        sl_select_parts.append(f"od.sl{k}_within_5m")
        sl_select_parts.append(f"od.sl{k}_within_10m")
        sl_cols += [f"first_minus{k}_time", f"sl{k}_within_5m", f"sl{k}_within_10m"]

    select_sql = fixed_select + ",\n        " + ",\n        ".join(tp_select_parts + sl_select_parts)
    col_names  = fixed_cols + tp_cols + sl_cols
    return select_sql, col_names


_SELECT_FIELDS, _COL_NAMES = _build_select_and_cols()


# ── 主回測函數 ────────────────────────────────────────────────────────

def run_backtest(
    db: Session,
    run_id: int,
    date_from: date,
    date_to: date,
    params: dict,
) -> dict:
    """
    批次跑所有策略組合，輸出 backtest_trades。

    backtest_trades 的 observed_return_pct：
        hit = True  → TP 值（e.g. 1.0 = +1%）
        hit = False → 截止時的實際 close 報酬
    不計算 commission / tax（研究達成率，不模擬損益）。
    """
    av = params.get("attack_version",  "V1")
    ov = params.get("outcome_version", "V1")
    rt = params.get("research_threshold")

    attack_defs    = params.get("attack_definitions") or list(ATK_DEF_COL.keys())
    attack_numbers = params.get("attack_numbers")     or DEFAULT_ATTACK_NUMBERS
    entry_modes    = [m for m in (params.get("entry_modes") or DEFAULT_ENTRY_MODES)
                      if m != "trigger"]              # 確保 trigger 不進入
    tp_levels      = params.get("tp_levels")          or DEFAULT_TP_LEVELS
    sl_levels      = params.get("sl_levels")          or DEFAULT_SL_LEVELS
    exit_times     = params.get("exit_times")         or DEFAULT_EXIT_TIMES
    intrabar_policy= params.get("intrabar_policy",    "conservative")

    # ── SELECT（含所有 within 欄位）───────────────────────────────────
    all_rows = db.execute(text(f"""
        SELECT {_SELECT_FIELDS}
        FROM attack_events ae
        JOIN daily_context dc ON ae.date = dc.date AND ae.stock_id = dc.stock_id
        JOIN outcome_data od
            ON ae.attack_id = od.attack_id AND od.outcome_version = :ov
        WHERE ae.date >= :df AND ae.date <= :dt
          AND ae.attack_version = :av
          AND (:rt IS NULL OR dc.early_high_pct >= :rt * 100)
          AND (:mvr IS NULL OR ae.volume_ratio_at_attack >= :mvr)
          AND od.entry_mode = ANY(:ems)
        ORDER BY ae.date, ae.stock_id, ae.attack_number
    """), {"df": date_from, "dt": date_to, "av": av, "ov": ov,
           "rt": rt, "mvr": params.get("min_volume_ratio"),
           "ems": entry_modes}).fetchall()

    if not all_rows:
        update_run_status(db, run_id, "done", total_combos=0, total_events=0, total_trades=0)
        return {"total_combos": 0, "total_events": 0, "total_trades": 0}

    df = pd.DataFrame(all_rows, columns=_COL_NAMES)
    total_events = len(df["attack_id"].unique())

    entry_price_col = {
        "bar_close":  "entry_at_bar_close",
        "next_open":  "entry_next_open",
        "next_close": "entry_next_close",
    }

    combos = list(itertools.product(
        attack_defs, attack_numbers, entry_modes, tp_levels, sl_levels, exit_times
    ))
    total_trades = 0

    BATCH_SIZE = 500
    batch: list[dict] = []

    def flush_batch():
        nonlocal total_trades
        if not batch:
            return
        db.execute(text("""
            INSERT INTO backtest_trades (
                run_id, attack_id, strategy_id,
                date, stock_id, prev_close, early_high_pct, volume_ratio,
                key_price, attack_number,
                attack_volume, attack_volume_v1a,
                attack_definition, c21, c31, c31_v1a, c32, c41,
                entry_mode, entry_price,
                tp_pct, sl_pct, exit_time_limit,
                exit_reason, observed_return_pct, mfe, mae,
                tp_hit_time, sl_hit_time, tp_hit_first
            )
            SELECT * FROM unnest(
                CAST(:run_ids AS bigint[]), CAST(:attack_ids AS bigint[]), CAST(:strategy_ids AS varchar[]),
                CAST(:dates AS date[]), CAST(:stock_ids AS varchar[]), CAST(:prev_closes AS numeric[]), CAST(:ehpcts AS numeric[]), CAST(:vr0910s AS numeric[]),
                CAST(:key_prices AS numeric[]), CAST(:atk_nums AS int[]),
                CAST(:atk_vols AS bigint[]), CAST(:atk_v1as AS bigint[]),
                CAST(:atk_defs AS varchar[]), CAST(:c21s AS numeric[]), CAST(:c31s AS numeric[]), CAST(:c31v1as AS numeric[]), CAST(:c32s AS numeric[]), CAST(:c41s AS numeric[]),
                CAST(:entry_modes AS varchar[]), CAST(:entry_prices AS numeric[]),
                CAST(:tp_pcts AS numeric[]), CAST(:sl_pcts AS numeric[]), CAST(:exit_limits AS varchar[]),
                CAST(:exit_reasons AS varchar[]), CAST(:obs_rets AS numeric[]), CAST(:mfes AS numeric[]), CAST(:maes AS numeric[]),
                CAST(:tp_times AS time[]), CAST(:sl_times AS time[]), CAST(:tp_firsts AS boolean[])
            )
        """), {
            "run_ids":      [r["run_id"]            for r in batch],
            "attack_ids":   [r["attack_id"]         for r in batch],
            "strategy_ids": [r["strategy_id"]       for r in batch],
            "dates":        [r["date"]              for r in batch],
            "stock_ids":    [r["stock_id"]          for r in batch],
            "prev_closes":  [r["prev_close"]        for r in batch],
            "ehpcts":       [r.get("early_high_pct") for r in batch],
            "vr0910s":      [r.get("volume_ratio_at_0910") for r in batch],
            "vrat_s":       [r.get("volume_ratio_at_attack") for r in batch],
            "key_prices":   [r["key_price"]         for r in batch],
            "atk_nums":     [r["attack_number"]     for r in batch],
            "atk_vols":     [r["attack_volume"]     for r in batch],
            "atk_v1as":     [r.get("attack_volume_v1a") for r in batch],
            "atk_defs":     [r["attack_definition"] for r in batch],
            "c21s":         [r["c21"]               for r in batch],
            "c31s":         [r["c31"]               for r in batch],
            "c31v1as":      [r.get("c31_v1a")       for r in batch],
            "c32s":         [r["c32"]               for r in batch],
            "c41s":         [r["c41"]               for r in batch],
            "entry_modes":  [r["entry_mode"]        for r in batch],
            "entry_prices": [r["entry_price"]       for r in batch],
            "tp_pcts":      [r["tp_pct"]            for r in batch],
            "sl_pcts":      [r["sl_pct"]            for r in batch],
            "exit_limits":  [r["exit_time_limit"]   for r in batch],
            "exit_reasons": [r["exit_reason"]       for r in batch],
            "obs_rets":     [r["observed_return_pct"] for r in batch],
            "mfes":         [r["mfe"]               for r in batch],
            "maes":         [r["mae"]               for r in batch],
            "tp_times":     [r["tp_hit_time"]       for r in batch],
            "sl_times":     [r["sl_hit_time"]       for r in batch],
            "tp_firsts":    [r["tp_hit_first"]      for r in batch],
        })
        db.commit()
        total_trades += len(batch)
        batch.clear()

    for (atk_def, atk_num, entry_mode, tp, sl, exit_time) in combos:
        def_col = ATK_DEF_COL[atk_def]
        ep_col  = entry_price_col[entry_mode]
        sl_label = f"SL{_sl_key(sl)}" if sl else "noSL"
        strategy_id = f"{atk_def}|A{atk_num}|{entry_mode}|TP{_tp_key(tp)}|{sl_label}|{exit_time}"

        subset = df[
            (df["attack_number"] == atk_num) &
            (df[def_col] == True) &
            (df["entry_mode"] == entry_mode)
        ]
        if subset.empty:
            continue

        for _, row in subset.iterrows():
            ep = row.get(ep_col)
            if not ep or float(ep) <= 0:
                continue

            row_dict = row.to_dict()
            result = _determine_exit(tp, sl, exit_time, row_dict, intrabar_policy)

            if result["exit_reason"] == "excluded":
                continue

            batch.append({
                "run_id":          run_id,
                "attack_id":       int(row["attack_id"]),
                "strategy_id":     strategy_id,
                "date":            row["date"],
                "stock_id":        row["stock_id"],
                "prev_close":      row.get("prev_close"),
                "early_high_pct":  row.get("early_high_pct"),
                "volume_ratio_at_0910": row.get("volume_ratio_at_0910"),
                "key_price":       row.get("key_price"),
                "attack_number":   atk_num,
                "attack_volume":   row.get("attack_volume"),
                "attack_volume_v1a": row.get("attack_volume_v1a"),
                "attack_definition": atk_def,
                "c21":             row.get("c21"),
                "c31":             row.get("c31"),
                "c31_v1a":         row.get("c31_v1a"),
                "c32":             row.get("c32"),
                "c41":             row.get("c41"),
                "entry_mode":      entry_mode,
                "entry_price":     float(ep),
                "tp_pct":          tp,
                "sl_pct":          sl,
                "exit_time_limit": exit_time,
                "exit_reason":     result["exit_reason"],
                "observed_return_pct": result.get("observed_return_pct", 0),
                "mfe":             result["mfe"],
                "mae":             result["mae"],
                "tp_hit_time":     result.get("tp_hit_time"),
                "sl_hit_time":     result.get("sl_hit_time"),
                "tp_hit_first":    result.get("tp_hit_first"),
            })
            if len(batch) >= BATCH_SIZE:
                flush_batch()

    flush_batch()

    update_run_status(db, run_id, "done",
                      total_combos=len(combos),
                      total_events=total_events,
                      total_trades=total_trades)
    logger.info(f"[Backtest] run_id={run_id}: {len(combos)} combos, {total_trades} trades")
    return {"total_combos": len(combos), "total_events": total_events, "total_trades": total_trades}


# ── Summary ───────────────────────────────────────────────────────────

def generate_summary(db: Session, run_id: int) -> pd.DataFrame:
    """
    BACKTEST_SUMMARY：各策略組合的達成率統計。

    主要輸出：
        sample_count         樣本數
        trading_day_count    涵蓋的交易日數
        avg_signals_per_day  平均每日訊號數
        hit_rate_plus050_0959 ~ hit_rate_plus500_0959  各 TP 09:59 前達成率
        hit_rate_plus100_0959  主要排序欄位
        avg_mfe / avg_mae

    設計原則：
        hit_rate = COUNT(exit_reason='hit') / COUNT(*)
        sample_count 和 trading_day_count 同時顯示，避免少樣本假高勝率
    """
    # 建立動態的 hit_rate 計算欄位
    # 格式：hit_rate_plusXXX_LABEL = SUM(CASE WHEN tp_pct=X AND exit_time=L AND exit_reason='hit' THEN 1 ELSE 0 END) / COUNT(*)
    # 因為 summary 是按 strategy_id 分組（已含 TP 和 exit_time），直接用 exit_reason 即可
    rows = db.execute(text("""
        SELECT
            strategy_id,
            attack_definition,
            attack_number,
            entry_mode,
            tp_pct,
            sl_pct,
            exit_time_limit,
            COUNT(*)                                              AS sample_count,
            COUNT(DISTINCT date)                                  AS trading_day_count,
            ROUND(COUNT(*)::numeric / NULLIF(COUNT(DISTINCT date),0), 2) AS avg_signals_per_day,
            SUM(CASE WHEN exit_reason='hit' THEN 1 ELSE 0 END)   AS hits,
            ROUND(
                SUM(CASE WHEN exit_reason='hit' THEN 1 ELSE 0 END)::numeric
                / NULLIF(COUNT(*), 0) * 100, 2
            )                                                     AS hit_rate_pct,
            ROUND(AVG(mfe)::numeric, 4)                           AS avg_mfe,
            ROUND(AVG(mae)::numeric, 4)                           AS avg_mae,
            ROUND(AVG(observed_return_pct)::numeric, 4)           AS avg_observed_return,
            ROUND(AVG(volume_ratio)::numeric, 4)                  AS avg_vr_0910,
            ROUND(AVG(c31)::numeric, 4)                           AS avg_c31
        FROM backtest_trades
        WHERE run_id = :run_id
        GROUP BY strategy_id, attack_definition, attack_number, entry_mode,
                 tp_pct, sl_pct, exit_time_limit
        ORDER BY hit_rate_pct DESC, sample_count DESC
    """), {"run_id": run_id}).fetchall()

    cols = [
        "strategy_id","attack_definition","attack_number","entry_mode",
        "tp_pct","sl_pct","exit_time_limit",
        "sample_count","trading_day_count","avg_signals_per_day",
        "hits","hit_rate_pct","avg_mfe","avg_mae","avg_return","avg_vr_0910","avg_c31",
    ]
    return pd.DataFrame(rows, columns=cols)
