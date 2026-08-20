"""
FinMind Fetcher
===============

歷史資料抓取流程（兩階段）：

Stage 1：全市場當日日線（1 次 request）
  TaiwanStockPrice，只傳 start_date=target_date，不傳 end_date 和 data_id
  → 取得全市場當天收盤漲幅 + 總成交量
  → 篩選：漲幅 >= threshold AND 成交量 >= min_volume_zhang
  → 輸出：candidates 名單

Stage 2：逐支 candidate（candidates × 2 次 request）
  TaiwanStockPrice，傳 data_id + 14天範圍 → 取昨收 + 前5日量（量比分母）
  TaiwanStockKBar，傳 data_id + date → 取當日 1 分 K

Request 數：1 + candidates × 2（約 61~201 次）
"""

import os
import time
import logging
from datetime import date, timedelta
from typing import Optional

import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
BASE_URL      = "https://api.finmindtrade.com/api/v4/data"

logger = logging.getLogger(__name__)


def _request(dataset: str, params: dict, retry: int = 3) -> Optional[pd.DataFrame]:
    """共用 HTTP 請求，帶 retry 與 timeout。"""
    p = dict(params)
    p["token"]   = FINMIND_TOKEN
    p["dataset"] = dataset

    for attempt in range(retry):
        try:
            resp = requests.get(BASE_URL, params=p, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != 200:
                logger.warning(f"[FinMind] status={data.get('status')} msg={data.get('msg')} dataset={dataset}")
                return None

            records = data.get("data", [])
            if not records:
                return pd.DataFrame()

            return pd.DataFrame(records)

        except requests.Timeout:
            logger.warning(f"[FinMind] Timeout attempt={attempt+1}/{retry} dataset={dataset}")
            if attempt < retry - 1:
                time.sleep(2 ** attempt)
        except requests.RequestException as e:
            logger.warning(f"[FinMind] Request failed attempt={attempt+1}/{retry}: {e}")
            if attempt < retry - 1:
                time.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"[FinMind] Unexpected error dataset={dataset}: {e}")
            return None

    logger.error(f"[FinMind] All {retry} retries failed dataset={dataset}")
    return None


def fetch_market_daily_screen(target_date: date) -> Optional[pd.DataFrame]:
    """
    Stage 1：全市場當日日線（1 次 request）。

    只傳 start_date=target_date，不傳 end_date 和 data_id。
    回傳當天所有股票的收盤漲幅和成交量。
    """
    date_str = target_date.strftime("%Y-%m-%d")
    logger.info(f"[FinMind] fetch_market_daily_screen date={target_date}")

    df = _request("TaiwanStockPrice", {
        "start_date": date_str,
        # 不傳 end_date，不傳 data_id → 取當天全市場
    })

    if df is None or df.empty:
        logger.warning(f"[FinMind] 查無 {target_date} 的全市場日線資料")
        return None

    # 成交量換算為張（Trading_Volume 是股數）
    vol_col = next((c for c in ["Trading_Volume", "volume"] if c in df.columns), None)
    if vol_col:
        df["volume_zhang"] = (
            pd.to_numeric(df[vol_col], errors="coerce").fillna(0) / 1000
        ).astype(int)
    else:
        df["volume_zhang"] = 0

    # 漲幅計算（spread = 漲跌金額）
    spread_col = next((c for c in ["spread", "Change", "change"] if c in df.columns), None)
    if spread_col and "close" in df.columns:
        df["close"]     = pd.to_numeric(df["close"],     errors="coerce")
        df[spread_col]  = pd.to_numeric(df[spread_col],  errors="coerce")
        prev_close      = df["close"] - df[spread_col]
        df["change_pct"] = (df[spread_col] / prev_close.replace(0, float("nan"))) * 100
    else:
        df["change_pct"] = 0.0

    logger.info(f"[FinMind] 全市場日線 {target_date}: {len(df)} 筆")
    return df[["stock_id", "close", "volume_zhang", "change_pct"]].copy()


def fetch_prev_context(stock_id: str, target_date: date) -> Optional[dict]:
    """
    Stage 2a：取單支股票的昨收 + 前5日量（1 次 request）。
    傳 data_id + start_date + end_date。
    """
    end_str   = target_date.strftime("%Y-%m-%d")
    start_str = (target_date - timedelta(days=14)).strftime("%Y-%m-%d")

    df = _request("TaiwanStockPrice", {
        "data_id":    stock_id,
        "start_date": start_str,
        "end_date":   end_str,
    })
    if df is None or df.empty:
        return None

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df_before  = df[df["date"] < target_date].sort_values("date")
    if df_before.empty:
        return None

    def to_zhang(row):
        for col in ["Trading_Volume", "volume"]:
            if col in row.index:
                v = row[col]
                val = float(v) if v and str(v) != "nan" else 0
                return int(val / 1000) if col == "Trading_Volume" else int(val)
        return 0

    prev      = df_before.iloc[-1]
    prev5     = df_before.tail(5)
    prev5_vols = [to_zhang(r) for _, r in prev5.iterrows()]
    prev5_vols = [v for v in prev5_vols if v > 0]

    return {
        "prev_close":        float(prev.get("close") or 0),
        "prev_day_volume":   to_zhang(prev),
        "prev5_day_volumes": prev5_vols,
    }


def fetch_1min_kbar(stock_id: str, target_date: date) -> Optional[pd.DataFrame]:
    """Stage 2b：取單支股票當日 1 分 K（1 次 request）。"""
    date_str = target_date.strftime("%Y-%m-%d")
    df = _request("TaiwanStockKBar", {
        "data_id":    stock_id,
        "start_date": date_str,
        "end_date":   date_str,
    })

    if df is None or df.empty:
        return df

    df["datetime"] = pd.to_datetime(df["date"])
    df["date"]     = df["datetime"].dt.date
    df["time"]     = df["datetime"].dt.time
    df["stock_id"] = stock_id

    rename = {}
    if "max" in df.columns: rename["max"] = "high"
    if "min" in df.columns: rename["min"] = "low"
    if rename:
        df = df.rename(columns=rename)

    for col in ["open","high","low","close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)

    cols = [c for c in ["date","stock_id","time","open","high","low","close","volume"]
            if c in df.columns]
    return df[cols].sort_values("time").reset_index(drop=True)


def fetch_candidates(
    target_date: date,
    min_change_pct: float = 4.0,
    min_volume_zhang: int = 4000,
) -> Optional[pd.DataFrame]:
    """
    Stage 1 完整流程：全市場日線篩選 candidates。
    只回傳通過條件的股票清單（不含昨收和前5日量，那在 Stage 2 逐支取）。
    """
    df = fetch_market_daily_screen(target_date)
    if df is None:
        return None
    if df.empty:
        return pd.DataFrame()

    mask = (
        (df["change_pct"]   >= min_change_pct) &
        (df["volume_zhang"] >= min_volume_zhang)
    )
    candidates = df[mask].copy()

    logger.info(
        f"[FinMind] {target_date} candidates={len(candidates)} "
        f"（>={min_change_pct}% >={min_volume_zhang}張）"
    )
    return candidates.reset_index(drop=True)
