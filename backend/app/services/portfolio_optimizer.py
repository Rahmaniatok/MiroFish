"""
Phase 5e — turn the screened, ranked candidate list into actual portfolio weights.

Phase 5b (`consensus_screener`) selects and ranks compliant candidates using the
LLM-derived consensus scores. Per the Option A decision, those scores are used
ONLY for candidate selection / ranking - they are NOT an input to the
optimization math here. This module takes the tickers that survive Phase 5b and
runs a classical mean-variance (max-Sharpe) optimization on real historical
returns:

    screen_and_rank(candidates)            # Phase 5b  - compliant, ranked top-K
      -> build_returns_matrix(tickers)     # daily returns from lookahead-safe price data (Phase 1)
      -> optimize_portfolio(tickers)       # skfolio MeanRisk, Ledoit-Wolf Σ, long-only, capped
      -> build_portfolio(candidates)       # the end-to-end orchestration

Placement: `app/services/`, alongside `consensus_screener` - this is
orchestration over the Phase 1 data layer and Phase 5b screener, not raw
data-layer logic. `app/data_layer/` stays free of portfolio concerns.
"""

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from skfolio.moments import LedoitWolf
from skfolio.optimization import MeanRisk, ObjectiveFunction
from skfolio.prior import EmpiricalPrior

from ..data_layer.market_data import fetch_price_data, get_price_data
from ..utils.logger import get_logger
from .consensus_screener import screen_and_rank

logger = get_logger("mirofish.portfolio")

# Annualisation factor for daily -> yearly return / vol / Sharpe (US trading days).
_TRADING_DAYS_PER_YEAR = 252

# Risk-free rate used for the Sharpe ratio and the max-ratio objective. Default
# is 0.0 (annualised) - CONFIGURABLE via `optimize_portfolio(risk_free_rate=...)`
# / `build_portfolio(risk_free_rate=...)`. Pass e.g. 0.04 for a ~4% T-bill.
_DEFAULT_RISK_FREE_RATE = 0.0

# A skfolio weight below this is treated as a true zero (the solver leaves ~1e-10
# dust on assets it wants out).
_WEIGHT_EPS = 1e-5

# Expected trading-day count per lookback string, for the "enough history?" test.
_LOOKBACK_TRADING_DAYS = {
    "1mo": 21, "3mo": 63, "6mo": 126,
    "1y": 252, "2y": 504, "3y": 756, "5y": 1260,
}


def _expected_trading_days(lookback: str) -> int:
    return _LOOKBACK_TRADING_DAYS.get(lookback, 252)


def _min_observations(lookback: str) -> int:
    """Minimum daily returns a ticker must have to stay in the matrix: half the
    lookback window, with an absolute floor of 40 (~2 months)."""
    return max(40, _expected_trading_days(lookback) // 2)


def _close_series(price_result: Dict[str, Any], ticker: str) -> Optional[pd.Series]:
    """`fetch_price_data`/`get_price_data` result -> a date-indexed close series."""
    ohlcv = price_result.get("ohlcv") or []
    rows = [(r.get("date"), r.get("close")) for r in ohlcv
            if r.get("date") and r.get("close") is not None]
    if not rows:
        return None
    dates, closes = zip(*rows)
    s = pd.Series(closes, index=pd.to_datetime(dates), name=ticker, dtype="float64")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def _fetch_close_series(
    ticker: str, as_of_date: Optional[str], lookback: str
) -> Dict[str, Any]:
    """Fetch one ticker's close series. Returns {"series": pd.Series|None, "reason": str|None}.

    Uses the cached `get_price_data` for the default 1y lookback; for any other
    lookback it must go to `fetch_price_data` directly, because the price cache
    key is (ticker, "price", as_of_date) with NO period component - reusing a 1y
    cache row for a 2y request would silently return the wrong window.
    """
    ticker = (ticker or "").strip().upper()
    try:
        if lookback == "1y":
            result = get_price_data(ticker, as_of_date=as_of_date)
        else:
            result = fetch_price_data(ticker, period=lookback, as_of_date=as_of_date)
    except Exception as exc:  # noqa: BLE001 - one bad ticker must not abort the batch
        return {"series": None, "reason": f"price fetch raised ({type(exc).__name__}: {exc})"}

    if not result or not result.get("success"):
        err = (result or {}).get("error") or "get/fetch_price_data returned success=False"
        return {"series": None, "reason": f"price data unavailable: {err}"}

    series = _close_series(result, ticker)
    if series is None or series.empty:
        return {"series": None, "reason": "no usable close prices in ohlcv"}
    return {"series": series, "reason": None}


def _build_returns_with_report(
    tickers: List[str],
    as_of_date: Optional[str],
    lookback: str,
) -> Dict[str, Any]:
    """Core of `build_returns_matrix`. Returns
    {"returns": pd.DataFrame, "dropped": [{"ticker", "reason"}, ...]}.

    `build_returns_matrix` is the thin public wrapper (returns the DataFrame
    only); `optimize_portfolio` needs the drop report too.
    """
    min_obs = _min_observations(lookback)
    seen: set = set()
    kept_series: Dict[str, pd.Series] = {}
    dropped: List[Dict[str, str]] = []

    for raw in tickers:
        ticker = (raw or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)

        fetched = _fetch_close_series(ticker, as_of_date, lookback)
        if fetched["series"] is None:
            logger.warning("returns matrix: drop %s - %s", ticker, fetched["reason"])
            dropped.append({"ticker": ticker, "reason": fetched["reason"]})
            continue

        series = fetched["series"]
        # one close series -> N-1 daily returns
        if len(series) - 1 < min_obs:
            reason = (
                f"insufficient history: {len(series)} close prices "
                f"({len(series) - 1} returns) < required {min_obs} for lookback={lookback}"
            )
            logger.warning("returns matrix: drop %s - %s", ticker, reason)
            dropped.append({"ticker": ticker, "reason": reason})
            continue

        kept_series[ticker] = series

    if len(kept_series) < 2:
        # Not enough to build a covariance matrix; hand back what we have and let
        # optimize_portfolio raise the clear error.
        cols = list(kept_series)
        prices = pd.concat(kept_series.values(), axis=1) if cols else pd.DataFrame()
        returns = prices.pct_change().iloc[1:] if not prices.empty else pd.DataFrame()
        return {"returns": returns, "dropped": dropped}

    # Outer-join every surviving close series on date, then drop any column whose
    # coverage is materially short of the fullest one (a NaN-riddled column would
    # otherwise poison pct_change / the covariance estimate). Never forward-fill
    # - that fabricates prices.
    prices = pd.concat(kept_series.values(), axis=1).sort_index()
    non_null = prices.notna().sum()
    fullest = int(non_null.max())
    coverage_floor = max(min_obs + 1, int(0.9 * fullest))

    thin = [c for c in prices.columns if int(non_null[c]) < coverage_floor]
    for c in thin:
        reason = (
            f"history too short vs peers: {int(non_null[c])} of {fullest} trading days "
            f"(< {coverage_floor}); would introduce gaps in the returns matrix"
        )
        logger.warning("returns matrix: drop %s - %s", c, reason)
        dropped.append({"ticker": c, "reason": reason})
    prices = prices.drop(columns=thin)

    # Rows where every surviving ticker has a price -> a clean, gap-free matrix.
    prices = prices.dropna(how="any")
    returns = prices.pct_change().iloc[1:]
    returns = returns.dropna(how="any")

    logger.info(
        "returns matrix: %d ticker(s) x %d trading days (lookback=%s, as_of=%s); %d dropped",
        returns.shape[1], returns.shape[0], lookback, as_of_date or "live", len(dropped),
    )
    return {"returns": returns, "dropped": dropped}


def build_returns_matrix(
    tickers: List[str],
    as_of_date: Optional[str] = None,
    lookback: str = "1y",
) -> pd.DataFrame:
    """Daily percentage-return matrix for `tickers` - assets as columns, dates as
    rows, aligned on a common gap-free date index.

    Prices come from the Phase 1 lookahead-safe path (`get_price_data` for the
    default 1y lookback, `fetch_price_data` otherwise), honouring `as_of_date`
    exactly as those functions do. Tickers with no data or with materially less
    history than their peers are dropped (and logged, with the reason) rather
    than left as NaN gaps in the matrix.
    """
    return _build_returns_with_report(tickers, as_of_date, lookback)["returns"]


def optimize_portfolio(
    tickers: List[str],
    as_of_date: Optional[str] = None,
    max_weight: float = 0.20,
    *,
    lookback: str = "1y",
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
    objective_function: ObjectiveFunction = ObjectiveFunction.MAXIMIZE_RATIO,
) -> Dict[str, Any]:
    """Mean-variance optimize `tickers` on real historical returns.

    - long-only, fully invested: weights >= 0, sum to 1
    - per-asset cap: weight <= `max_weight`
    - covariance: Ledoit-Wolf shrinkage (`EmpiricalPrior(covariance_estimator=LedoitWolf())`)
    - objective: max-Sharpe (`MAXIMIZE_RATIO`) by default; mean from the sample mean
    - `risk_free_rate` is ANNUALISED (default 0.0, configurable) and feeds both
      the objective and the reported Sharpe.

    Returns a dict:
      {
        "weights": {ticker: weight, ...},     # nonzero positions only, sum ~= 1
        "expected_return": float,             # annualised
        "volatility": float,                  # annualised standard deviation
        "sharpe_ratio": float,                # annualised, net of risk_free_rate
        "risk_free_rate": float,              # annualised, as used
        "risk_free_rate_note": str,           # flags that it's configurable
        "objective": str,
        "lookback": str,
        "n_assets_optimized": int,
        "tickers_in": [...],                  # what was passed in
        "dropped": [{"ticker", "reason"}, ...],   # from build_returns_matrix
      }

    Raises:
        ValueError: if fewer than 2 tickers have usable history, or if the
            per-asset cap makes a long-only sum-to-1 portfolio infeasible
            (n_assets * max_weight < 1).
    """
    if not 0 < max_weight <= 1:
        raise ValueError(f"max_weight must be in (0, 1], got {max_weight}")

    report = _build_returns_with_report(tickers, as_of_date, lookback)
    returns: pd.DataFrame = report["returns"]
    dropped = report["dropped"]
    assets = list(returns.columns)

    if len(assets) < 2:
        raise ValueError(
            f"Cannot optimize: only {len(assets)} ticker(s) have usable price history "
            f"({', '.join(assets) or 'none'}); need at least 2. "
            f"Dropped: {[d['ticker'] for d in dropped] or 'none'}."
        )

    # Feasibility of long-only + sum-to-1 + scalar per-asset cap.
    min_assets_needed = int(np.ceil(1.0 / max_weight - 1e-9))
    if len(assets) * max_weight < 1.0 - 1e-9:
        raise ValueError(
            f"Cannot optimize: {len(assets)} compliant tickers remain but "
            f"max_weight={max_weight:g} requires at least {min_assets_needed} "
            f"({len(assets)} * {max_weight:g} = {len(assets) * max_weight:.2f} < 1.0). "
            f"Reduce max_weight or provide more candidate tickers."
        )

    rf_annual = float(risk_free_rate)
    rf_period = (1.0 + rf_annual) ** (1.0 / _TRADING_DAYS_PER_YEAR) - 1.0

    model = MeanRisk(
        objective_function=objective_function,
        prior_estimator=EmpiricalPrior(covariance_estimator=LedoitWolf()),
        min_weights=0.0,            # long-only
        max_weights=float(max_weight),
        budget=1.0,                 # fully invested, weights sum to 1
        risk_free_rate=rf_period,
        portfolio_params={"risk_free_rate": rf_period},
    )
    model.fit(returns)
    portfolio = model.predict(returns)

    raw_weights = dict(zip(model.feature_names_in_, np.asarray(model.weights_, dtype=float)))
    weights = {
        t: round(float(w), 6)
        for t, w in sorted(raw_weights.items(), key=lambda kv: kv[1], reverse=True)
        if abs(w) > _WEIGHT_EPS
    }

    return {
        "weights": weights,
        "expected_return": float(portfolio.annualized_mean),
        "volatility": float(portfolio.annualized_standard_deviation),
        "sharpe_ratio": float(portfolio.annualized_sharpe_ratio),
        "risk_free_rate": rf_annual,
        "risk_free_rate_note": (
            "annualised; default 0.0, configurable via risk_free_rate= "
            "(feeds both the max-Sharpe objective and this ratio)"
        ),
        "objective": objective_function.name,
        "lookback": lookback,
        "n_assets_optimized": len(assets),
        "tickers_in": [(t or "").strip().upper() for t in tickers],
        "dropped": dropped,
    }


def build_portfolio(
    candidate_tickers: List[str],
    as_of_date: Optional[str] = None,
    top_k: int = 25,
    max_weight: float = 0.20,
    *,
    lookback: str = "1y",
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
) -> Dict[str, Any]:
    """End-to-end: screen + rank (Phase 5b) -> mean-variance optimize (Phase 5e).

    1. `screen_and_rank(candidate_tickers, as_of_date, top_k)` - the compliant,
       consensus-ranked top-K. Non-compliant names (JPM, GS, ...) never get here.
    2. `optimize_portfolio(...)` on exactly those tickers.

    Returns:
      {
        "as_of_date": str | None,
        "top_k": int, "max_weight": float,
        "n_candidates": int,                 # length of candidate_tickers
        "n_ranked": int,                     # survived the compliance + ranking screen
        "weights": {ticker: weight, ...},     # FINAL portfolio, nonzero only
        "portfolio": {expected_return, volatility, sharpe_ratio, risk_free_rate, ...},
        "holdings": [                          # one row per screened-in ticker
          {
            "ticker": str,
            "weight": float,                  # 0.0 if the optimizer zeroed it / it was dropped
            "consensus_score": float,         # TRANSPARENCY ONLY - not an optimizer input (Option A)
            "consensus_rank": int,            # 1 = highest consensus
            "passes_compliance": bool,
            "compliance_verdict": str,
            "in_optimization": bool,          # False if dropped for insufficient history
            "dropped_reason": str | None,
          }, ...
        ],
        "dropped_from_optimization": [{"ticker", "reason"}, ...],
      }
    """
    ranked = screen_and_rank(candidate_tickers, as_of_date=as_of_date, top_k=top_k)
    ranked_tickers = [r["ticker"] for r in ranked]

    opt = optimize_portfolio(
        ranked_tickers, as_of_date=as_of_date, max_weight=max_weight,
        lookback=lookback, risk_free_rate=risk_free_rate,
    )
    weights = opt["weights"]
    dropped_by_ticker = {d["ticker"]: d["reason"] for d in opt["dropped"]}

    holdings: List[Dict[str, Any]] = []
    for rank, r in enumerate(ranked, start=1):
        t = r["ticker"]
        holdings.append({
            "ticker": t,
            "weight": weights.get(t, 0.0),
            # consensus score is carried for reporting only - it is NOT fed into
            # the optimization math (Option A).
            "consensus_score": r["consensus_score"],
            "consensus_rank": rank,
            "passes_compliance": r["passes_compliance"],
            "compliance_verdict": r["compliance_verdict"],
            "in_optimization": t not in dropped_by_ticker,
            "dropped_reason": dropped_by_ticker.get(t),
        })

    return {
        "as_of_date": as_of_date,
        "top_k": top_k,
        "max_weight": max_weight,
        "n_candidates": len(candidate_tickers),
        "n_ranked": len(ranked_tickers),
        "weights": weights,
        "portfolio": {
            "expected_return": opt["expected_return"],
            "volatility": opt["volatility"],
            "sharpe_ratio": opt["sharpe_ratio"],
            "risk_free_rate": opt["risk_free_rate"],
            "risk_free_rate_note": opt["risk_free_rate_note"],
            "objective": opt["objective"],
            "lookback": opt["lookback"],
            "n_assets_optimized": opt["n_assets_optimized"],
        },
        "holdings": holdings,
        "dropped_from_optimization": opt["dropped"],
    }


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


if __name__ == "__main__":
    # Live demo (hits yfinance for prices; consensus screen stays LLM-free):
    #   cd backend && python -m app.services.portfolio_optimizer
    CANDIDATES = ["AAPL", "MSFT", "NVDA", "JPM", "GS", "KO", "XOM", "WMT", "PFE"]

    result = build_portfolio(CANDIDATES, as_of_date=None, top_k=25, max_weight=0.20)

    print("\n" + "=" * 90)
    print("Phase 5e — build_portfolio")
    print(f"  candidates      : {CANDIDATES}")
    print(f"  ranked (5b)     : {[h['ticker'] for h in result['holdings']]}")
    print("=" * 90)

    print(f"\n  {'TICKER':<7} {'WEIGHT':>9}  {'CONSENSUS':>9}  {'RANK':>4}  {'COMPLIANT':>9}  IN-OPT")
    print("  " + "-" * 62)
    for h in result["holdings"]:
        print(f"  {h['ticker']:<7} {h['weight'] * 100:>8.2f}%  {h['consensus_score']:>+9.3f}  "
              f"{h['consensus_rank']:>4}  {str(h['passes_compliance']):>9}  "
              f"{'yes' if h['in_optimization'] else 'NO (' + (h['dropped_reason'] or '') + ')'}")

    p = result["portfolio"]
    print(f"\n  weights sum        : {sum(result['weights'].values()):.4f}")
    print(f"  expected return    : {_fmt_pct(p['expected_return'])}  (annualised)")
    print(f"  volatility         : {_fmt_pct(p['volatility'])}  (annualised)")
    print(f"  Sharpe ratio       : {p['sharpe_ratio']:.3f}  (rf={p['risk_free_rate']:.2%})")
    if result["dropped_from_optimization"]:
        print("\n  dropped from optimization:")
        for d in result["dropped_from_optimization"]:
            print(f"    {d['ticker']:<7} {d['reason']}")
