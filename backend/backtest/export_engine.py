"""
Export Engine
=============
產生三份標準輸出 CSV：
  1. EVENT_MASTER.csv    — 所有事件原始特徵（最重要）
  2. BACKTEST_TRADES.csv — 每筆觀察記錄（研究達成率）
  3. BACKTEST_SUMMARY.csv — 各策略統計

研究純化原則：
  - 不輸出交易成本相關欄位（已移除）
  - 不輸出 entry_at_trigger（永遠 NULL，對研究無意義）
  - gross_return 已更名為 observed_return_pct
  - 量比（volume_ratio_at_0910）進入 EVENT_MASTER 和 BACKTEST_TRADES
"""

import io
import logging
from datetime import date

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import text

from .backtest_engine import generate_summary
from events.outcome_engine import TP_LEVELS, SL_LEVELS, _tp_key, _sl_key

logger = logging.getLogger(__name__)


# ── EVENT_MASTER 欄位（固定順序）────────────────────────────────────
# 不含 entry_at_trigger（永遠 NULL，無研究價值）
# 含量比三件組（09:10 當下真實可見的狀態）

def _build_event_master_cols() -> list[str]:
    base = [
        "attack_id", "date", "stock_id", "prev_close",
        "early_high_price", "early_high_pct",
        # 量比三件組
        "cumulative_volume_at_0910", "volume_ratio_at_0910",
        "key_price", "key_confirmed_time",
        "attack_number", "attack_version",
        "attack_start", "attack_end", "bars_used",
        "attack_volume", "attack_volume_v1a", "attack_volume_v1b",
        "attack_high", "attack_low",
        "is_touch", "is_upward", "is_cross", "is_close_above",
        "crossed_key", "closed_above_key",
        "c21", "c31", "c31_v1a", "c32", "c41",
        "entry_at_bar_close", "entry_next_open", "entry_next_close",
        "mfe_5m",   "mae_5m",
        "mfe_10m",  "mae_10m",
        "mfe_0959", "mae_0959",
        "mfe_1030", "mae_1030",
        "mfe_1130", "mae_1130",
        "mfe_close","mae_close",
    ]
    for tp in TP_LEVELS:
        k = _tp_key(tp)
        base.append(f"first_plus{k}_time")
    for sl in SL_LEVELS:
        k = _sl_key(sl)
        base.append(f"first_minus{k}_time")
    return base

EVENT_MASTER_COLS = _build_event_master_cols()


# ── BACKTEST_TRADES 欄位 ──────────────────────────────────────────────
# 與 schema 的 backtest_trades 表 100% 一致
# 不含 commission_cost / tax_cost / net_return / entry_at_trigger

BACKTEST_TRADES_COLS = [
    "trade_id", "run_id", "attack_id", "strategy_id",
    "date", "stock_id", "prev_close", "early_high_pct",
    "volume_ratio_at_0910",          # 量比
    "key_price", "attack_number",
    "attack_volume", "attack_volume_v1a",
    "attack_definition",
    "c21", "c31", "c31_v1a", "c32", "c41",
    "entry_mode", "entry_price",
    "tp_pct", "sl_pct", "exit_time_limit",
    "exit_reason",
    "observed_return_pct",           # 原 gross_return，改名
    "mfe", "mae",
    "tp_hit_time", "sl_hit_time", "tp_hit_first",
    "created_at",
]


def export_event_master(
    db: Session,
    date_from: date,
    date_to: date,
    attack_version:  str = "V1",
    outcome_version: str = "V1",
) -> pd.DataFrame:
    """
    EVENT_MASTER：每個 Attack 事件一列，含所有研究特徵。
    不含 entry_at_trigger（永遠 NULL）。
    含量比兩件組：cumulative_volume_at_0910 / volume_ratio_at_0910。
    （early_high_pct 已代表早盤最高漲幅，不重複保存 price_pct_at_0910）
    """
    # 動態生成 TP/SL first-hit SELECT
    tp_selects = [f"od.first_plus{_tp_key(tp)}_time"  for tp in TP_LEVELS]
    sl_selects = [f"od.first_minus{_sl_key(sl)}_time" for sl in SL_LEVELS]
    extra_selects = ",\n            ".join(tp_selects + sl_selects)

    rows = db.execute(text(f"""
        SELECT
            ae.attack_id,
            ae.date,
            ae.stock_id,
            dc.prev_close,
            dc.early_high_price,
            dc.early_high_pct,
            dc.cumulative_volume_at_0910,
            COALESCE(dc.volume_ratio_at_0910, dc.volume_ratio) AS volume_ratio_at_0910,
            ke.key_price,
            COALESCE(ke.key_confirmed_time, ke.key_created_time) AS key_confirmed_time,
            ae.attack_number,
            ae.attack_version,
            ae.start_time   AS attack_start,
            ae.end_time     AS attack_end,
            ae.bars_used,
            ae.attack_volume,
            ae.attack_volume_v1a,
            ae.attack_volume_v1b,
            ae.attack_high,
            ae.attack_low,
            ae.is_touch,
            ae.is_upward,
            ae.is_cross,
            ae.is_close_above,
            ae.crossed_key,
            ae.closed_above_key,
            ae.c21, ae.c31, ae.c31_v1a, ae.c32, ae.c41,
            ae.entry_at_bar_close,
            ae.entry_next_open,
            ae.entry_next_close,
            od.mfe_5m,   od.mae_5m,
            od.mfe_10m,  od.mae_10m,
            od.mfe_0959, od.mae_0959,
            od.mfe_1030, od.mae_1030,
            od.mfe_1130, od.mae_1130,
            od.mfe_close,od.mae_close,
            {extra_selects}
        FROM attack_events ae
        JOIN daily_context dc ON ae.date = dc.date AND ae.stock_id = dc.stock_id
        JOIN key_events ke    ON ae.key_id = ke.key_id
        LEFT JOIN outcome_data od
            ON ae.attack_id = od.attack_id
           AND od.outcome_version = :ov
           AND od.entry_mode = 'bar_close'
        WHERE ae.date >= :df AND ae.date <= :dt
          AND ae.attack_version = :av
        ORDER BY ae.date, ae.stock_id, ae.attack_number
    """), {"df": date_from, "dt": date_to, "av": attack_version, "ov": outcome_version}).fetchall()

    return pd.DataFrame(rows, columns=EVENT_MASTER_COLS)


def export_backtest_trades(db: Session, run_id: int) -> pd.DataFrame:
    """
    BACKTEST_TRADES：每筆觀察記錄，欄位與 schema 完全一致。
    SELECT 欄位與 BACKTEST_TRADES_COLS 嚴格對應，任何不一致會在 consistency test 捕捉。
    """
    rows = db.execute(text("""
        SELECT
            trade_id, run_id, attack_id, strategy_id,
            date, stock_id, prev_close, early_high_pct,
            volume_ratio_at_0910,
            key_price, attack_number,
            attack_volume, attack_volume_v1a,
            attack_definition,
            c21, c31, c31_v1a, c32, c41,
            entry_mode, entry_price,
            tp_pct, sl_pct, exit_time_limit,
            exit_reason,
            observed_return_pct,
            mfe, mae,
            tp_hit_time, sl_hit_time, tp_hit_first,
            created_at
        FROM backtest_trades
        WHERE run_id = :run_id
        ORDER BY date, stock_id, strategy_id
    """), {"run_id": run_id}).fetchall()

    if not rows:
        return pd.DataFrame(columns=BACKTEST_TRADES_COLS)
    return pd.DataFrame(rows, columns=BACKTEST_TRADES_COLS)


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """DataFrame → CSV bytes（UTF-8 with BOM，Excel 直接開啟不亂碼）。"""
    buf = io.BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue()
