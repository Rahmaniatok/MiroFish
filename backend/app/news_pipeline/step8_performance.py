"""
Step 8 — performance report of the picks, from as_of until today.

Portfolios (all buy-and-hold, see performance_metrics.py):
  - Consensus      step-7 consensus tickers, weight ∝ persona votes
  - 8 personas     each persona's step-6 tickers, equal weight (1 vote each)
  - Benchmarks     SPY (S&P 500), the SPDR sector ETF of every universe
                   sector, and "Universe EW" (all ticker_universe, equal
                   weight) — did the simulation pick better than the pool?

Entry is the close of the first trading day AFTER as_of: the news window ends
on as_of and may include articles published after that day's close, so
trading on the as_of close would use information not yet available.

Risk-free: mean of ^IRX (13-week T-bill yield) over the period; 0 if missing.
Prices come from yfinance (auto-adjusted = total return) and are refetched
each time the report is (re)built, so "until today" moves forward.

    08_performance.json
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List

import pandas as pd

from ..data_layer.price_history import fetch_close_history
from ..utils.logger import get_logger
from . import performance_metrics as pm
from . import run_store
from .step1_universe import ARTIFACT as UNIVERSE_ARTIFACT
from .step6_report_json import ARTIFACT as REPORT_JSON_ARTIFACT
from .step7_consensus import ARTIFACT as CONSENSUS_ARTIFACT

logger = get_logger('mirofish.news_pipeline.step8')

STEP = "performance"
ARTIFACT = "08_performance.json"
MARKET_BENCHMARK = "SPY"
RISK_FREE_TICKER = "^IRX"
HORIZONS = {"4w": 28, "6w": 42}   # the simulation prompt's 4–6 week horizon

# Sharia-screened market benchmarks, added when the run used the Sharia filter
SHARIA_BENCHMARKS = {
    "SPUS": "S&P 500 Sharia (SPUS)",
    "HLAL": "FTSE USA Shariah (HLAL)",
}

SECTOR_ETF = {
    "Communication Services": "XLC", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Energy": "XLE", "Financials": "XLF", "Health Care": "XLV", "Industrials": "XLI",
    "Information Technology": "XLK", "Materials": "XLB", "Real Estate": "XLRE", "Utilities": "XLU",
}


def _slug(name: str) -> str:
    return "persona:" + "".join(c for c in name.lower() if c.isalnum())


def build(run_id: str, fetch=fetch_close_history, today: date = None) -> Dict[str, Any]:
    manifest = run_store.load_run(run_id)
    if manifest["steps"]["consensus"]["status"] != run_store.STEP_COMPLETED:
        raise ValueError("Step 05 (consensus) is not completed yet")
    universe = run_store.read_artifact(run_id, UNIVERSE_ARTIFACT)
    report_json = run_store.read_artifact(run_id, REPORT_JSON_ARTIFACT)
    consensus = run_store.read_artifact(run_id, CONSENSUS_ARTIFACT)
    as_of = date.fromisoformat(universe["as_of_date"])
    today = today or date.today()

    votes = {r["ticker"]: r["votes"] for r in consensus["ranking"] if r["selected"]}
    sector_etfs = [SECTOR_ETF[s] for s in universe["sectors"] if s in SECTOR_ETF]
    sharia_etfs = list(SHARIA_BENCHMARKS) if universe.get("sharia") else []
    wanted = list(dict.fromkeys(universe["ticker_universe"] + [MARKET_BENCHMARK] + sharia_etfs + sector_etfs))

    run_store.update_step(run_id, STEP, run_store.STEP_RUNNING)
    try:
        history = fetch(wanted + [RISK_FREE_TICKER], as_of - timedelta(days=10), today)
        result = _compute(universe, report_json, consensus, votes, sector_etfs, history, as_of)
    except Exception as e:  # noqa: BLE001
        run_store.update_step(run_id, STEP, run_store.STEP_FAILED, error=str(e)[:500])
        raise

    result.update(generated_at=datetime.now().isoformat(timespec='seconds'),
                  consensus_votes=_consensus_votes(consensus))
    run_store.write_artifact(run_id, ARTIFACT, result)
    cons = next(p for p in result["portfolios"] if p["key"] == "consensus")
    spy = next((p for p in result["portfolios"] if p["key"] == MARKET_BENCHMARK), None)
    tr = cons["metrics"]["total_return"]
    run_store.append_log(run_id, "performance_built",
                         f"Performance {result['base_date']} → {result['end_date']}: consensus "
                         f"{tr:+.2%}" + (f" vs SPY {spy['metrics']['total_return']:+.2%}" if spy else ""))
    return run_store.update_step(run_id, STEP, run_store.STEP_COMPLETED, artifact=ARTIFACT, summary={
        "base_date": result["base_date"], "end_date": result["end_date"],
        "consensus_return": tr, "spy_return": spy["metrics"]["total_return"] if spy else None,
    })


def _compute(universe, report_json, consensus, votes, sector_etfs, history, as_of) -> Dict[str, Any]:
    warnings: List[str] = []
    rf_hist = history.pop(RISK_FREE_TICKER, None)
    if not history:
        raise ValueError("yfinance returned no prices")
    prices = pd.DataFrame({t: pd.Series(v) for t, v in history.items()})
    prices.index = pd.to_datetime(prices.index)
    prices = prices.sort_index()
    prices = prices[prices.index > pd.Timestamp(as_of)]      # entry: first close after as_of
    if len(prices) < 2:
        raise ValueError(f"Not enough trading days after as_of {as_of} yet to measure performance")
    prices = prices.ffill(limit=3)                            # isolated gaps only
    usable = {t for t in prices.columns if pd.notna(prices[t].iloc[0]) and prices[t].notna().all()}
    missing = sorted(set(universe["ticker_universe"]) - usable)
    if missing:
        warnings.append(f"No complete price history for {', '.join(missing)} — left out of every portfolio")
    prices = prices[sorted(usable)]

    rf = 0.0
    if rf_hist:
        s = pd.Series(rf_hist)
        s.index = pd.to_datetime(s.index)
        s = s[s.index > pd.Timestamp(as_of)]
        if not s.empty:
            rf = float(s.mean()) / 100
    else:
        warnings.append("No ^IRX data — risk-free rate taken as 0%")

    specs = [{"key": "consensus", "label": "Consensus", "kind": "consensus",
              "weights": {t: float(v) for t, v in votes.items()}}]
    for p in report_json["personas"]:
        specs.append({"key": _slug(p["persona"]), "label": p["persona"], "kind": "persona",
                      "weights": {t: 1.0 for t in p["tickers"]}})
    specs.append({"key": MARKET_BENCHMARK, "label": "S&P 500 (SPY)", "kind": "benchmark",
                  "weights": {MARKET_BENCHMARK: 1.0}})
    if universe.get("sharia"):
        for etf, label in SHARIA_BENCHMARKS.items():
            specs.append({"key": etf, "label": label, "kind": "benchmark", "weights": {etf: 1.0}})
    for etf, sector in zip(sector_etfs, [s for s in universe["sectors"] if s in SECTOR_ETF]):
        specs.append({"key": etf, "label": f"{sector} ({etf})", "kind": "benchmark", "weights": {etf: 1.0}})
    specs.append({"key": "universe_ew", "label": "Universe EW", "kind": "benchmark",
                  "weights": {t: 1.0 for t in universe["ticker_universe"]}})

    portfolios = []
    for spec in specs:
        weights = {t: w for t, w in spec["weights"].items() if t in usable}
        dropped = sorted(set(spec["weights"]) - set(weights))
        if dropped and spec["kind"] != "benchmark":
            warnings.append(f"{spec['label']}: {', '.join(dropped)} dropped (no prices)")
        if not weights:
            if spec["kind"] != "benchmark" or spec["key"] == "universe_ew":
                warnings.append(f"{spec['label']}: no priced tickers — skipped")
            continue
        value = pm.portfolio_value(prices, weights)
        dd = pm.drawdown(value)
        portfolios.append({
            "key": spec["key"], "label": spec["label"], "kind": spec["kind"],
            "tickers": list(weights),
            "weights": {t: round(w / sum(weights.values()), 6) for t, w in weights.items()},
            "metrics": pm.metrics(value, prices, weights, rf),
            "horizons": {h: pm.value_at_offset(value, d) for h, d in HORIZONS.items()},
            "holdings": pm.holdings(prices, weights),
            "series": [round(v - 1, 6) for v in value.values],      # cumulative return
            "drawdown": [round(v, 6) for v in dd.values],
        })

    picked = list(votes)
    others = sorted({t for p in report_json["personas"] for t in p["tickers"]} - set(picked))
    corr_tickers = [t for t in picked + others if t in usable]
    return {
        "as_of_date": as_of.isoformat(),
        "base_date": prices.index[0].strftime("%Y-%m-%d"),
        "end_date": prices.index[-1].strftime("%Y-%m-%d"),
        "trading_days": len(prices),
        "dates": [d.strftime("%Y-%m-%d") for d in prices.index],
        "risk_free_rate": round(rf, 6),
        "horizons": {h: (prices.index[0] + pd.Timedelta(days=d)).strftime("%Y-%m-%d") for h, d in HORIZONS.items()},
        "portfolios": portfolios,
        "correlation": pm.correlation(prices, corr_tickers) if len(corr_tickers) > 1 else None,
        "consensus_tickers": [t for t in picked if t in usable],
        "warnings": warnings,
        "method": {
            "entry": "close of the first trading day after as_of",
            "weighting": "consensus ∝ persona votes; personas equal weight; buy-and-hold (no rebalancing)",
            "risk_free": "mean ^IRX (13-week T-bill) over the period",
        },
    }


def _consensus_votes(consensus: Dict[str, Any]) -> Dict[str, int]:
    return {r["ticker"]: r["votes"] for r in consensus["ranking"] if r["selected"]}


def get(run_id: str) -> Dict[str, Any]:
    manifest = run_store.load_run(run_id)
    result = run_store.read_artifact(run_id, ARTIFACT)
    consensus = run_store.read_artifact(run_id, CONSENSUS_ARTIFACT)
    # stale = the consensus (tickers / votes) changed after this report was built
    stale = bool(result and consensus and result.get("consensus_votes") != _consensus_votes(consensus))
    return {"result": result, "stale": stale, "status": manifest["steps"][STEP]["status"]}
