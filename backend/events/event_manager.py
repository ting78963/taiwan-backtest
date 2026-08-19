"""
Event Manager
=============
決定哪些日期需要重跑事件辨識。

規則：
  1. 該日從未跑過（event_runs 無記錄）→ 需要跑
  2. 目前 current engine_version 與 event_runs 記錄的版本不符 → 需要重跑
  3. 已跑過且版本相符 → 跳過

重跑時：
  - 先刪除舊的 key_events / attack_events / outcome_data（CASCADE 自動清理）
  - 重新計算並寫入
  - 更新 event_runs

這樣保證：事件資料的版本永遠與 engine_version 一致。
前端 RUN 回測前，系統可以自動檢查一致性。
"""

import logging
from datetime import date, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)


def get_dates_needing_events(
    db: Session,
    date_from: date,
    date_to: date,
    key_version: str,
    attack_version: str,
    outcome_version: str,
) -> list[date]:
    """
    回傳需要（重）跑事件辨識的日期清單。
    條件：
      a) 有原始資料（data_inventory.fetch_status='done'）
      b) 尚未跑過，或 engine_version 有更新
    """
    # 找有資料的日期
    rows_data = db.execute(text("""
        SELECT date FROM data_inventory
        WHERE date >= :df AND date <= :dt AND fetch_status = 'done'
        ORDER BY date
    """), {"df": date_from, "dt": date_to}).fetchall()
    data_dates = {r[0] for r in rows_data}

    # 找已用相同版本跑過的日期
    rows_done = db.execute(text("""
        SELECT date FROM event_runs
        WHERE date >= :df AND date <= :dt
          AND key_version = :kv
          AND attack_version = :av
          AND outcome_version = :ov
          AND run_status = 'done'
    """), {
        "df": date_from, "dt": date_to,
        "kv": key_version, "av": attack_version, "ov": outcome_version,
    }).fetchall()
    done_dates = {r[0] for r in rows_done}

    need = sorted(data_dates - done_dates)
    logger.info(f"[EventManager] 需要跑事件辨識：{len(need)} 天（共 {len(data_dates)} 天有資料，{len(done_dates)} 天已完成）")
    return need


def clear_events_for_date(db: Session, target_date: date):
    """
    清除某日期的所有事件資料。
    CASCADE 會自動清理 attack_events → outcome_data。
    """
    db.execute(text("""
        DELETE FROM key_events WHERE date = :date
    """), {"date": target_date})
    db.commit()
    logger.info(f"[EventManager] 清除 {target_date} 的事件資料")


def mark_event_run_done(
    db: Session,
    target_date: date,
    key_version: str,
    attack_version: str,
    outcome_version: str,
    keys_found: int = 0,
    attacks_found: int = 0,
):
    """記錄某日期的事件辨識已完成（用哪個版本）"""
    db.execute(text("""
        INSERT INTO event_runs
            (date, key_version, attack_version, outcome_version, run_status, keys_found, attacks_found)
        VALUES (:date, :kv, :av, :ov, 'done', :kf, :af)
        ON CONFLICT (date, key_version, attack_version, outcome_version) DO UPDATE SET
            run_status    = 'done',
            keys_found    = EXCLUDED.keys_found,
            attacks_found = EXCLUDED.attacks_found,
            ran_at        = NOW()
    """), {
        "date": target_date, "kv": key_version, "av": attack_version, "ov": outcome_version,
        "kf": keys_found, "af": attacks_found,
    })
    db.commit()


def get_event_coverage(db: Session, date_from: date, date_to: date) -> dict:
    """回傳事件辨識的覆蓋狀況（供前端顯示）"""
    r = db.execute(text("""
        SELECT
            COUNT(DISTINCT di.date)                                      AS data_days,
            COUNT(DISTINCT er.date) FILTER (WHERE er.run_status='done')  AS event_days,
            SUM(er.keys_found)                                           AS total_keys,
            SUM(er.attacks_found)                                        AS total_attacks
        FROM data_inventory di
        LEFT JOIN event_runs er ON di.date = er.date
        WHERE di.date >= :df AND di.date <= :dt
    """), {"df": date_from, "dt": date_to}).fetchone()

    return {
        "data_days":    int(r[0] or 0),
        "event_days":   int(r[1] or 0),
        "total_keys":   int(r[2] or 0),
        "total_attacks": int(r[3] or 0),
    }
