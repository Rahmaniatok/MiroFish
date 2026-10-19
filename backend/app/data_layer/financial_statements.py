"""
Financial statements (income statement, balance sheet, cash flow) from
yfinance — quarterly AND annual — for the fundamental filter of the news
pipeline (app/news_pipeline/fundamentals.py).

Unlike `.info` (a current snapshot, see market_data.py), statements are dated
columns, so the ratio engine can pick only periods published before as_of.
yfinance keeps ~5-7 quarters and ~4-5 fiscal years per statement.

Only the rows the ratio engine reads are kept (STATEMENT_ROWS), as
{statement: {row: {period_end_iso: value}}}.

Cache: market_cache.db, data_type "statements", keyed by the FETCH DATE
(as_of_date column = today's ISO date). A statement set fetched today is
reused all day for any as_of (the ratio engine filters periods by as_of
itself); the next day misses and refetches, so new filings are picked up.
"""

from datetime import date
from typing import Any, Dict, Optional

import yfinance as yf

from ..utils.logger import get_logger
from .cache import get_cached, set_cache

logger = get_logger('mirofish.data_layer.financial_statements')

DATA_TYPE = "statements"
SCHEMA_VERSION = 2      # 2: + total liabilities (Graham NCAV)

STATEMENT_ROWS = {
    "income": [
        "Total Revenue", "Gross Profit", "Operating Income", "EBIT", "EBITDA", "Normalized EBITDA",
        "Net Income Common Stockholders", "Net Income", "Diluted EPS", "Basic EPS",
        "Research And Development", "Interest Expense", "Tax Rate For Calcs",
        "Reconciled Depreciation", "Diluted Average Shares",
    ],
    "balance": [
        "Stockholders Equity", "Common Stock Equity", "Tangible Book Value", "Total Assets",
        "Total Debt", "Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents",
        "Current Assets", "Current Liabilities", "Invested Capital", "Ordinary Shares Number",
        "Share Issued", "Total Liabilities Net Minority Interest",
    ],
    "cashflow": [
        "Free Cash Flow", "Operating Cash Flow", "Capital Expenditure", "Cash Dividends Paid",
        "Common Stock Dividend Paid", "Depreciation And Amortization",
        "Depreciation Amortization Depletion",
    ],
}

# (our key, yfinance Ticker attribute)
_SOURCES = [
    ("quarterly_income", "quarterly_income_stmt"),
    ("quarterly_balance", "quarterly_balance_sheet"),
    ("quarterly_cashflow", "quarterly_cashflow"),
    ("annual_income", "income_stmt"),
    ("annual_balance", "balance_sheet"),
    ("annual_cashflow", "cashflow"),
]


def _frame_to_dict(df, rows) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    if df is None or getattr(df, "empty", True):
        return out
    for row in rows:
        if row not in df.index:
            continue
        series = {}
        for col in df.columns:
            v = df.loc[row, col]
            if v is not None and v == v:          # drop None / NaN
                series[col.date().isoformat()] = float(v)
        if series:
            out[row] = series
    return out


def fetch_statements(ticker: str) -> Dict[str, Any]:
    ticker = (ticker or "").strip().upper()
    tk = yf.Ticker(ticker)
    statements: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for key, attr in _SOURCES:
        try:
            statements[key] = _frame_to_dict(getattr(tk, attr), STATEMENT_ROWS[key.split("_", 1)[1]])
        except Exception as e:  # noqa: BLE001 — one missing statement must not sink the others
            statements[key] = {}
            errors[key] = str(e)
    success = any(statements.values())
    return {
        "ticker": ticker,
        "success": success,
        "error": None if success else (errors and "; ".join(f"{k}: {v}" for k, v in errors.items())
                                       or "yfinance returned no financial statements"),
        "schema_version": SCHEMA_VERSION,
        "fetched_on": date.today().isoformat(),
        "statements": statements,
        "errors": errors,
    }


def get_statements(ticker: str, today: Optional[str] = None) -> Dict[str, Any]:
    """Cached fetch_statements (one fetch per ticker per day)."""
    ticker = (ticker or "").strip().upper()
    key = today or date.today().isoformat()
    cached = get_cached(ticker, DATA_TYPE, key)
    if cached is not None and cached.get("schema_version") == SCHEMA_VERSION:
        return cached
    result = fetch_statements(ticker)
    if result["success"]:
        set_cache(ticker, DATA_TYPE, result, key)
    return result
