"""
Step 1 · Fundamental filter — financial ratios benchmarked per sector / industry.

For every company of the screened sectors (ALL tiers, so the benchmark is the
whole S&P 500 sector or industry, not only the tickers the user keeps):

  1. ratios from yfinance statements for four periods
       q0  — latest quarter:   last 4 quarters (TTM) ending at the latest quarter
       q1  — previous quarter: TTM ending one quarter earlier
       fy0 — last full fiscal year
       fy1 — the fiscal year before
     Flows are summed over the period, the balance sheet is the one at its end.
     Only periods PUBLISHED before as_of are used: a quarter counts once
     QUARTER_LAG_DAYS have passed since it ended (10-Q deadline is 40 days),
     a fiscal year after ANNUAL_LAG_DAYS (10-K deadline 60-90 days).
     Price: the latest periods (q0, fy0) use the close on/before as_of — today's
     valuation; the earlier ones (q1, fy1) the close at their period end — the
     valuation at that time. Market cap = that price x the period's share count.
  2. benchmark per GICS sector and per sub-industry: n, min, Q1, median, mean,
     Q3, max. "N/M" values (e.g. P/E with negative EPS) and, from 8 peers on,
     outliers beyond 3 x IQR are left out of the statistics.
  3. rank of each company per ratio: direction-adjusted percentile (100 = best
     in the group), better/worse than the median, raw min/max.

Ratios are grouped into stock CATEGORIES (value, growth, dividend, quality,
GARP, defensive). The user picks a category and then all or some of its
ratios; SECTOR_PROFILES gives the recommended starting set per sector, and
NOT_APPLICABLE drops ratios that mean nothing for a sector (EV/EBITDA for a
bank). A company PASSES when it beats its group median on at least
`threshold` of the chosen ratios (N/M counts as not beating it).

compute_ticker / build_benchmarks / evaluate are pure; start_fundamentals runs
the fetch as a background task over a finished universe screen.
"""

import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..data_layer.financial_statements import get_statements
from ..data_layer.market_data import get_price_data
from ..data_layer.price_history import fetch_close_history
from ..models.task import TaskManager, TaskStatus
from ..utils.logger import get_logger

logger = get_logger('mirofish.news_pipeline.fundamentals')

TASK_TYPE = "pipeline_fundamentals"
QUARTER_LAG_DAYS = 45
ANNUAL_LAG_DAYS = 75
OUTLIER_MIN_PEERS = 8
OUTLIER_IQR_K = 3.0
SMALL_GROUP = 5
FETCH_WORKERS = 6
DEFAULT_TAX_RATE = 0.21
PERIODS = ("q0", "q1", "fy0", "fy1")
LEVELS = ("sector", "sub_industry")
THRESHOLDS = (0.5, 0.6, 0.75, 1.0)
PRICE_HISTORY_DAYS = 3 * 365          # far enough back for the fiscal year before last

PERIOD_INFO = {
    "q0": {"label": "Latest quarter", "short": "Q", "kind": "quarter",
           "desc": "Last 4 quarters (TTM) ending at the latest published quarter; price on as_of"},
    "q1": {"label": "Previous quarter", "short": "Q−1", "kind": "quarter",
           "desc": "TTM ending one quarter earlier; price at that quarter's end"},
    "fy0": {"label": "Last fiscal year", "short": "FY", "kind": "year",
            "desc": "Last full fiscal year published before as_of; price on as_of"},
    "fy1": {"label": "Previous fiscal year", "short": "FY−1", "kind": "year",
            "desc": "The fiscal year before; price at that year's end"},
}

# better: which direction is good · unit: "x" multiple, "%" fraction shown as %
# rule: a well-known rule of thumb (Graham / Buffett), shown as guidance only
METRICS: Dict[str, Dict[str, str]] = {
    # valuation
    "pe": {"label": "P/E", "name": "Price / Earnings", "kind": "valuation", "better": "lower", "unit": "x",
           "formula": "Price ÷ diluted EPS", "nm": "EPS ≤ 0", "rule": "Graham: below 15×"},
    "pb": {"label": "P/B", "name": "Price / Book", "kind": "valuation", "better": "lower", "unit": "x",
           "formula": "Market cap ÷ stockholders' equity", "nm": "equity ≤ 0", "rule": "Graham: below 1.5×"},
    "ptbv": {"label": "P/TBV", "name": "Price / Tangible Book", "kind": "valuation", "better": "lower", "unit": "x",
             "formula": "Market cap ÷ tangible book value", "nm": "tangible book ≤ 0"},
    "ps": {"label": "P/S", "name": "Price / Sales", "kind": "valuation", "better": "lower", "unit": "x",
           "formula": "Market cap ÷ revenue", "nm": "revenue ≤ 0"},
    "ev_ebitda": {"label": "EV/EBITDA", "name": "Enterprise Value / EBITDA", "kind": "valuation", "better": "lower",
                  "unit": "x", "formula": "(Market cap + total debt − cash & ST investments) ÷ EBITDA",
                  "nm": "EBITDA ≤ 0"},
    "p_ffo": {"label": "P/FFO", "name": "Price / Funds From Operations", "kind": "valuation", "better": "lower",
              "unit": "x", "formula": "Market cap ÷ (net income + depreciation & amortization)", "nm": "FFO ≤ 0"},
    "earnings_yield": {"label": "Earnings Yield", "name": "Earnings Yield", "kind": "valuation", "better": "higher",
                       "unit": "%", "formula": "Diluted EPS ÷ price",
                       "rule": "Buffett: compare with the 10-year Treasury yield"},
    "fcf_yield": {"label": "FCF Yield", "name": "Free Cash Flow Yield", "kind": "valuation", "better": "higher",
                  "unit": "%", "formula": "Free cash flow ÷ market cap"},
    "div_yield": {"label": "Div Yield", "name": "Dividend Yield", "kind": "valuation", "better": "higher",
                  "unit": "%", "formula": "Cash dividends paid ÷ market cap"},
    "peg": {"label": "PEG", "name": "P/E-to-Growth", "kind": "valuation", "better": "lower", "unit": "x",
            "formula": "P/E ÷ (EPS growth × 100)", "nm": "P/E is N/M or EPS growth ≤ 0", "rule": "Lynch: below 1×"},
    # profitability / quality
    "roe": {"label": "ROE", "name": "Return on Equity", "kind": "profitability", "better": "higher", "unit": "%",
            "formula": "Net income to common ÷ stockholders' equity", "nm": "equity ≤ 0",
            "rule": "Buffett: above 15%, year after year"},
    "roa": {"label": "ROA", "name": "Return on Assets", "kind": "profitability", "better": "higher", "unit": "%",
            "formula": "Net income to common ÷ total assets"},
    "roic": {"label": "ROIC", "name": "Return on Invested Capital", "kind": "profitability", "better": "higher",
             "unit": "%", "formula": "EBIT × (1 − tax rate) ÷ invested capital", "nm": "invested capital ≤ 0"},
    "gross_margin": {"label": "Gross Margin", "name": "Gross Margin", "kind": "profitability", "better": "higher",
                     "unit": "%", "formula": "Gross profit ÷ revenue", "rule": "Buffett: above 40% hints at a moat"},
    "op_margin": {"label": "Op Margin", "name": "Operating Margin", "kind": "profitability", "better": "higher",
                  "unit": "%", "formula": "Operating income ÷ revenue"},
    "net_margin": {"label": "Net Margin", "name": "Net Profit Margin", "kind": "profitability", "better": "higher",
                   "unit": "%", "formula": "Net income to common ÷ revenue", "rule": "Buffett: above 20%"},
    "fcf_margin": {"label": "FCF Margin", "name": "Free Cash Flow Margin", "kind": "profitability",
                   "better": "higher", "unit": "%", "formula": "Free cash flow ÷ revenue"},
    "capex_to_ni": {"label": "Capex / NI", "name": "Capex / Net Income", "kind": "profitability", "better": "lower",
                    "unit": "%", "formula": "Capital expenditure ÷ net income", "nm": "net income ≤ 0",
                    "rule": "Buffett: below 50% — earnings not eaten by reinvestment"},
    "rd_intensity": {"label": "R&D / Sales", "name": "R&D Intensity", "kind": "growth", "better": "higher",
                     "unit": "%", "formula": "Research & development ÷ revenue"},
    # growth
    "rev_growth": {"label": "Rev Growth", "name": "Revenue Growth (YoY)", "kind": "growth", "better": "higher",
                   "unit": "%", "formula": "Revenue ÷ revenue one year earlier − 1"},
    "eps_growth": {"label": "EPS Growth", "name": "EPS Growth (YoY)", "kind": "growth", "better": "higher",
                   "unit": "%", "formula": "(EPS − EPS one year earlier) ÷ EPS one year earlier",
                   "nm": "EPS one year earlier ≤ 0"},
    "ffo_growth": {"label": "FFO Growth", "name": "FFO Growth (YoY)", "kind": "growth", "better": "higher",
                   "unit": "%", "formula": "FFO ÷ FFO one year earlier − 1", "nm": "FFO one year earlier ≤ 0"},
    "div_growth": {"label": "Div Growth", "name": "Dividend Growth (YoY)", "kind": "growth", "better": "higher",
                   "unit": "%", "formula": "Dividends paid ÷ dividends paid one year earlier − 1",
                   "nm": "no dividend one year earlier"},
    # payout / balance-sheet health
    "payout_ratio": {"label": "Payout Ratio", "name": "Dividend Payout Ratio", "kind": "health", "better": "lower",
                     "unit": "%", "formula": "Cash dividends paid ÷ net income", "nm": "net income ≤ 0",
                     "rule": "below ~60% leaves room to keep paying"},
    "de": {"label": "D/E", "name": "Debt / Equity", "kind": "health", "better": "lower", "unit": "x",
           "formula": "Total debt ÷ stockholders' equity", "nm": "equity ≤ 0", "rule": "Buffett: below 0.5×"},
    "debt_to_ni": {"label": "Debt / NI", "name": "Years to Repay Debt", "kind": "health", "better": "lower",
                   "unit": "x", "formula": "Total debt ÷ net income", "nm": "net income ≤ 0",
                   "rule": "Buffett: repayable from 3–4 years of earnings"},
    "net_debt_ebitda": {"label": "Net Debt/EBITDA", "name": "Net Debt / EBITDA", "kind": "health",
                        "better": "lower", "unit": "x", "formula": "(Total debt − cash & ST investments) ÷ EBITDA",
                        "nm": "EBITDA ≤ 0"},
    "wc_to_assets": {"label": "WC / Assets", "name": "Working Capital / Total Assets", "kind": "health",
                     "better": "higher", "unit": "%", "formula": "(Current assets − current liabilities) ÷ total assets",
                     "rule": "Altman Z-score input: negative means short-term debts exceed short-term assets"},
    "wc_to_revenue": {"label": "WC / Sales", "name": "Working Capital / Revenue", "kind": "health",
                      "better": "lower", "unit": "%", "formula": "(Current assets − current liabilities) ÷ revenue",
                      "nm": "revenue ≤ 0",
                      "rule": "less capital tied up to run the business; negative can mean customers pay in advance"},
    "ncav": {"label": "NCAV / Mcap", "name": "Net Current Asset Value / Market Cap", "kind": "valuation",
             "better": "higher", "unit": "%", "formula": "(Current assets − total liabilities) ÷ market cap",
             "rule": "Graham: above 150% (price below ⅔ of NCAV) is a 'net-net' bargain"},
    "current_ratio": {"label": "Current Ratio", "name": "Current Ratio", "kind": "health", "better": "higher",
                      "unit": "x", "formula": "Current assets ÷ current liabilities", "rule": "Graham: above 2×"},
    "interest_coverage": {"label": "Int. Coverage", "name": "Interest Coverage", "kind": "health",
                          "better": "higher", "unit": "x", "formula": "EBIT ÷ interest expense",
                          "nm": "no interest expense"},
}

GROWTH_PERIOD_NOTE = {
    "quarter": "growth = the period's last quarter vs the same quarter one year earlier",
    "year": "growth = the fiscal year vs the fiscal year before",
}

# Stock categories: every category lists ALL its ratios; the user keeps all or picks some.
CATEGORIES: Dict[str, Dict[str, Any]] = {
    "value": {
        "label": "Value", "tagline": "Cheap relative to what the business already earns",
        "question": "Is the market under-pricing this company's earnings, assets and cash?",
        "source": "Graham & Buffett: a margin of safety on price, a durable business, little debt",
        "metrics": ["pe", "pb", "ptbv", "p_ffo", "ps", "ev_ebitda", "earnings_yield", "fcf_yield", "div_yield",
                    "ncav", "roe", "net_margin", "de", "debt_to_ni", "current_ratio", "interest_coverage"],
    },
    "growth": {
        "label": "Growth", "tagline": "Fast, profitable expansion that can justify a premium",
        "question": "Is this company growing faster — and better — than its peers?",
        "source": "Fisher / O'Neil style: sales and earnings growth backed by margins and returns",
        "metrics": ["rev_growth", "eps_growth", "ffo_growth", "peg", "gross_margin", "op_margin", "roic",
                    "fcf_margin", "rd_intensity"],
    },
    "dividend": {
        "label": "Dividend", "tagline": "Reliable, growing cash returned to shareholders",
        "question": "Is the dividend high, growing and safely covered?",
        "source": "Income investing: yield, payout coverage and balance-sheet safety",
        "metrics": ["div_yield", "payout_ratio", "div_growth", "fcf_yield", "de", "interest_coverage"],
    },
    "quality": {
        "label": "Quality", "tagline": "High returns on capital, fat margins, strong balance sheet",
        "question": "Is this an exceptionally good business, whatever its price?",
        "source": "Buffett's 'wonderful company' tests and quality-factor research",
        "metrics": ["roe", "roic", "gross_margin", "op_margin", "net_margin", "capex_to_ni", "wc_to_revenue", "de",
                    "interest_coverage"],
    },
    "garp": {
        "label": "GARP", "tagline": "Growth at a reasonable price",
        "question": "Is the growth available without paying a growth-stock multiple?",
        "source": "Peter Lynch: PEG near or below 1 with solid growth and returns",
        "metrics": ["peg", "pe", "eps_growth", "rev_growth", "roe", "fcf_yield"],
    },
    "defensive": {
        "label": "Defensive", "tagline": "Can ride out a downturn",
        "question": "Would this company come through a recession intact?",
        "source": "Graham's defensive-investor criteria: liquidity, low debt, steady profits",
        "metrics": ["de", "current_ratio", "wc_to_assets", "interest_coverage", "debt_to_ni", "net_margin", "div_yield"],
    },
}

# Ratios that mean nothing for a sector are left out of its lists, statistics and pass rule.
_FFO_ONLY = {"p_ffo", "ffo_growth"}
_WORKING_CAPITAL = {"current_ratio", "wc_to_assets", "wc_to_revenue", "ncav"}   # need a current / non-current split
_RD_SECTORS = {"Information Technology", "Health Care"}
NOT_APPLICABLE: Dict[str, set] = {
    "Financials": {"ev_ebitda", "net_debt_ebitda", "current_ratio", "gross_margin", "op_margin", "roic",
                   "interest_coverage", "fcf_yield", "fcf_margin", "capex_to_ni", "debt_to_ni", "de", "ps"}
                  | _WORKING_CAPITAL,
    "Real Estate": set(_WORKING_CAPITAL),
}


def not_applicable(sector: Optional[str]) -> set:
    na = set(NOT_APPLICABLE.get(sector or "", set()))
    if sector != "Real Estate":
        na |= _FFO_ONLY
    if sector not in _RD_SECTORS:
        na.add("rd_intensity")
    return na


# Recommended starting set per sector for value / growth (why: "note", shown in the UI).
_DEFAULT = {
    "value": ["pe", "pb", "ev_ebitda", "earnings_yield", "fcf_yield", "roe", "de", "current_ratio"],
    "growth": ["rev_growth", "eps_growth", "peg", "gross_margin", "op_margin", "roic", "fcf_margin"],
}
SECTOR_PROFILES: Dict[str, Dict[str, Any]] = {
    "Information Technology": {
        "value": ["pe", "ev_ebitda", "ps", "fcf_yield", "pb", "de", "current_ratio"],
        "growth": ["rev_growth", "eps_growth", "peg", "gross_margin", "op_margin", "roic", "fcf_margin",
                   "rd_intensity"],
        "note": "Asset-light: sales and cash-flow multiples matter more than book value; R&D intensity signals "
                "reinvestment.",
    },
    "Communication Services": {
        "value": ["pe", "ev_ebitda", "ps", "fcf_yield", "div_yield", "de"],
        "growth": ["rev_growth", "eps_growth", "peg", "op_margin", "roic", "fcf_margin"],
        "note": "Mix of telecoms (debt, dividends) and internet/media (sales growth, margins).",
    },
    "Health Care": {
        "value": ["pe", "ev_ebitda", "ps", "fcf_yield", "pb", "de", "current_ratio"],
        "growth": ["rev_growth", "eps_growth", "peg", "gross_margin", "op_margin", "roic", "rd_intensity"],
        "note": "Pipelines are funded by R&D; gross margin separates pharma/devices from services.",
    },
    "Consumer Discretionary": {
        "value": ["pe", "ev_ebitda", "ps", "pb", "fcf_yield", "de", "current_ratio"],
        "growth": ["rev_growth", "eps_growth", "peg", "gross_margin", "op_margin", "roic"],
        "note": "Cyclical: EV/EBITDA and liquidity guard against peak-earnings traps.",
    },
    "Consumer Staples": {
        "value": ["pe", "ev_ebitda", "pb", "div_yield", "fcf_yield", "de", "current_ratio"],
        "growth": ["rev_growth", "eps_growth", "peg", "gross_margin", "op_margin", "roe"],
        "note": "Defensive income stocks: dividend yield is part of the value case.",
    },
    "Industrials": {
        "value": ["pe", "ev_ebitda", "pb", "fcf_yield", "div_yield", "de", "current_ratio"],
        "growth": ["rev_growth", "eps_growth", "peg", "op_margin", "roic", "fcf_margin"],
        "note": "Capital-intensive: ROIC and free cash flow show whether growth earns its capital.",
    },
    "Materials": {
        "value": ["ev_ebitda", "pe", "pb", "fcf_yield", "div_yield", "net_debt_ebitda"],
        "growth": ["rev_growth", "eps_growth", "op_margin", "roic", "fcf_margin"],
        "note": "Commodity cycles swing earnings, so EV/EBITDA and net debt/EBITDA lead; PEG is unstable.",
    },
    "Energy": {
        "value": ["ev_ebitda", "pe", "pb", "fcf_yield", "div_yield", "net_debt_ebitda"],
        "growth": ["rev_growth", "eps_growth", "op_margin", "roic", "fcf_margin"],
        "note": "Commodity-driven: cash-flow multiples and leverage over earnings multiples; PEG is unstable.",
    },
    "Utilities": {
        "value": ["pe", "pb", "ev_ebitda", "div_yield", "net_debt_ebitda", "interest_coverage"],
        "growth": ["rev_growth", "eps_growth", "peg", "roe", "op_margin"],
        "note": "Regulated, debt-funded: dividend yield and the ability to service debt drive value.",
    },
    "Real Estate": {
        "value": ["p_ffo", "pb", "div_yield", "ev_ebitda", "net_debt_ebitda"],
        "growth": ["rev_growth", "ffo_growth", "op_margin", "roe"],
        "note": "REITs: FFO (net income + depreciation) replaces earnings, since depreciation understates "
                "property value. FFO here is approximated (gains on property sales are not removed).",
    },
    "Financials": {
        "value": ["pe", "pb", "ptbv", "div_yield", "roe"],
        "growth": ["rev_growth", "eps_growth", "peg", "roe", "roa", "net_margin"],
        "note": "Banks and insurers: debt is raw material, not leverage — EV/EBITDA, margins and current ratio "
                "are meaningless; book value and returns on it lead.",
    },
}


def category_metrics(category: str, sector: Optional[str]) -> List[str]:
    """All ratios of a category that apply to the sector."""
    na = not_applicable(sector)
    return [m for m in CATEGORIES[category]["metrics"] if m not in na]


def recommended(category: str, sector: Optional[str]) -> List[str]:
    if category in ("value", "growth"):
        prof = SECTOR_PROFILES.get(sector or "") or _DEFAULT
        return [m for m in prof[category] if m in category_metrics(category, sector)]
    return category_metrics(category, sector)


def applicable_metrics(sector: Optional[str]) -> List[str]:
    na = not_applicable(sector)
    return [m for m in METRICS if m not in na]


def get_catalog() -> Dict[str, Any]:
    sectors = list(SECTOR_PROFILES)
    return {
        "metrics": METRICS,
        "categories": CATEGORIES,
        "category_order": list(CATEGORIES),
        "applicable": {s: {c: category_metrics(c, s) for c in CATEGORIES} for s in sectors},
        "recommended": {s: {c: recommended(c, s) for c in CATEGORIES} for s in sectors},
        "sector_notes": {s: SECTOR_PROFILES[s]["note"] for s in sectors},
        "periods": PERIOD_INFO,
        "period_order": list(PERIODS),
        "growth_period_note": GROWTH_PERIOD_NOTE,
        "levels": {"sector": "GICS sector", "sub_industry": "GICS sub-industry"},
        "thresholds": list(THRESHOLDS),
        "rules": {
            "quarter_lag_days": QUARTER_LAG_DAYS,
            "annual_lag_days": ANNUAL_LAG_DAYS,
            "outlier": f"from {OUTLIER_MIN_PEERS} peers on, values beyond {OUTLIER_IQR_K:g}×IQR of Q1/Q3 are "
                       "left out of the statistics",
            "small_group": SMALL_GROUP,
        },
    }


# ------------------------------------------------------------- statements ---

def _dates(stmt: Dict[str, Dict[str, float]], cutoff: date) -> List[str]:
    """Period ends <= cutoff, newest first. yfinance sometimes adds a nearly empty
    column (a few rows of a period not fully reported yet): a period counts only
    when at least half as many rows have it as the best-filled period."""
    counts: Dict[str, int] = {}
    for series in stmt.values():
        for d in series:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return []
    full = max(counts.values())
    return sorted((d for d, n in counts.items() if n * 2 >= full and date.fromisoformat(d) <= cutoff),
                  reverse=True)


def _pick(stmt: Dict[str, Dict[str, float]], rows: Iterable[str], d: str) -> Optional[float]:
    for row in rows:
        v = (stmt.get(row) or {}).get(d)
        if v is not None:
            return v
    return None


def _sum(stmt: Dict[str, Dict[str, float]], rows: Iterable[str], ds: List[str]) -> Optional[float]:
    """Sum over the dates, using the first row that has ALL of them."""
    for row in rows:
        series = stmt.get(row) or {}
        if all(d in series for d in ds):
            return sum(series[d] for d in ds)
    return None


def _consecutive_quarters(ds: List[str]) -> bool:
    days = [date.fromisoformat(d) for d in ds]
    return all(75 <= (a - b).days <= 105 for a, b in zip(days, days[1:]))


def _year_earlier(ds_all: List[str], d: str) -> Optional[str]:
    target = date.fromisoformat(d)
    for x in ds_all:
        if 340 <= (target - date.fromisoformat(x)).days <= 390:
            return x
    return None


_R = {
    "revenue": ("Total Revenue",),
    "gross": ("Gross Profit",),
    "opinc": ("Operating Income",),
    "ebit": ("EBIT", "Operating Income"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "ni": ("Net Income Common Stockholders", "Net Income"),
    "eps": ("Diluted EPS", "Basic EPS"),
    "rnd": ("Research And Development",),
    "interest": ("Interest Expense",),
    "tax_rate": ("Tax Rate For Calcs",),
    "da_inc": ("Reconciled Depreciation",),
    "fcf": ("Free Cash Flow",),
    "ocf": ("Operating Cash Flow",),
    "capex": ("Capital Expenditure",),
    "divs": ("Cash Dividends Paid", "Common Stock Dividend Paid"),
    "da_cf": ("Depreciation And Amortization", "Depreciation Amortization Depletion"),
    "equity": ("Stockholders Equity", "Common Stock Equity"),
    "tbv": ("Tangible Book Value",),
    "assets": ("Total Assets",),
    "debt": ("Total Debt",),
    "cash": ("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"),
    "ca": ("Current Assets",),
    "cl": ("Current Liabilities",),
    "invested": ("Invested Capital",),
    "shares": ("Ordinary Shares Number", "Share Issued"),
    "liabilities": ("Total Liabilities Net Minority Interest",),
}


def _flows(inc, cf, period: List[str]) -> Dict[str, Optional[float]]:
    """Income/cash-flow items summed over `period` (1 date = a year or a quarter)."""
    f = {k: _sum(inc, _R[k], period) for k in ("revenue", "gross", "opinc", "ebit", "ebitda", "ni", "eps",
                                              "rnd", "interest", "da_inc")}
    f.update({k: _sum(cf, _R[k], period) for k in ("fcf", "ocf", "capex", "divs", "da_cf")})
    da = f["da_cf"] if f["da_cf"] is not None else f["da_inc"]
    if f["ebitda"] is None and f["ebit"] is not None and da is not None:
        f["ebitda"] = f["ebit"] + da
    if f["fcf"] is None and f["ocf"] is not None and f["capex"] is not None:
        f["fcf"] = f["ocf"] + f["capex"]          # capex is reported negative
    if f["eps"] is None:                          # EPS line missing: net income / diluted shares per period
        per = [(_pick(inc, _R["ni"], d), _pick(inc, ("Diluted Average Shares",), d)) for d in period]
        if all(ni is not None and sh for ni, sh in per):
            f["eps"] = sum(ni / sh for ni, sh in per)
    f["ffo"] = f["ni"] + da if (f["ni"] is not None and da is not None) else None
    rates = [r for r in (_pick(inc, _R["tax_rate"], d) for d in period) if r is not None]
    f["tax_rate"] = sum(rates) / len(rates) if rates else None
    f["has_cf"] = bool(cf) and any(d in (cf.get(r) or {}) for r in cf for d in period)
    return f


def _balance(bs, d: Optional[str]) -> Dict[str, Optional[float]]:
    return {k: (_pick(bs, _R[k], d) if d else None)
            for k in ("equity", "tbv", "assets", "debt", "cash", "ca", "cl", "invested", "shares", "liabilities")}


def _latest_on_or_before(stmt, d: str) -> Optional[str]:
    ds = _dates(stmt, date.fromisoformat(d))
    return ds[0] if ds else None


def _div(a, b, positive_denominator=True):
    if a is None or b is None:
        return None
    if positive_denominator and b <= 0:
        return None
    if b == 0:
        return None
    return a / b


def _ratios(f: Dict[str, Any], b: Dict[str, Any], prev: Dict[str, Any], price: Optional[float],
            shares: Optional[float]) -> Tuple[Dict[str, Optional[float]], Dict[str, str]]:
    """Return (values, flags); flags[m] = 'nm' (not meaningful) or 'na' (data missing)."""
    v: Dict[str, Optional[float]] = {}
    flags: Dict[str, str] = {}
    mcap = price * shares if (price and shares) else None
    debt = b["debt"] if b["debt"] is not None else 0.0     # no debt line = no debt
    cash = b["cash"] if b["cash"] is not None else 0.0

    def put(key, value, nm_when=False):
        if nm_when:
            flags[key] = "nm"
            v[key] = None
        elif value is None:
            flags[key] = "na"
            v[key] = None
        else:
            v[key] = float(value)

    eps, equity, rev, ebitda, ffo = f["eps"], b["equity"], f["revenue"], f["ebitda"], f["ffo"]
    put("pe", _div(price, eps), eps is not None and price is not None and eps <= 0)
    put("pb", _div(mcap, equity), equity is not None and mcap is not None and equity <= 0)
    put("ptbv", _div(mcap, b["tbv"]), b["tbv"] is not None and mcap is not None and b["tbv"] <= 0)
    put("ps", _div(mcap, rev), rev is not None and mcap is not None and rev <= 0)
    ev = mcap + debt - cash if mcap is not None else None
    put("ev_ebitda", _div(ev, ebitda), ebitda is not None and ev is not None and ebitda <= 0)
    put("p_ffo", _div(mcap, ffo), ffo is not None and mcap is not None and ffo <= 0)
    put("fcf_yield", _div(f["fcf"], mcap))
    divs = f["divs"] if f["divs"] is not None else (0.0 if f["has_cf"] else None)
    put("div_yield", _div(abs(divs) if divs is not None else None, mcap))

    put("roe", _div(f["ni"], equity), equity is not None and f["ni"] is not None and equity <= 0)
    put("roa", _div(f["ni"], b["assets"]))
    tax = f["tax_rate"] if f["tax_rate"] is not None else DEFAULT_TAX_RATE
    tax = min(max(tax, 0.0), 0.5)
    nopat = f["ebit"] * (1 - tax) if f["ebit"] is not None else None
    put("roic", _div(nopat, b["invested"]), b["invested"] is not None and nopat is not None and b["invested"] <= 0)
    put("gross_margin", _div(f["gross"], rev))
    put("op_margin", _div(f["opinc"], rev))
    put("net_margin", _div(f["ni"], rev))
    put("fcf_margin", _div(f["fcf"], rev))
    rnd = f["rnd"] if f["rnd"] is not None else (0.0 if rev else None)   # no R&D line = no R&D spend
    put("rd_intensity", _div(rnd, rev))

    rev_ratio = _div(rev, prev.get("revenue"))
    put("rev_growth", rev_ratio - 1 if rev_ratio is not None else None)
    p_eps = prev.get("eps")
    put("eps_growth", (eps - p_eps) / abs(p_eps) if (eps is not None and p_eps) else None,
        p_eps is not None and eps is not None and p_eps <= 0)
    p_ffo = prev.get("ffo")
    put("ffo_growth", (ffo / p_ffo - 1) if (ffo is not None and p_ffo and p_ffo > 0) else None,
        p_ffo is not None and ffo is not None and p_ffo <= 0)
    g = v.get("eps_growth")
    if v.get("pe") is None or flags.get("eps_growth") == "nm":
        put("peg", None, flags.get("pe") == "nm" or flags.get("eps_growth") == "nm")
    else:
        put("peg", _div(v["pe"], g * 100) if g is not None else None, g is not None and g <= 0)

    put("de", _div(b["debt"], equity), equity is not None and b["debt"] is not None and equity <= 0)
    put("net_debt_ebitda", _div(debt - cash, ebitda) if b["debt"] is not None else None,
        ebitda is not None and b["debt"] is not None and ebitda <= 0)
    put("current_ratio", _div(b["ca"], b["cl"]))
    wc = b["ca"] - b["cl"] if (b["ca"] is not None and b["cl"] is not None) else None
    put("wc_to_assets", _div(wc, b["assets"]))
    put("wc_to_revenue", _div(wc, rev), wc is not None and rev is not None and rev <= 0)
    ncav = b["ca"] - b["liabilities"] if (b["ca"] is not None and b["liabilities"] is not None) else None
    put("ncav", _div(ncav, mcap))
    ni = f["ni"]
    ni_nm = ni is not None and ni <= 0
    put("earnings_yield", _div(eps, price))
    put("payout_ratio", _div(abs(divs) if divs is not None else None, ni), ni_nm and divs is not None)
    put("capex_to_ni", _div(abs(f["capex"]) if f["capex"] is not None else None, ni),
        ni_nm and f["capex"] is not None)
    put("debt_to_ni", _div(debt if b["debt"] is not None or b["equity"] is not None else None, ni), ni_nm)
    p_div = prev.get("divs")
    if p_div is None and prev.get("has_cf"):
        p_div = 0.0
    cur_div = abs(divs) if divs is not None else None
    put("div_growth", (cur_div / abs(p_div) - 1) if (cur_div is not None and p_div) else None,
        p_div is not None and cur_div is not None and p_div == 0)
    interest = f["interest"]
    put("interest_coverage", _div(f["ebit"], abs(interest)) if interest else None,
        f["ebit"] is not None and not interest)
    return v, flags


_GROWTH_KEYS = ("rev_growth", "eps_growth", "ffo_growth", "div_growth")


def _shares_at(bs, d: Optional[str]) -> Optional[float]:
    return _pick(bs, _R["shares"], d) if d else None


def compute_ticker(statements: Dict[str, Any], price: Optional[float], as_of: str,
                   price_at: Optional[Callable[[str], Optional[float]]] = None) -> Dict[str, Any]:
    """
    Ratios for q0 / q1 / fy0 / fy1 from a get_statements()['statements'] dict, as known at as_of.
    price = close on/before as_of (used by q0 and fy0); price_at(period_end) = close on/before a
    date (used by q1 and fy1). Each period is None when its statements are not available.
    """
    as_of_d = date.fromisoformat(as_of)
    q_cut = as_of_d - timedelta(days=QUARTER_LAG_DAYS)
    a_cut = as_of_d - timedelta(days=ANNUAL_LAG_DAYS)
    qi, qb, qc = (statements.get(k) or {} for k in ("quarterly_income", "quarterly_balance", "quarterly_cashflow"))
    ai, ab, ac = (statements.get(k) or {} for k in ("annual_income", "annual_balance", "annual_cashflow"))

    # fallback share count: latest balance sheet published before as_of
    fallback_shares = None
    for bs, cut in ((qb, q_cut), (ab, a_cut)):
        d = (_dates(bs, cut) or [None])[0]
        if _shares_at(bs, d):
            fallback_shares = _shares_at(bs, d)
            break

    out: Dict[str, Any] = {"price": price, "market_cap": price * fallback_shares if (price and fallback_shares) else None,
                           "periods": {}, "notes": []}
    for p in PERIODS:
        out[p] = None

    def finish(key, f, b, prev, px, shares, info):
        vals, flags = _ratios(f, b, prev, px, shares)
        out[key] = {"values": vals, "flags": flags}
        out["periods"][key] = {**info, "price": px, "market_cap": px * shares if (px and shares) else None}

    # ---- quarters: TTM windows ending at the latest and the previous quarter ----
    q_all = _dates(qi, q_cut)
    for k, key in enumerate(("q0", "q1")):
        window = q_all[k:k + 4]
        if len(window) < 4 or not _consecutive_quarters(window):
            out["notes"].append(f"{PERIOD_INFO[key]['label']}: only {len(window)} consecutive quarter(s) "
                                f"published before as_of in yfinance")
            continue
        bs_d = _latest_on_or_before(qb, window[0])
        b = _balance(qb, bs_d)
        shares = b["shares"] or fallback_shares
        px = price if k == 0 else (price_at(window[0]) if price_at else None)
        vals_f = _flows(qi, qc, window)
        yago = _year_earlier(q_all, window[0])
        last_q = _flows(qi, qc, [window[0]])
        prev_q = _flows(qi, qc, [yago]) if yago else {}
        finish(key, vals_f, b, {}, px, shares, {"quarters": window, "end": window[0], "growth_vs": yago})
        # growth: the window's last quarter vs the same quarter a year earlier
        gv, gf = _ratios(last_q, b, prev_q, px, shares)
        vals, flags = out[key]["values"], out[key]["flags"]
        for g in _GROWTH_KEYS:
            vals[g] = gv.get(g)
            flags.pop(g, None)
            if g in gf:
                flags[g] = gf[g]
        if not yago:
            for g in _GROWTH_KEYS:
                flags[g] = "na"
        eg = vals.get("eps_growth")
        flags.pop("peg", None)
        if vals.get("pe") is not None and eg is not None and eg > 0:
            vals["peg"] = vals["pe"] / (eg * 100)
        else:
            vals["peg"] = None
            flags["peg"] = "nm" if (flags.get("pe") == "nm" or flags.get("eps_growth") == "nm"
                                    or (eg is not None and eg <= 0)) else "na"

    # ---- fiscal years ----
    fy_all = _dates(ai, a_cut)
    for k, key in enumerate(("fy0", "fy1")):
        if len(fy_all) <= k:
            out["notes"].append(f"{PERIOD_INFO[key]['label']}: not published before as_of in yfinance")
            continue
        fy = fy_all[k]
        b = _balance(ab, fy)
        shares = b["shares"] or fallback_shares
        px = price if k == 0 else (price_at(fy) if price_at else None)
        prev = _flows(ai, ac, [fy_all[k + 1]]) if len(fy_all) > k + 1 else {}
        finish(key, _flows(ai, ac, [fy]), b, prev, px, shares,
               {"end": fy, "growth_vs": fy_all[k + 1] if len(fy_all) > k + 1 else None})
    return out


# --------------------------------------------------------------- benchmark --

def _quantile(sorted_vals: List[float], q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def benchmark_stats(pairs: List[Tuple[str, float]], nm_count: int = 0, na_count: int = 0) -> Dict[str, Any]:
    """pairs = (ticker, value) for every peer with a value. Outliers dropped from the stats."""
    vals = sorted(v for _, v in pairs)
    outliers: List[List[Any]] = []
    clean = pairs
    if len(vals) >= OUTLIER_MIN_PEERS:
        q1, q3 = _quantile(vals, 0.25), _quantile(vals, 0.75)
        iqr = q3 - q1
        lo, hi = q1 - OUTLIER_IQR_K * iqr, q3 + OUTLIER_IQR_K * iqr
        clean = [(t, v) for t, v in pairs if lo <= v <= hi]
        outliers = sorted([[t, v] for t, v in pairs if not lo <= v <= hi], key=lambda x: x[1])
    cv = sorted(v for _, v in clean)
    base = {"n": len(cv), "nm": nm_count, "na": na_count, "outliers": outliers,
            "values": sorted([[t, v] for t, v in clean], key=lambda x: x[1])}
    if not cv:
        return {**base, "min": None, "q1": None, "median": None, "mean": None, "q3": None, "max": None}
    return {**base, "min": cv[0], "q1": _quantile(cv, 0.25), "median": _quantile(cv, 0.5),
            "mean": sum(cv) / len(cv), "q3": _quantile(cv, 0.75), "max": cv[-1]}


def rank_in_group(value: Optional[float], ticker: str, stats: Dict[str, Any], better: str) -> Optional[Dict[str, Any]]:
    """Direction-adjusted percentile (100 = best), side of the median, raw min/max.
    Mirrored in FundamentalLab.vue (rankIn) — keep the two in sync."""
    if value is None or not stats or not stats["n"]:
        return None
    others = [v for t, v in stats["values"] if t != ticker]
    if others:
        below = sum(1 for v in others if v < value)
        equal = sum(1 for v in others if v == value)
        pct_low = (below + 0.5 * equal) / len(others) * 100     # percentile by raw value
    else:
        pct_low = 50.0
    pct = pct_low if better == "higher" else 100 - pct_low
    med = stats["median"]
    if value == med:
        side = "equal"
    else:
        side = "better" if (value > med) == (better == "higher") else "worse"
    extreme = "max" if value >= stats["max"] else ("min" if value <= stats["min"] else None)
    return {"pct": round(pct, 1), "side": side, "extreme": extreme,
            "outlier": any(t == ticker for t, _ in stats["outliers"])}


def _group_of(entry: Dict[str, Any], level: str) -> str:
    return (entry.get("gics_sub_industry") if level == "sub_industry" else entry.get("gics_sector")) or "Unclassified"


def build_benchmarks(entries: List[Dict[str, Any]], per_ticker: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Stats per period / level / group / metric (every ratio that applies to the group's sector)."""
    benchmarks: Dict[str, Any] = {}
    for period in PERIODS:
        benchmarks[period] = {}
        for level in LEVELS:
            groups: Dict[str, List[Dict[str, Any]]] = {}
            for e in entries:
                groups.setdefault(_group_of(e, level), []).append(e)
            bl = {}
            for g, members in groups.items():
                metric_keys = list(dict.fromkeys(m for e in members for m in applicable_metrics(e.get("gics_sector"))))
                gs = {}
                for m in metric_keys:
                    pairs, nm, na = [], 0, 0
                    for e in members:
                        data = (per_ticker.get(e["ticker"]) or {}).get(period)
                        val = data["values"].get(m) if data else None
                        if val is not None:
                            pairs.append((e["ticker"], val))
                        elif data and data["flags"].get(m) == "nm":
                            nm += 1
                        else:
                            na += 1
                    gs[m] = benchmark_stats(pairs, nm, na)
                bl[g] = {"peers": len(members), "small": len(members) < SMALL_GROUP, "metrics": gs}
            benchmarks[period][level] = bl
    return benchmarks


def _check_cfg(category: str, period: str, level: str) -> None:
    if category not in CATEGORIES or period not in PERIODS or level not in LEVELS:
        raise ValueError("category / period / level not recognised")


def evaluate(result: Dict[str, Any], tickers: List[str], category: str, period: str, level: str,
             threshold: float, metrics: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """
    Pass/score per ticker. metrics = the ratios the user picked (None = all of the category);
    per company only those that apply to its sector count.
    """
    _check_cfg(category, period, level)
    chosen = [m for m in (metrics or CATEGORIES[category]["metrics"]) if m in CATEGORIES[category]["metrics"]]
    out = {}
    for t in tickers:
        info = result["tickers"].get(t)
        if not info:
            continue
        sector = info["sector"]
        ms = [m for m in chosen if m not in not_applicable(sector)]
        group = info["sub_industry"] if level == "sub_industry" else sector
        stats = ((result["benchmarks"][period][level].get(group) or {}).get("metrics") or {})
        data = info.get(period) or {}
        values, flags = data.get("values") or {}, data.get("flags") or {}
        ranks = {m: rank_in_group(values.get(m), t, stats.get(m), METRICS[m]["better"]) for m in ms}
        beats = [m for m in ms if (ranks[m] or {}).get("side") == "better"]
        # score: N/M (e.g. losses for P/E) counts as the bottom percentile, missing data is skipped;
        # with data on fewer than half of the ratios there is no score
        pcts = [ranks[m]["pct"] if ranks[m] else 0.0 for m in ms if ranks[m] or flags.get(m) == "nm"]
        scored = len(pcts) * 2 >= len(ms) and pcts
        out[t] = {
            "sector": sector, "group": group, "metrics": ms, "beats": beats, "beat_count": len(beats),
            "n": len(ms), "coverage": len(pcts), "score": round(sum(pcts) / len(pcts), 1) if scored else None,
            "passes": bool(ms) and len(beats) >= math.ceil(threshold * len(ms) - 1e-9),
            "ranks": ranks, "values": {m: values.get(m) for m in ms},
        }
    return out


# ------------------------------------------------------------------- task ---

_BY_SCREEN: Dict[str, str] = {}
_LOCK = threading.Lock()


def _screen_entries(screen_task_id: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from .step1_universe import TASK_TYPE as SCREEN_TASK
    task = TaskManager().get_task(screen_task_id)
    if task is None or task.task_type != SCREEN_TASK:
        raise LookupError("Screening result not found (backend restarted?) — please screen again")
    if task.status != TaskStatus.COMPLETED:
        raise ValueError("Screening has not finished yet")
    screen = task.result
    return screen, screen["universe"] + screen["filtered_out"]


def _price_on(ticker: str, as_of: str) -> Tuple[Optional[float], Optional[str]]:
    p = get_price_data(ticker, as_of_date=as_of)
    if not p.get("success"):
        return None, None
    return p.get("latest_close"), p.get("latest_date")


def _close_lookup(history: Dict[str, float]) -> Callable[[str], Optional[float]]:
    """close on or before a date (up to 10 days back, e.g. a period ending on a weekend)."""
    def at(d: str) -> Optional[float]:
        day = date.fromisoformat(d)
        for back in range(11):
            v = history.get((day - timedelta(days=back)).isoformat())
            if v is not None:
                return v
        return None
    return at


def start_fundamentals(screen_task_id: str) -> str:
    screen, entries = _screen_entries(screen_task_id)
    tm = TaskManager()
    with _LOCK:
        existing = _BY_SCREEN.get(screen_task_id)
        if existing:
            t = tm.get_task(existing)
            if t is not None and t.status != TaskStatus.FAILED:
                return existing
        task_id = tm.create_task(TASK_TYPE, metadata={"screen_task_id": screen_task_id,
                                                      "as_of_date": screen["as_of_date"]})
        _BY_SCREEN[screen_task_id] = task_id

    as_of = screen["as_of_date"]
    as_of_d = date.fromisoformat(as_of)
    history: Dict[str, Dict[str, float]] = {}

    def one(entry):
        t = entry["ticker"]
        try:
            st = get_statements(t)
            price, price_date = _price_on(t, as_of)
            if not st.get("success"):
                return t, None, st.get("error") or "no statements"
            data = compute_ticker(st["statements"], price, as_of, _close_lookup(history.get(t, {})))
            data["price_date"] = price_date
            return t, data, None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"fundamentals {t} failed: {e}")
            return t, None, str(e)

    def worker():
        try:
            tm.update_task(task_id, status=TaskStatus.PROCESSING, message="Downloading price history",
                           progress_detail={"processed": 0, "total": len(entries), "current": "price history"})
            try:   # quoted closes for the earlier periods' valuation (one batch download)
                history.update(fetch_close_history([e["ticker"] for e in entries],
                                                   as_of_d - timedelta(days=PRICE_HISTORY_DAYS), as_of_d,
                                                   adjusted=False))
            except Exception as e:  # noqa: BLE001 — earlier periods then simply lack valuation ratios
                logger.warning(f"price history for fundamentals failed: {e}")
            tm.update_task(task_id, message="Fetching financial statements")
            per_ticker: Dict[str, Any] = {}
            failed: Dict[str, str] = {}
            done = 0
            with ThreadPoolExecutor(FETCH_WORKERS) as ex:
                for t, data, err in ex.map(one, entries):
                    done += 1
                    if data is None:
                        failed[t] = err
                    else:
                        per_ticker[t] = data
                    tm.update_task(task_id, progress=min(int(done * 95 / max(len(entries), 1)), 95),
                                   message=f"{done}/{len(entries)} · {t}",
                                   progress_detail={"processed": done, "total": len(entries), "current": t})
            tm.update_task(task_id, progress=97, message="Benchmarking sectors and industries")
            benchmarks = build_benchmarks(entries, per_ticker)
            tickers = {}
            for e in entries:
                t = e["ticker"]
                d = per_ticker.get(t) or {}
                tickers[t] = {
                    "company_name": e.get("company_name"), "sector": e.get("gics_sector"),
                    "sub_industry": e.get("gics_sub_industry") or "Unclassified",
                    "market_cap_tier": e.get("market_cap_tier"),
                    "price": d.get("price"), "price_date": d.get("price_date"),
                    "market_cap": d.get("market_cap"), "periods": d.get("periods", {}),
                    **{p: d.get(p) for p in PERIODS},
                    "notes": d.get("notes", []) + ([f"no data: {failed[t]}"] if t in failed else []),
                }
            coverage = {p: sum(1 for d in per_ticker.values() if d.get(p)) for p in PERIODS}
            period_ends = {p: sorted({d["periods"][p]["end"] for d in per_ticker.values() if d.get(p)},
                                     reverse=True)[:3] for p in PERIODS}
            tm.complete_task(task_id, {
                "screen_task_id": screen_task_id,
                "as_of_date": as_of,
                "cutoffs": {"quarter": (as_of_d - timedelta(days=QUARTER_LAG_DAYS)).isoformat(),
                            "annual": (as_of_d - timedelta(days=ANNUAL_LAG_DAYS)).isoformat()},
                "total": len(entries),
                "coverage": coverage,
                "period_ends": period_ends,
                "failed": failed,
                "tickers": tickers,
                "benchmarks": benchmarks,
                "built_at": datetime.now().isoformat(timespec='seconds'),
            })
        except Exception as e:  # noqa: BLE001
            logger.exception("fundamentals task failed")
            tm.fail_task(task_id, str(e))

    threading.Thread(target=worker, daemon=True).start()
    return task_id


def get_fundamentals(task_id: str) -> Optional[Dict[str, Any]]:
    task = TaskManager().get_task(task_id)
    if task is None or task.task_type != TASK_TYPE:
        return None
    return task.to_dict()


def lock_record(fund_task_id: str, screen_task_id: str, tickers: List[str], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """What 01_universe.json stores about the fundamental filter used at lock time."""
    task = TaskManager().get_task(fund_task_id)
    if task is None or task.task_type != TASK_TYPE or task.status != TaskStatus.COMPLETED:
        raise LookupError("Fundamentals result not found (backend restarted?) — reload fundamentals")
    result = task.result
    if result["screen_task_id"] != screen_task_id:
        raise ValueError("Fundamentals belong to a different screen — reload fundamentals")
    category = cfg.get("category", "value")
    period, level = cfg.get("period", "q0"), cfg.get("level", "sector")
    _check_cfg(category, period, level)
    threshold = float(cfg.get("threshold", 0.5))
    metrics = cfg.get("metrics") or None
    ev = evaluate(result, tickers, category, period, level, threshold, metrics)
    groups = sorted({e["group"] for e in ev.values()})
    bench = result["benchmarks"][period][level]
    chosen = sorted({m for e in ev.values() for m in e["metrics"]}, key=list(METRICS).index)
    return {
        "category": category, "period": period, "level": level, "threshold": threshold,
        "metrics": chosen,
        "cutoffs": result["cutoffs"],
        "passing": sorted(t for t, e in ev.items() if e["passes"]),
        "evaluation": ev,
        "periods": {t: result["tickers"][t]["periods"].get(period) for t in tickers if t in result["tickers"]},
        "group_stats": {
            g: {m: {k: s[k] for k in ("n", "min", "q1", "median", "mean", "q3", "max")}
                for m, s in bench[g]["metrics"].items() if m in chosen}
            for g in groups if g in bench
        },
    }
