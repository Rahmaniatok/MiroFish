"""
Portfolio performance math for step 8 (pure: pandas in, numbers out).

Conventions (stated once, used everywhere):
  - buy-and-hold: weights are set at the base date and then drift with prices
    (no rebalancing) — what "buy the picks and hold them" actually earns
  - daily simple returns, 252 trading days per year
  - risk-free rate: annual, converted to a daily rate (1+rf)^(1/252)-1
  - Sharpe  = mean(excess daily return)·252 / (std(daily return)·√252)
  - Sortino = mean(excess)·252 / (downside deviation of excess returns·√252)
  - annualized return = CAGR over calendar days; for windows under a year it
    extrapolates, so total return is reported next to it
  - Calmar = annualized return / |max drawdown|
  - diversification ratio = Σ wᵢσᵢ / √(wᵀΣw)   (base-date weights; 1 = no
    diversification benefit, higher = more)
  - effective assets = 1 / Σ wᵢ²   (weight concentration; N for equal weights)
"""

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _num(x: float) -> Optional[float]:
    return None if x is None or not math.isfinite(x) else float(round(x, 6))


def portfolio_value(prices: pd.DataFrame, weights: Dict[str, float]) -> pd.Series:
    """Buy-and-hold value, 1.0 at the first row."""
    cols = list(weights)
    rel = prices[cols] / prices[cols].iloc[0]
    w = pd.Series(weights, dtype=float)
    return (rel * w).sum(axis=1) / w.sum()


def drawdown(value: pd.Series) -> pd.Series:
    return value / value.cummax() - 1.0


def value_at_offset(value: pd.Series, days: int) -> Optional[Dict[str, float]]:
    """Total return at the first trading day >= base + `days` calendar days."""
    target = value.index[0] + pd.Timedelta(days=days)
    after = value[value.index >= target]
    if after.empty:
        return None
    return {"date": after.index[0].strftime("%Y-%m-%d"), "return": _num(after.iloc[0] / value.iloc[0] - 1)}


def metrics(value: pd.Series, prices: pd.DataFrame, weights: Dict[str, float], rf_annual: float) -> Dict[str, Optional[float]]:
    r = value.pct_change().dropna()
    total = value.iloc[-1] / value.iloc[0] - 1
    years = max((value.index[-1] - value.index[0]).days, 1) / 365.25
    ann = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    vol = r.std(ddof=1) * math.sqrt(TRADING_DAYS) if len(r) > 1 else float("nan")

    rf_d = (1 + rf_annual) ** (1 / TRADING_DAYS) - 1
    excess = r - rf_d
    sharpe = excess.mean() * TRADING_DAYS / vol if vol and vol > 0 else float("nan")
    downside = math.sqrt((np.minimum(excess, 0) ** 2).mean()) * math.sqrt(TRADING_DAYS) if len(r) else float("nan")
    sortino = excess.mean() * TRADING_DAYS / downside if downside and downside > 0 else float("nan")
    mdd = drawdown(value).min()
    calmar = ann / abs(mdd) if mdd < 0 else float("nan")

    w = pd.Series(weights, dtype=float)
    w = w / w.sum()
    asset_r = prices[list(w.index)].pct_change().dropna()
    cov = asset_r.cov() * TRADING_DAYS
    port_sd = math.sqrt(float(w.values @ cov.values @ w.values)) if len(asset_r) > 1 else float("nan")
    weighted_sd = float((w * np.sqrt(np.diag(cov.values))).sum()) if len(asset_r) > 1 else float("nan")
    div_ratio = weighted_sd / port_sd if port_sd and port_sd > 0 else float("nan")

    return {
        "total_return": _num(total),
        "annualized_return": _num(ann),
        "volatility": _num(vol),
        "sharpe": _num(sharpe),
        "sortino": _num(sortino),
        "max_drawdown": _num(mdd),
        "calmar": _num(calmar),
        "diversification_ratio": _num(div_ratio),
        "effective_assets": _num(1.0 / float((w ** 2).sum())),
    }


def holdings(prices: pd.DataFrame, weights: Dict[str, float]) -> List[Dict[str, Optional[float]]]:
    """Per ticker: base weight, own total return, contribution to the portfolio return."""
    total_w = sum(weights.values())
    out = []
    for t, w in weights.items():
        ret = prices[t].iloc[-1] / prices[t].iloc[0] - 1
        out.append({"ticker": t, "weight": _num(w / total_w), "return": _num(ret),
                    "contribution": _num(w / total_w * ret)})
    return sorted(out, key=lambda h: -(h["contribution"] or 0))


def correlation(prices: pd.DataFrame, tickers: List[str]) -> Dict[str, object]:
    r = prices[tickers].pct_change().dropna()
    corr = r.corr()
    cov = r.cov() * TRADING_DAYS
    return {
        "tickers": tickers,
        "corr": [[_num(corr.loc[a, b]) for b in tickers] for a in tickers],
        "cov": [[_num(cov.loc[a, b]) for b in tickers] for a in tickers],
        "vol": [_num(math.sqrt(cov.loc[t, t])) if cov.loc[t, t] >= 0 else None for t in tickers],
    }
