"""
Step 1 · Fundamental filter — financial ratios benchmarked per industry.

For every company of the screened sectors (ALL tiers, so the benchmark is the
whole S&P 500 industry, not only the tickers the user keeps):

  1. ratios from yfinance statements, on two bases
       ttm — last 4 quarters (flows summed; balance sheet = latest quarter)
       fy  — last full fiscal year (balance sheet = fiscal year end)
     Only periods PUBLISHED before as_of are used: a quarter counts once
     QUARTER_LAG_DAYS have passed since it ended (10-Q deadline is 40 days),
     a fiscal year after ANNUAL_LAG_DAYS (10-K deadline 60-90 days).
     Price = close on/before as_of; market cap = that price x latest shares.
  2. benchmark per GICS sub-industry (and per sector): n, min, Q1, median,
     mean, Q3, max. "N/M" values (e.g. P/E with negative EPS) and, from 8
     peers on, outliers beyond 3 x IQR are left out of the statistics.
  3. rank of each company per ratio: direction-adjusted percentile (100 = best
     in the industry), better/worse than the median, raw min/max.

Which ratios count depends on the lens (value / growth) and the sector
(SECTOR_PROFILES) — a bank is not judged by EV/EBITDA, a REIT by P/FFO.
A company PASSES when it beats its industry median on at least `threshold`
of the active ratios (N/M counts as not beating it).

compute_ticker / build_benchmarks / evaluate are pure; start_fundamentals runs
the fetch as a background task over a finished universe screen.
"""

import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..data_layer.financial_statements import get_statements
from ..data_layer.market_data import get_price_data
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
BASES = ("ttm", "fy")
LEVELS = ("sub_industry", "sector")
THRESHOLDS = (0.5, 0.6, 0.75, 1.0)

# better: which direction is good · unit: "x" multiple, "%" fraction shown as %
METRICS: Dict[str, Dict[str, str]] = {
    # valuation
    "pe": {"label": "P/E", "name": "Price / Earnings", "kind": "valuation", "better": "lower", "unit": "x",
           "formula": "Price ÷ diluted EPS", "nm": "EPS ≤ 0"},
    "pb": {"label": "P/B", "name": "Price / Book", "kind": "valuation", "better": "lower", "unit": "x",
           "formula": "Market cap ÷ stockholders' equity", "nm": "equity ≤ 0"},
    "ptbv": {"label": "P/TBV", "name": "Price / Tangible Book", "kind": "valuation", "better": "lower", "unit": "x",
             "formula": "Market cap ÷ tangible book value", "nm": "tangible book ≤ 0"},
    "ps": {"label": "P/S", "name": "Price / Sales", "kind": "valuation", "better": "lower", "unit": "x",
           "formula": "Market cap ÷ revenue", "nm": "revenue ≤ 0"},
    "ev_ebitda": {"label": "EV/EBITDA", "name": "Enterprise Value / EBITDA", "kind": "valuation", "better": "lower",
                  "unit": "x", "formula": "(Market cap + total debt − cash & ST investments) ÷ EBITDA",
                  "nm": "EBITDA ≤ 0"},
    "p_ffo": {"label": "P/FFO", "name": "Price / Funds From Operations", "kind": "valuation", "better": "lower",
              "unit": "x", "formula": "Market cap ÷ (net income + depreciation & amortization)", "nm": "FFO ≤ 0"},
    "fcf_yield": {"label": "FCF Yield", "name": "Free Cash Flow Yield", "kind": "valuation", "better": "higher",
                  "unit": "%", "formula": "Free cash flow ÷ market cap"},
    "div_yield": {"label": "Div Yield", "name": "Dividend Yield", "kind": "valuation", "better": "higher",
                  "unit": "%", "formula": "Cash dividends paid ÷ market cap"},
    "peg": {"label": "PEG", "name": "P/E-to-Growth", "kind": "valuation", "better": "lower", "unit": "x",
            "formula": "P/E ÷ (EPS growth × 100)", "nm": "P/E is N/M or EPS growth ≤ 0"},
    # profitability / quality
    "roe": {"label": "ROE", "name": "Return on Equity", "kind": "profitability", "better": "higher", "unit": "%",
            "formula": "Net income to common ÷ stockholders' equity", "nm": "equity ≤ 0"},
    "roa": {"label": "ROA", "name": "Return on Assets", "kind": "profitability", "better": "higher", "unit": "%",
            "formula": "Net income to common ÷ total assets"},
    "roic": {"label": "ROIC", "name": "Return on Invested Capital", "kind": "profitability", "better": "higher",
             "unit": "%", "formula": "EBIT × (1 − tax rate) ÷ invested capital", "nm": "invested capital ≤ 0"},
    "gross_margin": {"label": "Gross Margin", "name": "Gross Margin", "kind": "profitability", "better": "higher",
                     "unit": "%", "formula": "Gross profit ÷ revenue"},
    "op_margin": {"label": "Op Margin", "name": "Operating Margin", "kind": "profitability", "better": "higher",
                  "unit": "%", "formula": "Operating income ÷ revenue"},
    "net_margin": {"label": "Net Margin", "name": "Net Profit Margin", "kind": "profitability", "better": "higher",
                   "unit": "%", "formula": "Net income to common ÷ revenue"},
    "fcf_margin": {"label": "FCF Margin", "name": "Free Cash Flow Margin", "kind": "profitability",
                   "better": "higher", "unit": "%", "formula": "Free cash flow ÷ revenue"},
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
    # balance-sheet health
    "de": {"label": "D/E", "name": "Debt / Equity", "kind": "health", "better": "lower", "unit": "x",
           "formula": "Total debt ÷ stockholders' equity", "nm": "equity ≤ 0"},
    "net_debt_ebitda": {"label": "Net Debt/EBITDA", "name": "Net Debt / EBITDA", "kind": "health",
                        "better": "lower", "unit": "x", "formula": "(Total debt − cash & ST investments) ÷ EBITDA",
                        "nm": "EBITDA ≤ 0"},
    "current_ratio": {"label": "Current Ratio", "name": "Current Ratio", "kind": "health", "better": "higher",
                      "unit": "x", "formula": "Current assets ÷ current liabilities"},
    "interest_coverage": {"label": "Int. Coverage", "name": "Interest Coverage", "kind": "health",
                          "better": "higher", "unit": "x", "formula": "EBIT ÷ interest expense",
                          "nm": "no interest expense"},
}

GROWTH_BASIS_NOTE = {
    "ttm": "growth = latest quarter vs the same quarter one year earlier",
    "fy": "growth = last fiscal year vs the fiscal year before",
}

LENSES = {
    "value": {"label": "Value", "tagline": "Cheap relative to fundamentals, with a balance sheet that can wait",
              "question": "Is the market under-pricing what this company already earns?"},
    "growth": {"label": "Growth", "tagline": "Fast, profitable expansion that can justify a premium",
               "question": "Is this company growing faster — and better — than its industry?"},
}

# Why each sector differs from the default is written next to it (shown in the UI).
_DEFAULT = {
    "value": ["pe", "ev_ebitda", "ps", "pb", "fcf_yield", "de", "current_ratio"],
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


def profile_for(sector: Optional[str]) -> Dict[str, Any]:
    p = SECTOR_PROFILES.get(sector or "")
    return p if p else {**_DEFAULT, "note": "Default profile."}


def sector_metrics(sector: Optional[str]) -> List[str]:
    p = profile_for(sector)
    return list(dict.fromkeys(p["value"] + p["growth"]))


def get_catalog() -> Dict[str, Any]:
    return {
        "metrics": METRICS,
        "lenses": LENSES,
        "profiles": {s: profile_for(s) for s in SECTOR_PROFILES},
        "bases": {"ttm": "Last 4 quarters (TTM)", "fy": "Last full fiscal year"},
        "growth_basis_note": GROWTH_BASIS_NOTE,
        "levels": {"sub_industry": "GICS sub-industry", "sector": "GICS sector"},
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
            for k in ("equity", "tbv", "assets", "debt", "cash", "ca", "cl", "invested", "shares")}


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
    interest = f["interest"]
    put("interest_coverage", _div(f["ebit"], abs(interest)) if interest else None,
        f["ebit"] is not None and not interest)
    return v, flags


def compute_ticker(statements: Dict[str, Any], price: Optional[float], as_of: str) -> Dict[str, Any]:
    """Ratios on both bases from a get_statements()['statements'] dict, as known at as_of."""
    as_of_d = date.fromisoformat(as_of)
    q_cut = as_of_d - timedelta(days=QUARTER_LAG_DAYS)
    a_cut = as_of_d - timedelta(days=ANNUAL_LAG_DAYS)
    qi, qb, qc = (statements.get(k) or {} for k in ("quarterly_income", "quarterly_balance", "quarterly_cashflow"))
    ai, ab, ac = (statements.get(k) or {} for k in ("annual_income", "annual_balance", "annual_cashflow"))

    # shares for market cap: latest quarterly balance sheet published before as_of, else annual
    shares = None
    for bs, cut in ((qb, q_cut), (ab, a_cut)):
        d = (_dates(bs, cut) or [None])[0]
        if d and _pick(bs, _R["shares"], d):
            shares = _pick(bs, _R["shares"], d)
            break

    out: Dict[str, Any] = {"price": price, "shares": shares,
                           "market_cap": price * shares if (price and shares) else None,
                           "periods": {}, "ttm": None, "fy": None, "notes": []}

    # ---- TTM: 4 consecutive quarters ----
    q_all = _dates(qi, q_cut)
    q4 = q_all[:4]
    if len(q4) == 4 and _consecutive_quarters(q4):
        f = _flows(qi, qc, q4)
        b = _balance(qb, _latest_on_or_before(qb, q4[0]))
        yago = _year_earlier(_dates(qi, q_cut), q4[0])
        prev_q = _flows(qi, qc, [yago]) if yago else {}
        last_q = _flows(qi, qc, [q4[0]])
        # growth on TTM basis = latest quarter vs same quarter a year earlier
        vals, flags = _ratios(f, b, {}, price, shares)
        gv, gf = _ratios(last_q, b, prev_q, price, shares)
        for k in ("rev_growth", "eps_growth", "ffo_growth"):
            vals[k] = gv.get(k)
            flags.pop(k, None)
            if k in gf:
                flags[k] = gf[k]
        # PEG on TTM basis: TTM P/E over the latest-quarter EPS growth
        g = vals.get("eps_growth")
        flags.pop("peg", None)
        if vals.get("pe") is not None and g is not None and g > 0:
            vals["peg"] = vals["pe"] / (g * 100)
        else:
            vals["peg"] = None
            flags["peg"] = "nm" if (flags.get("pe") == "nm" or flags.get("eps_growth") == "nm"
                                    or (g is not None and g <= 0)) else "na"
        out["ttm"] = {"values": vals, "flags": flags}
        out["periods"]["ttm"] = {"quarters": q4, "growth_vs": yago}
    else:
        out["notes"].append(f"TTM unavailable: {len(q4)} consecutive quarter(s) published before as_of in yfinance"
                            if len(q4) < 4 else "TTM unavailable: quarters are not consecutive")

    # ---- FY: last full fiscal year ----
    fy_all = _dates(ai, a_cut)
    if fy_all:
        fy0 = fy_all[0]
        f = _flows(ai, ac, [fy0])
        b = _balance(ab, fy0)
        prev = _flows(ai, ac, [fy_all[1]]) if len(fy_all) > 1 else {}
        vals, flags = _ratios(f, b, prev, price, shares)
        out["fy"] = {"values": vals, "flags": flags}
        out["periods"]["fy"] = {"year_end": fy0, "growth_vs": fy_all[1] if len(fy_all) > 1 else None}
    else:
        out["notes"].append("FY unavailable: no fiscal year published before as_of in yfinance")
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
    outliers: List[str] = []
    clean = pairs
    if len(vals) >= OUTLIER_MIN_PEERS:
        q1, q3 = _quantile(vals, 0.25), _quantile(vals, 0.75)
        iqr = q3 - q1
        lo, hi = q1 - OUTLIER_IQR_K * iqr, q3 + OUTLIER_IQR_K * iqr
        clean = [(t, v) for t, v in pairs if lo <= v <= hi]
        outliers = sorted(t for t, v in pairs if not lo <= v <= hi)
    cv = sorted(v for _, v in clean)
    base = {"n": len(cv), "nm": nm_count, "na": na_count, "outliers": outliers,
            "values": sorted([[t, v] for t, v in clean], key=lambda x: x[1])}
    if not cv:
        return {**base, "min": None, "q1": None, "median": None, "mean": None, "q3": None, "max": None}
    return {**base, "min": cv[0], "q1": _quantile(cv, 0.25), "median": _quantile(cv, 0.5),
            "mean": sum(cv) / len(cv), "q3": _quantile(cv, 0.75), "max": cv[-1]}


def rank_in_group(value: Optional[float], ticker: str, stats: Dict[str, Any], better: str) -> Optional[Dict[str, Any]]:
    """Direction-adjusted percentile (100 = best), side of the median, raw min/max."""
    if value is None or not stats["n"]:
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
    return {"pct": round(pct, 1), "side": side, "extreme": extreme, "outlier": ticker in stats["outliers"]}


def _group_of(entry: Dict[str, Any], level: str) -> str:
    return (entry.get("gics_sub_industry") if level == "sub_industry" else entry.get("gics_sector")) or "Unclassified"


def build_benchmarks(entries: List[Dict[str, Any]], per_ticker: Dict[str, Dict[str, Any]]) -> Tuple[dict, dict]:
    """Stats per basis/level/group/metric and ranks per basis/level/ticker/metric."""
    benchmarks: Dict[str, Any] = {}
    ranks: Dict[str, Any] = {}
    for basis in BASES:
        benchmarks[basis], ranks[basis] = {}, {}
        for level in LEVELS:
            groups: Dict[str, List[Dict[str, Any]]] = {}
            for e in entries:
                groups.setdefault(_group_of(e, level), []).append(e)
            bl, rl = {}, {}
            for g, members in groups.items():
                metric_keys = list(dict.fromkeys(m for e in members for m in sector_metrics(e.get("gics_sector"))))
                gs = {}
                for m in metric_keys:
                    pairs, nm, na = [], 0, 0
                    for e in members:
                        data = (per_ticker.get(e["ticker"]) or {}).get(basis)
                        val = data["values"].get(m) if data else None
                        if val is not None:
                            pairs.append((e["ticker"], val))
                        elif data and data["flags"].get(m) == "nm":
                            nm += 1
                        else:
                            na += 1
                    gs[m] = benchmark_stats(pairs, nm, na)
                bl[g] = {"peers": len(members), "small": len(members) < SMALL_GROUP, "metrics": gs}
                for e in members:
                    data = (per_ticker.get(e["ticker"]) or {}).get(basis)
                    rl[e["ticker"]] = {
                        m: rank_in_group(data["values"].get(m) if data else None, e["ticker"], gs[m],
                                         METRICS[m]["better"])
                        for m in sector_metrics(e.get("gics_sector"))
                    }
            benchmarks[basis][level] = bl
            ranks[basis][level] = rl
    return benchmarks, ranks


def evaluate(result: Dict[str, Any], tickers: List[str], lens: str, basis: str, level: str,
             threshold: float, disabled: Optional[Dict[str, List[str]]] = None) -> Dict[str, Dict[str, Any]]:
    """Pass/score per ticker. disabled = {sector: [metric keys switched off in the UI]}."""
    if lens not in LENSES or basis not in BASES or level not in LEVELS:
        raise ValueError("lens / basis / level not recognised")
    disabled = disabled or {}
    out = {}
    for t in tickers:
        info = result["tickers"].get(t)
        if not info:
            continue
        sector = info["sector"]
        metrics = [m for m in profile_for(sector)[lens] if m not in set(disabled.get(sector, []))]
        rk = result["ranks"][basis][level].get(t, {})
        beats = [m for m in metrics if (rk.get(m) or {}).get("side") == "better"]
        flags = ((info.get(basis) or {}).get("flags") or {})
        # score: N/M (e.g. losses for P/E) counts as the bottom percentile, missing data is skipped;
        # with data on fewer than half of the ratios there is no score
        pcts = [rk[m]["pct"] if rk.get(m) else 0.0 for m in metrics if rk.get(m) or flags.get(m) == "nm"]
        scored = len(pcts) * 2 >= len(metrics) and pcts
        out[t] = {
            "sector": sector, "metrics": metrics, "beats": beats, "beat_count": len(beats),
            "n": len(metrics), "coverage": len(pcts), "score": round(sum(pcts) / len(pcts), 1) if scored else None,
            "passes": bool(metrics) and len(beats) >= math.ceil(threshold * len(metrics) - 1e-9),
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

    def one(entry):
        t = entry["ticker"]
        try:
            st = get_statements(t)
            price, price_date = _price_on(t, as_of)
            if not st.get("success"):
                return t, None, st.get("error") or "no statements"
            data = compute_ticker(st["statements"], price, as_of)
            data["price_date"] = price_date
            return t, data, None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"fundamentals {t} failed: {e}")
            return t, None, str(e)

    def worker():
        try:
            tm.update_task(task_id, status=TaskStatus.PROCESSING, message="Fetching financial statements")
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
            tm.update_task(task_id, progress=97, message="Benchmarking industries")
            benchmarks, ranks = build_benchmarks(entries, per_ticker)
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
                    "ttm": d.get("ttm"), "fy": d.get("fy"),
                    "notes": d.get("notes", []) + ([f"no data: {failed[t]}"] if t in failed else []),
                }
            coverage = {b: sum(1 for d in per_ticker.values() if d.get(b)) for b in BASES}
            as_of_d = date.fromisoformat(as_of)
            tm.complete_task(task_id, {
                "screen_task_id": screen_task_id,
                "as_of_date": as_of,
                "cutoffs": {"quarter": (as_of_d - timedelta(days=QUARTER_LAG_DAYS)).isoformat(),
                            "annual": (as_of_d - timedelta(days=ANNUAL_LAG_DAYS)).isoformat()},
                "total": len(entries),
                "coverage": coverage,
                "failed": failed,
                "tickers": tickers,
                "benchmarks": benchmarks,
                "ranks": ranks,
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
    lens, basis, level = cfg.get("lens", "value"), cfg.get("basis", "ttm"), cfg.get("level", "sub_industry")
    threshold = float(cfg.get("threshold", 0.5))
    disabled = cfg.get("disabled") or {}
    ev = evaluate(result, tickers, lens, basis, level, threshold, disabled)
    sectors = sorted({result["tickers"][t]["sector"] for t in tickers if t in result["tickers"]})
    return {
        "lens": lens, "basis": basis, "level": level, "threshold": threshold,
        "metrics_by_sector": {s: [m for m in profile_for(s)[lens] if m not in set(disabled.get(s, []))]
                              for s in sectors},
        "cutoffs": result["cutoffs"],
        "passing": sorted(t for t, e in ev.items() if e["passes"]),
        "evaluation": ev,
        "values": {t: {"periods": result["tickers"][t]["periods"],
                       "values": (result["tickers"][t].get(basis) or {}).get("values"),
                       "ranks": result["ranks"][basis][level].get(t)}
                   for t in tickers if t in result["tickers"]},
        "group_stats": {
            g: {m: {k: s[k] for k in ("n", "min", "q1", "median", "mean", "q3", "max")}
                for m, s in result["benchmarks"][basis][level][g]["metrics"].items()}
            for g in sorted({_group_of({"gics_sub_industry": result["tickers"][t]["sub_industry"],
                                        "gics_sector": result["tickers"][t]["sector"]}, level)
                             for t in tickers if t in result["tickers"]})
        },
    }
