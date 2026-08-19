"""
FinMind Fetcher
===============
抓取台股 1 分 K 與昨日收盤資料。
使用 TaiwanStockKBar API（1 分 K）與 TaiwanStockPrice（日 K，取昨收）。
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

FINMIND_TOKEN = os.getenv(
    "FINMIND_TOKEN",
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiSGFtaWx0b25XYW5nIiwiZW1haWwiOiJ0aW5nNzg5NjNAZ21haWwuY29mIiwidG9rZW5fdmVyc2lvbiI6MH0.l67zXvqwdLQbdUqPBJGDokFU-zAgrnmlvM-r41Zt9yA",
)
BASE_URL = "https://api.finmindtrade.com/api/v4/data"

logger = logging.getLogger(__name__)


def _request(dataset: str, params: dict, retry: int = 3) -> Optional[pd.DataFrame]:
    """共用 HTTP 請求，帶 retry 與 rate limit 保護。"""
    params["token"] = FINMIND_TOKEN
    params["dataset"] = dataset

    for attempt in range(retry):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != 200:
                logger.warning(f"FinMind API error: {data.get('msg')} | params={params}")
                return None

            records = data.get("data", [])
            if not records:
                return pd.DataFrame()

            return pd.DataFrame(records)

        except requests.RequestException as e:
            logger.warning(f"Request failed (attempt {attempt+1}/{retry}): {e}")
            if attempt < retry - 1:
                time.sleep(2 ** attempt)

    return None


def fetch_1min_kbar(stock_id: str, target_date: date) -> Optional[pd.DataFrame]:
    """
    抓取指定股票、指定日期的 1 分 K。
    
    Returns DataFrame columns:
        date, stock_id, open, high, low, close, volume
        （time 從 date 欄位拆出）
    """
    date_str = target_date.strftime("%Y-%m-%d")
    df = _request("TaiwanStockKBar", {
        "data_id": stock_id,
        "start_date": date_str,
        "end_date": date_str,
    })

    if df is None or df.empty:
        return df

    # TaiwanStockKBar 的 date 欄位格式：2024-03-15 09:01:00
    df["datetime"] = pd.to_datetime(df["date"])
    df["date"] = df["datetime"].dt.date
    df["time"] = df["datetime"].dt.time
    df["stock_id"] = stock_id

    # 欄位標準化
    df = df.rename(columns={
        "open": "open",
        "max": "high",
        "min": "low",
        "close": "close",
        "volume": "volume",
    })

    # 只保留交易時間內的資料
    cols = ["date", "stock_id", "time", "open", "high", "low", "close", "volume"]
    available = [c for c in cols if c in df.columns]
    df = df[available].copy()

    # 確保數值型別
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)

    return df.sort_values("time").reset_index(drop=True)


def fetch_daily_price(stock_id: str, target_date: date) -> Optional[dict]:
    """
    抓取指定股票、指定日期的日 K（用來取 prev_close 與 prev_day_volume）。
    target_date 是「今天」，所以我們抓前一個交易日的資料。
    
    Returns dict: {prev_close, prev_day_volume} or None
    """
    # 抓最近 10 天確保能取到前一交易日
    end_str = target_date.strftime("%Y-%m-%d")
    start_str = (target_date - timedelta(days=14)).strftime("%Y-%m-%d")

    df = _request("TaiwanStockPrice", {
        "data_id": stock_id,
        "start_date": start_str,
        "end_date": end_str,
    })

    if df is None or df.empty:
        return None

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[df["date"] < target_date].sort_values("date")

    if df.empty:
        return None

    prev = df.iloc[-1]
    return {
        "prev_close": float(prev.get("close", 0) or 0),
        "prev_day_volume": int(prev.get("Trading_Volume", 0) or prev.get("volume", 0) or 0),
    }


def fetch_all_stock_ids() -> list[str]:
    """
    取得所有上市櫃股票代號。
    使用 TaiwanStockInfo dataset。
    """
    df = _request("TaiwanStockInfo", {})
    if df is None or df.empty:
        return []

    # 只保留普通股（排除 ETF、權證等）
    if "type" in df.columns:
        df = df[df["type"].isin(["twse", "tpex"])]

    return df["stock_id"].astype(str).tolist() if "stock_id" in df.columns else []


def fetch_batch_daily_context(target_date: date) -> Optional[pd.DataFrame]:
    """
    批次取得指定日期所有股票的昨收、昨量。
    使用 TaiwanStockPrice 批次 API（不傳 data_id，傳 date）。
    """
    date_str = target_date.strftime("%Y-%m-%d")
    prev_date_str = (target_date - timedelta(days=14)).strftime("%Y-%m-%d")

    df = _request("TaiwanStockPrice", {
        "start_date": prev_date_str,
        "end_date": date_str,
    })

    if df is None or df.empty:
        return None

    df["date"] = pd.to_datetime(df["date"]).dt.date

    # 找每支股票在 target_date 之前最近一個交易日
    df_before = df[df["date"] < target_date].copy()
    df_before = df_before.sort_values("date")
    df_prev = df_before.groupby("stock_id").last().reset_index()

    df_prev = df_prev.rename(columns={
        "close": "prev_close",
        "Trading_Volume": "prev_day_volume",
    })

    vol_col = "prev_day_volume" if "prev_day_volume" in df_prev.columns else "volume"
    df_prev["prev_day_volume"] = pd.to_numeric(df_prev.get(vol_col, 0), errors="coerce").fillna(0).astype(int)
    df_prev["prev_close"] = pd.to_numeric(df_prev["prev_close"], errors="coerce")

    return df_prev[["stock_id", "prev_close", "prev_day_volume"]]
