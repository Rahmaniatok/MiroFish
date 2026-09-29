"""
Daily close history for several tickers over a date range (one yfinance
batch download). Used by the news pipeline's performance report (step 8),
which needs prices AFTER as_of — the opposite of market_data.py, which is
built to never look past as_of.

Closes are auto-adjusted (splits + dividends), so returns are total returns.
"""

from datetime import date, timedelta
from typing import Dict, List

import pandas as pd
import yfinance as yf

from ..utils.logger import get_logger

logger = get_logger('mirofish.data_layer.price_history')


def fetch_close_history(tickers: List[str], start: date, end: date) -> Dict[str, Dict[str, float]]:
    """
    {ticker: {"YYYY-MM-DD": close}} for trading days in [start, end].
    Tickers yfinance has no data for are simply absent from the result.
    """
    tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t))
    if not tickers:
        return {}
    df = yf.download(tickers, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                     auto_adjust=True, progress=False, group_by="column", threads=True)
    if df is None or df.empty:
        return {}
    closes = df["Close"]
    if isinstance(closes, pd.Series):  # single ticker
        closes = closes.to_frame(tickers[0])
    out: Dict[str, Dict[str, float]] = {}
    for t in tickers:
        if t not in closes.columns:
            continue
        s = closes[t].dropna()
        if s.empty:
            continue
        out[t] = {d.strftime("%Y-%m-%d"): float(v) for d, v in s.items()}
    missing = [t for t in tickers if t not in out]
    if missing:
        logger.warning(f"No price history for: {missing}")
    return out
