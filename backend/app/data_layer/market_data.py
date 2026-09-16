"""
历史行情数据获取（yfinance）
供 correlation builder（以及未来的 backtest 模块）复用，避免重复实现价格 fetch 逻辑。
"""

import random
import time
from typing import Optional

import pandas as pd
import yfinance as yf

from ..utils.logger import get_logger

logger = get_logger('mirofish.market_data')

_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.5


def fetch_price_history(
    tickers: list[str],
    start_date: str,
    end_date: str,
    price_field: str = "Close",
) -> pd.DataFrame:
    """
    批量获取多只股票的历史价格（一次 yfinance 批量请求，而非逐个 ticker 调用）。

    返回宽表 DataFrame：index=Date，columns=ticker，values=price_field
    （auto_adjust=True，所以 "Close" 已经是复权后价格）。
    在 date range 内完全没有数据的 ticker 会被丢弃（记录到日志），
    不会让整批请求失败。
    """
    if not tickers:
        return pd.DataFrame()

    last_error: Optional[Exception] = None
    raw = None
    for attempt in range(_MAX_RETRIES):
        try:
            raw = yf.download(
                tickers,
                start=start_date,
                end=end_date,
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            break
        except Exception as error:
            last_error = error
            if attempt < _MAX_RETRIES - 1:
                delay = _BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(f"批量获取价格失败（第 {attempt + 1} 次），{delay:.1f}s 后重试: {error}")
                time.sleep(delay)
            else:
                raise

    if raw is None:
        raise RuntimeError(f"Failed to fetch price history: {last_error}")

    if isinstance(raw.columns, pd.MultiIndex):
        prices = raw[price_field].copy()
    else:
        # yfinance 对单个 ticker 不会返回 MultiIndex 列
        prices = raw[[price_field]].copy()
        prices.columns = tickers[:1]

    fully_empty = prices.columns[prices.isna().all()].tolist()
    if fully_empty:
        logger.warning(f"以下 {len(fully_empty)} 只股票在 {start_date}~{end_date} 内无数据，已跳过: {fully_empty}")
    prices = prices.drop(columns=fully_empty)

    return prices
