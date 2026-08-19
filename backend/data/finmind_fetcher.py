"""
FinMind Fetcher
===============

歷史資料抓取流程（兩階段）：

Stage 1：全市場日線（1 次 request）
  TaiwanStockPrice，傳 date，不傳 data_id
  → 取得全市場當天收盤漲幅 + 總成交量
  → 篩選：漲幅 >= threshold AND 成交量 >= min_volume_zhang
  → 輸出：candidates 名單 + 昨收 + 前5日量（量比分母）

Stage 2：逐支 1 分 K（candidates × 1 次 request）
  TaiwanStockKBar，傳 data_id + date
  → 每天約 30~100 支，共 31~101 次 request
  → 比原本全市場逐支（3,600 次）減少 97%

注意：
  - TaiwanStockPrice 全市場日線：~1,800 筆 × 100 bytes ≈ 176 KB，無 OOM 風險
  - TaiwanStockKBar 1分K：每支 270 根，candidates 100 支 ≈ 2.6 MB，安全
  - 量比分母（前5日量）從同一次日線 response 計算，不額外 request
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


def fetch_market_daily(target_date: date, lookback_days: int = 14) -> Optional[pd.DataFrame]:
    """
    Stage 1：全市場日線（1 次 request）。

    抓取 target_date 前 lookback_days 天到 target_date 的日線，
    用途：
      - 取得 target_date 當天的收盤漲幅和總成交量（候選篩選）
      - 取得前5個交易日的日量（量比分母）

    回傳欄位：
      stock_id, date, open, close, volume_zhang（張）, change_pct（%）
    """
    end_str   = target_date.strftime("%Y-%m-%d")
    start_str = (target_date - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    logger.info(f"[FinMind] fetch_market_daily date={target_date} ({start_str}~{end_str})")

    df = _request("TaiwanStockPrice", {
        "start_date": start_str,
        "end_date":   end_str,
        # 不傳 data_id → 全市場日線
        # 資料量：~1,800 支 × 14 天 = ~25,200 筆 ≈ 2.5 MB，可接受
    })

    if df is None or df.empty:
        return None

    df["date"] = pd.to_datetime(df["date"]).dt.date

    # 成交量換算為張
    # TaiwanStockPrice 的 Trading_Volume 是股數，除以 1000 轉張
    vol_col = next((c for c in ["Trading_Volume", "volume"] if c in df.columns), None)
    if vol_col:
        df["volume_zhang"] = (
            pd.to_numeric(df[vol_col], errors="coerce").fillna(0) / 1000
        ).astype(int)
    else:
        df["volume_zhang"] = 0

    # 漲幅：(close - yesterday_close) / yesterday_close * 100
    # TaiwanStockPrice 有 Change 欄位（漲跌金額），change_pct = Change / (close - Change) * 100
    if "Change" in df.columns and "close" in df.columns:
        df["close"]  = pd.to_numeric(df["close"],  errors="coerce")
        df["Change"] = pd.to_numeric(df["Change"], errors="coerce")
        prev_close   = df["close"] - df["Change"]
        df["change_pct"] = (df["Change"] / prev_close.replace(0, float("nan"))) * 100
    else:
        df["change_pct"] = 0.0

    cols = [c for c in ["stock_id","date","open","close","volume_zhang","change_pct"]
            if c in df.columns]
    return df[cols].copy()


def fetch_candidates(
    target_date: date,
    min_change_pct: float = 4.0,
    min_volume_zhang: int = 4000,
) -> Optional[pd.DataFrame]:
    """
    Stage 1 完整流程：取全市場日線 → 篩選 candidates → 計算量比分母。

    回傳 DataFrame，每列一個 candidate，欄位：
      stock_id, close（昨收用的 prev_close）, volume_zhang（當日總量）,
      change_pct, prev5_volumes（list，量比分母）
    """
    df = fetch_market_daily(target_date)
    if df is None or df.empty:
        return None

    # 今天的資料（當天收盤）
    today_df = df[df["date"] == target_date].copy()
    if today_df.empty:
        logger.warning(f"[FinMind] 查無 {target_date} 的日線資料（可能非交易日或資料延遲）")
        return None

    # 前5個交易日的資料（量比分母）
    prev_df = df[df["date"] < target_date].copy()

    # 計算每支股票的前5日量
    def get_prev5(sid):
        rows = prev_df[prev_df["stock_id"] == sid].sort_values("date").tail(5)
        return rows["volume_zhang"].tolist()

    # 篩選 candidates
    mask = (
        (today_df["change_pct"]   >= min_change_pct) &
        (today_df["volume_zhang"] >= min_volume_zhang)
    )
    candidates = today_df[mask].copy()

    if candidates.empty:
        logger.info(f"[FinMind] {target_date} 無符合條件的 candidates（>={min_change_pct}% >={min_volume_zhang}張）")
        return pd.DataFrame()

    # 加入前5日量（量比分母）和 prev_close
    # prev_close = close - Change（即昨收）
    # 但這裡的 close 是當天收盤，我們用它作為 prev_close 給明天用
    # 對於「當天的研究」，prev_close 需要從 prev_df 取
    def get_prev_close(sid):
        rows = prev_df[prev_df["stock_id"] == sid].sort_values("date")
        if rows.empty:
            return 0.0
        return float(rows.iloc[-1]["close"])

    candidates["prev_close"]     = candidates["stock_id"].apply(get_prev_close)
    candidates["prev5_volumes"]  = candidates["stock_id"].apply(get_prev5)

    logger.info(
        f"[FinMind] {target_date} candidates={len(candidates)} "
        f"（>={min_change_pct}% >={min_volume_zhang}張）"
    )
    return candidates.reset_index(drop=True)


def fetch_1min_kbar(stock_id: str, target_date: date) -> Optional[pd.DataFrame]:
    """
    Stage 2：取單支股票當日 1 分 K（1 次 request）。

    TaiwanStockKBar 的 volume 已是張，不需除以 1000。
    """
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
