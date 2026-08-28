"""
Outcome Engine
==============
計算每次 Attack Entry 的價格後續走勢。

研究目標：
    確認買進條件後，價格往上達成各目標的機率（upside hit rate）
    以及往下觸及各跌幅的時序（downside，用於震盪分析）

TP targets（上漲目標，研究達成率）：
    +0.5% / +0.75% / +1.0% / +1.25% / +1.5% / +2.0% / +2.5% / +3.0% / +4.0% / +5.0%

SL levels（下跌觀察，研究洗盤容忍度）：
    -0.25% / -0.5% / -0.75% / -1.0% / -1.25% / -1.5% / -2.0% / -2.5% / -3.0%

每個 level 保存：首次觸及時間 + 是否在 5m/10m 內觸及（boolean）
觀察期：5m、10m、09:59、10:30、11:30、收盤（exit_price 用實際 close，不估算）

嚴格原則：
    entry_at_trigger 永遠 NULL（不可執行）
    entry_mode 只用 bar_close / next_open / next_close
"""

import logging
from datetime import date

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

# 可執行進場模式（移除 trigger）
ENTRY_MODES = ["bar_close", "next_open", "next_close"]

# 觀察截止時間
EXIT_CUTOFFS = [
    ("5m",    None),
    ("10m",   None),
    ("0959",  "09:59:00"),
    ("1030",  "10:30:00"),
    ("1130",  "11:30:00"),
    ("close", "13:30:00"),
]

# 上漲目標（研究達成率）
TP_LEVELS = [0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00, 4.00, 5.00, 6.00, 7.00, 8.00, 9.00, 9.90]

# 下跌觀察（研究洗盤容忍度）
SL_LEVELS = [0.25, 0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00]


def _tp_key(tp: float) -> str:
    """0.50 → '050'，2.50 → '250'，5.00 → '500'"""
    return f"{int(round(tp * 100)):03d}"


def _sl_key(sl: float) -> str:
    return f"{int(round(sl * 100)):03d}"


def compute_outcome(bars_after: pd.DataFrame, entry_price: float) -> dict:
    """
    逐分鐘掃描，記錄各 TP/SL level 的首次觸及時間與 bar index。

    Returns:
        dict 含：
            exit_price_XXX, return_XXX, mfe_XXX, mae_XXX（各截止時間）
            first_plusXXX_time, tpXXX_within_5m, tpXXX_within_10m（TP）
            first_minusXXX_time, slXXX_within_5m, slXXX_within_10m（SL）
    """
    if bars_after.empty or entry_price <= 0:
        return {}

    result = {}
    max_fav = 0.0
    max_adv = 0.0

    tp_first     = {_tp_key(tp): None for tp in TP_LEVELS}
    sl_first     = {_sl_key(sl): None for sl in SL_LEVELS}
    tp_first_bar = {_tp_key(tp): None for tp in TP_LEVELS}
    sl_first_bar = {_sl_key(sl): None for sl in SL_LEVELS}

    snap_exit_price: dict = {}
    snap_mfe: dict = {}
    snap_mae: dict = {}

    bars_list = list(bars_after.iterrows())

    for idx, (_, bar) in enumerate(bars_list):
        bar_time  = str(bar["time"])
        bar_high  = float(bar["high"]  or entry_price)
        bar_low   = float(bar["low"]   or entry_price)
        bar_close = float(bar["close"] or entry_price)

        h_pct = (bar_high  - entry_price) / entry_price * 100
        l_pct = (bar_low   - entry_price) / entry_price * 100

        max_fav = max(max_fav, h_pct)
        max_adv = min(max_adv, l_pct)

        bar_pos = idx + 1  # 1-based，進場後第幾根

        # TP 首次觸及
        for tp in TP_LEVELS:
            k = _tp_key(tp)
            if tp_first[k] is None and h_pct >= tp:
                tp_first[k]     = bar_time
                tp_first_bar[k] = bar_pos

        # SL 首次觸及
        for sl in SL_LEVELS:
            k = _sl_key(sl)
            if sl_first[k] is None and l_pct <= -sl:
                sl_first[k]     = bar_time
                sl_first_bar[k] = bar_pos

        # 各截止時間快照
        if bar_pos <= 5:
            snap_exit_price["5m"]  = bar_close
            snap_mfe["5m"]  = round(max_fav, 4)
            snap_mae["5m"]  = round(max_adv, 4)
        if bar_pos <= 10:
            snap_exit_price["10m"] = bar_close
            snap_mfe["10m"] = round(max_fav, 4)
            snap_mae["10m"] = round(max_adv, 4)
        for label, cutoff in EXIT_CUTOFFS:
            if cutoff and bar_time <= cutoff:
                snap_exit_price[label] = bar_close
                snap_mfe[label] = round(max_fav, 4)
                snap_mae[label] = round(max_adv, 4)

    def pct(ep):
        if ep is None:
            return None
        return round((ep - entry_price) / entry_price * 100, 4)

    for label, _ in EXIT_CUTOFFS:
        ep = snap_exit_price.get(label)
        result[f"exit_price_{label}"] = ep
        result[f"return_{label}"]     = pct(ep)
        result[f"mfe_{label}"]        = snap_mfe.get(label)
        result[f"mae_{label}"]        = snap_mae.get(label)

    for tp in TP_LEVELS:
        k = _tp_key(tp)
        result[f"first_plus{k}_time"]   = tp_first[k]
        result[f"tp{k}_within_5m"]  = (tp_first_bar[k] is not None and tp_first_bar[k] <= 5)
        result[f"tp{k}_within_10m"] = (tp_first_bar[k] is not None and tp_first_bar[k] <= 10)

    for sl in SL_LEVELS:
        k = _sl_key(sl)
        result[f"first_minus{k}_time"]  = sl_first[k]
        result[f"sl{k}_within_5m"]  = (sl_first_bar[k] is not None and sl_first_bar[k] <= 5)
        result[f"sl{k}_within_10m"] = (sl_first_bar[k] is not None and sl_first_bar[k] <= 10)

    return result


def _build_insert_sql() -> tuple[str, str]:
    """動態生成 INSERT 欄位和 VALUES 佔位符，避免手工維護長列表。"""
    tp_time_cols   = [f"first_plus{_tp_key(tp)}_time"  for tp in TP_LEVELS]
    sl_time_cols   = [f"first_minus{_sl_key(sl)}_time" for sl in SL_LEVELS]
    tp_w5_cols  = [f"tp{_tp_key(tp)}_within_5m"   for tp in TP_LEVELS]
    tp_w10_cols = [f"tp{_tp_key(tp)}_within_10m"  for tp in TP_LEVELS]
    sl_w5_cols  = [f"sl{_sl_key(sl)}_within_5m"   for sl in SL_LEVELS]
    sl_w10_cols = [f"sl{_sl_key(sl)}_within_10m"  for sl in SL_LEVELS]

    all_within_cols = []
    for tp in TP_LEVELS:
        k = _tp_key(tp)
        all_within_cols += [f"tp{k}_within_5m", f"tp{k}_within_10m"]
    for sl in SL_LEVELS:
        k = _sl_key(sl)
        all_within_cols += [f"sl{k}_within_5m", f"sl{k}_within_10m"]

    fixed_cols = [
        "attack_id", "outcome_version", "entry_mode", "entry_price", "entry_time",
        "exit_price_5m",   "return_5m",   "mfe_5m",   "mae_5m",
        "exit_price_10m",  "return_10m",  "mfe_10m",  "mae_10m",
        "exit_price_0959", "return_0959", "mfe_0959", "mae_0959",
        "exit_price_1030", "return_1030", "mfe_1030", "mae_1030",
        "exit_price_1130", "return_1130", "mfe_1130", "mae_1130",
        "exit_price_close","return_close","mfe_close","mae_close",
    ]
    all_cols = fixed_cols + tp_time_cols + sl_time_cols + all_within_cols
    binds    = [f":{c}" for c in all_cols]

    return ", ".join(all_cols), ", ".join(binds)


_INSERT_COLS, _INSERT_BINDS = _build_insert_sql()

# ON CONFLICT DO UPDATE — 所有非 PK 欄位
_UPDATE_SET = ", ".join(
    f"{c} = EXCLUDED.{c}"
    for c in _INSERT_COLS.split(", ")
    if c not in ("attack_id", "outcome_version", "entry_mode")
)


def run_outcome_engine(
    db: Session,
    target_date: date,
    attack_version:  str = "V1",
    outcome_version: str = "V1",
) -> dict:
    """對指定日期所有 attack_events 計算並寫入 outcome_data。"""
    attacks = db.execute(text("""
        SELECT ae.attack_id, ae.stock_id, ae.end_time,
               ae.entry_at_bar_close,
               ae.entry_next_open,
               ae.entry_next_close
        FROM attack_events ae
        WHERE ae.date = :date AND ae.attack_version = :av
    """), {"date": target_date, "av": attack_version}).fetchall()

    stats = {"total": len(attacks), "processed": 0, "errors": 0}

    for row in attacks:
        attack_id, stock_id, end_time = row[0], row[1], str(row[2])
        entry_prices = {
            "bar_close":  row[3],
            "next_open":  row[4],
            "next_close": row[5],
        }

        raw = db.execute(text("""
            SELECT time, open, high, low, close, volume
            FROM market_data
            WHERE date = :date AND stock_id = :sid AND time >= :t
            ORDER BY time
        """), {"date": target_date, "sid": stock_id, "t": end_time}).fetchall()

        df_all = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])

        try:
            for mode in ENTRY_MODES:
                ep = entry_prices.get(mode)
                if not ep:
                    continue
                ep = float(ep)

                bars = df_all.iloc[1:] if mode in ("bar_close", "next_open") else df_all.iloc[2:]
                outcome = compute_outcome(bars, ep)
                if not outcome:
                    continue

                params = {
                    "attack_id":    attack_id,
                    "outcome_version": outcome_version,
                    "entry_mode":   mode,
                    "entry_price":  ep,
                    "entry_time":   end_time,
                    "exit_price_5m":    outcome.get("exit_price_5m"),
                    "return_5m":        outcome.get("return_5m"),
                    "mfe_5m":           outcome.get("mfe_5m"),
                    "mae_5m":           outcome.get("mae_5m"),
                    "exit_price_10m":   outcome.get("exit_price_10m"),
                    "return_10m":       outcome.get("return_10m"),
                    "mfe_10m":          outcome.get("mfe_10m"),
                    "mae_10m":          outcome.get("mae_10m"),
                    "exit_price_0959":  outcome.get("exit_price_0959"),
                    "return_0959":      outcome.get("return_0959"),
                    "mfe_0959":         outcome.get("mfe_0959"),
                    "mae_0959":         outcome.get("mae_0959"),
                    "exit_price_1030":  outcome.get("exit_price_1030"),
                    "return_1030":      outcome.get("return_1030"),
                    "mfe_1030":         outcome.get("mfe_1030"),
                    "mae_1030":         outcome.get("mae_1030"),
                    "exit_price_1130":  outcome.get("exit_price_1130"),
                    "return_1130":      outcome.get("return_1130"),
                    "mfe_1130":         outcome.get("mfe_1130"),
                    "mae_1130":         outcome.get("mae_1130"),
                    "exit_price_close": outcome.get("exit_price_close"),
                    "return_close":     outcome.get("return_close"),
                    "mfe_close":        outcome.get("mfe_close"),
                    "mae_close":        outcome.get("mae_close"),
                }
                # TP time + within
                for tp in TP_LEVELS:
                    k = _tp_key(tp)
                    params[f"first_plus{k}_time"]  = outcome.get(f"first_plus{k}_time")
                    params[f"tp{k}_within_5m"]  = outcome.get(f"tp{k}_within_5m",  False)
                    params[f"tp{k}_within_10m"] = outcome.get(f"tp{k}_within_10m", False)
                # SL time + within
                for sl in SL_LEVELS:
                    k = _sl_key(sl)
                    params[f"first_minus{k}_time"] = outcome.get(f"first_minus{k}_time")
                    params[f"sl{k}_within_5m"]  = outcome.get(f"sl{k}_within_5m",  False)
                    params[f"sl{k}_within_10m"] = outcome.get(f"sl{k}_within_10m", False)

                db.execute(text(f"""
                    INSERT INTO outcome_data ({_INSERT_COLS})
                    VALUES ({_INSERT_BINDS})
                    ON CONFLICT (attack_id, outcome_version, entry_mode) DO UPDATE SET
                    {_UPDATE_SET}
                """), params)

            stats["processed"] += 1

        except Exception as e:
            logger.error(f"Outcome error for attack {attack_id}: {e}")
            stats["errors"] += 1

    db.commit()
    logger.info(f"[OutcomeEngine] {target_date}: {stats}")
    return stats
