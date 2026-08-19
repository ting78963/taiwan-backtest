"""
Volume Ratio Module — 量比計算唯一來源
=======================================

量比定義（直接對應前端 loadHotVolStocks 的算法）：

    前端邏輯（index.html，loadHotVolStocks）：
        const avg5 = 近5個交易日日成交量（張）的均值（歷史日K，不含今日）
        const todayVolZhang = 今日即時成交量（張）
        const volRatio = todayVolZhang / avg5

    後端歷史回測版（本模組）：
        avg5 = 前5個交易日的日成交量均值（從 daily_price 或 market_data 取）
        obs_volume = 09:00~09:10 累積成交量（張）
        volume_ratio_at_0910 = obs_volume / avg5

    語意一致性：
        - 前端盤中看到的「量比」用的是今日目前累積量 ÷ 前5日均量
        - 後端回測在 09:10 那一刻，用相同的分母（前5日均量），
          分子換成 09:10 當下的累積量
        - 這樣「盤中看到的量比」和「歷史回測的量比」用同一套定義

嚴格規定：
    - avg5 只使用 target_date 之前的資料（不含當日），避免 look-ahead
    - volume_ratio_at_0910 只使用 09:10 以前的量，避免 look-ahead
    - 此模組是全專案量比計算的唯一來源，禁止在其他地方重複實作
"""

N_DAYS = 5   # 均量天數（對應前端 last5）


def compute_volume_ratio(
    obs_volume: int,
    prev_n_day_volumes: list[int],
) -> float | None:
    """
    計算量比。對應前端 loadHotVolStocks 的邏輯。

    Args:
        obs_volume:          觀察期間累積成交量（張）
                             盤中即時 = 今日目前累積量
                             歷史回測 = 09:00~09:10 累積量
        prev_n_day_volumes:  前 N 個交易日的日成交量清單（張），不含觀察當日
                             前端：last5（近5日）
                             後端：target_date 之前最近5個有效交易日

    Returns:
        volRatio（float），資料不足時回傳 None
    """
    if not prev_n_day_volumes:
        return None
    if obs_volume is None or obs_volume < 0:
        return None

    valid = [v for v in prev_n_day_volumes if v and v > 0]
    if not valid:
        return None

    avg = sum(valid) / len(valid)
    if avg <= 0:
        return None

    return round(obs_volume / avg, 4)


def classify_volume_ratio(vr: float | None) -> str:
    """
    量比分層標籤。用於回測報表的 volume_ratio 分組統計。
    門檻對應規格：>=1.5 / >=2.0 / >=2.5 / >=3.0 / >=3.5 / >=4.0 / >=4.5 / >=5.0
    """
    if vr is None:
        return "N/A"
    if vr >= 5.0:  return ">=5.0"
    if vr >= 4.5:  return ">=4.5"
    if vr >= 4.0:  return ">=4.0"
    if vr >= 3.5:  return ">=3.5"
    if vr >= 3.0:  return ">=3.0"
    if vr >= 2.5:  return ">=2.5"
    if vr >= 2.0:  return ">=2.0"
    if vr >= 1.5:  return ">=1.5"
    return "<1.5"


# ── 向後相容 alias（v3 tests 使用）────────────────────────────────────
def compute_early_volume_speed_ratio(
    early_volume: int,
    prev_day_volume: int,
    early_minutes: int,
) -> float | None:
    """
    向後相容 alias。
    原始公式：今日早盤每分鐘均速 ÷ 昨日全天每分鐘均速。
    注意：現在的主公式 compute_volume_ratio 使用前5日均量而非昨日單日量。
    此 alias 僅供舊 tests 使用，新代碼請用 compute_volume_ratio。
    """
    if not prev_day_volume or prev_day_volume <= 0:
        return None
    if early_volume is None or early_volume < 0:
        return None
    if not early_minutes or early_minutes <= 0:
        return None
    avg_per_minute = prev_day_volume / 270
    early_avg = early_volume / early_minutes
    if avg_per_minute <= 0:
        return None
    return round(early_avg / avg_per_minute, 4)
