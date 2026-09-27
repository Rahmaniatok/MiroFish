"""
Step 1 — ticker_universe: filter the S&P 500 by GICS sector + market-cap tier
at a chosen as_of date, then lock the result into a new pipeline run.

Screening itself reuses app.data_layer.universe (ported from the
konsesus-lllm branch). It is a stateless preview (a background task with
live progress); nothing is written to a run until the user locks the
universe. Per-ticker market data is cached in market_cache.db, so re-screening
after an interruption is fast — screening needs no resume logic of its own.
"""

import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from ..data_layer.universe import (
    MARKET_CAP_TIERS,
    _run_screen,
    get_sp500_constituents,
)
from ..models.task import TaskManager, TaskStatus
from ..utils.logger import get_logger
from . import run_store

logger = get_logger('mirofish.news_pipeline.step1')

TASK_TYPE = "pipeline_universe_screen"
ARTIFACT = "01_universe.json"

GICS_SECTORS = [
    "Communication Services", "Consumer Discretionary", "Consumer Staples",
    "Energy", "Financials", "Health Care", "Industrials",
    "Information Technology", "Materials", "Real Estate", "Utilities",
]

TIER_RANGES = {
    "mega": "> $200B",
    "large": "$10B – $200B",
    "mid": "$2B – $10B",
    "small": "< $2B",
}

# Step 2 fetches 90 days of Finnhub news ending at as_of. The free tier only
# reaches ~360 days back from the day of the fetch (verified in Fase 8a, see
# data_layer news_data on konsesus-lllm). So:
#   as_of older than 360 days  -> step 2 gets nothing at all (rejected here)
#   as_of older than 270 days  -> the start of the 90-day window is cut off (warning)
NEWS_WINDOW_DAYS = 90
FINNHUB_LIMIT_DAYS = 360
FULL_NEWS_COVERAGE_DAYS = FINNHUB_LIMIT_DAYS - NEWS_WINDOW_DAYS

CAVEATS = [
    "Market cap / tier uses the CURRENT yfinance snapshot, not the value on as_of "
    "(yfinance has no historical fundamentals).",
    "S&P 500 membership is today's list, not the index membership on as_of "
    "(survivorship bias).",
]


def get_options() -> Dict[str, Any]:
    today = date.today()
    return {
        "sectors": GICS_SECTORS,
        "market_cap_tiers": [{"key": k, "range": TIER_RANGES[k]} for k in MARKET_CAP_TIERS],
        "as_of": {
            "max": today.isoformat(),
            "min": (today - timedelta(days=FINNHUB_LIMIT_DAYS)).isoformat(),
            "full_news_coverage_min": (today - timedelta(days=FULL_NEWS_COVERAGE_DAYS)).isoformat(),
            "news_window_days": NEWS_WINDOW_DAYS,
        },
        "caveats": CAVEATS,
    }


def validate_as_of(as_of_date: str) -> Dict[str, Any]:
    """Returns {"as_of_date", "warnings"}; raises ValueError when unusable."""
    try:
        as_of = datetime.strptime(as_of_date or "", "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("as_of_date must be YYYY-MM-DD")
    today = date.today()
    if as_of > today:
        raise ValueError(f"as_of_date {as_of} is in the future")
    days_back = (today - as_of).days
    if days_back > FINNHUB_LIMIT_DAYS:
        raise ValueError(
            f"as_of_date {as_of} is {days_back} days ago; Finnhub free tier only has "
            f"~{FINNHUB_LIMIT_DAYS} days of history, so step 2 would get no news"
        )
    warnings = []
    if days_back > FULL_NEWS_COVERAGE_DAYS:
        cut = (as_of - timedelta(days=NEWS_WINDOW_DAYS))
        first = today - timedelta(days=FINNHUB_LIMIT_DAYS)
        warnings.append(
            f"News before {first} is beyond Finnhub's range, so the 90-day window "
            f"({cut} .. {as_of}) will be partly empty"
        )
    if days_back < 30:
        warnings.append(
            "as_of is less than 30 days ago — step 8 will have little price history "
            "after as_of to measure performance"
        )
    return {"as_of_date": as_of.isoformat(), "warnings": warnings}


def validate_filters(sectors: Optional[List[str]], tiers: Optional[List[str]]) -> None:
    if not sectors:
        raise ValueError("Pick at least one sector")
    unknown = set(sectors) - set(GICS_SECTORS)
    if unknown:
        raise ValueError(f"Unknown sector(s): {sorted(unknown)}")
    if not tiers:
        raise ValueError("Pick at least one market-cap tier")
    unknown = set(tiers) - set(MARKET_CAP_TIERS)
    if unknown:
        raise ValueError(f"Unknown market-cap tier(s): {sorted(unknown)}")


def list_constituents(sectors: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Instant preview (no market data): S&P 500 members in the chosen sectors."""
    rows = get_sp500_constituents()
    if sectors:
        rows = [r for r in rows if r["gics_sector"] in set(sectors)]
    return sorted(rows, key=lambda r: (r["gics_sector"], r["ticker"]))


def start_screen(sectors: List[str], tiers: List[str], as_of_date: str) -> str:
    validate_filters(sectors, tiers)
    as_of_date = validate_as_of(as_of_date)["as_of_date"]
    params = {"sectors": sectors, "market_cap_tiers": tiers, "as_of_date": as_of_date}

    tm = TaskManager()
    task_id = tm.create_task(TASK_TYPE, metadata=params)

    def worker():
        passed: List[Dict[str, Any]] = []
        filtered: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []

        def on_progress(ev: Dict[str, Any]) -> None:
            if ev["outcome"] == "passed":
                passed.append(ev["entry"])
            elif ev["outcome"] == "filtered":
                filtered.append(ev["entry"])
            else:
                skipped.append({"ticker": ev["ticker"], "reason": ev["reason"]})
            pct = int(ev["processed"] * 100 / max(ev["in_scope"], 1))
            tm.update_task(
                task_id, progress=min(pct, 99),
                message=f"{ev['processed']}/{ev['in_scope']} · {ev['ticker']}",
                progress_detail={
                    "processed": ev["processed"], "in_scope": ev["in_scope"],
                    "current": ev["ticker"],
                    # copies: the poller serialises these while we keep appending
                    "passed": list(passed), "filtered": list(filtered), "skipped": list(skipped),
                },
            )

        try:
            tm.update_task(task_id, status=TaskStatus.PROCESSING, message="Loading S&P 500 list")
            result = _run_screen(sectors, tiers, as_of_date, progress_callback=on_progress)
            tm.complete_task(task_id, {
                **params,
                "universe": result["passed"],
                "filtered_out": filtered,
                "skipped": result["skipped"],
                "sp500_total": result["total"],
                "screened_at": datetime.now().isoformat(timespec='seconds'),
            })
        except Exception as e:  # noqa: BLE001
            logger.exception("universe screen failed")
            tm.fail_task(task_id, str(e))

    threading.Thread(target=worker, daemon=True).start()
    return task_id


def get_screen(task_id: str) -> Optional[Dict[str, Any]]:
    task = TaskManager().get_task(task_id)
    if task is None or task.task_type != TASK_TYPE:
        return None
    return task.to_dict()


def lock_universe(
    task_id: str,
    name: str,
    excluded_tickers: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Create a pipeline run from a finished screen. The universe rows come from
    the server-side screen result; the client only says which tickers to drop.

    market_cap_tiers (optional) re-filters the screen by tier without a new
    screen: every sector-matched ticker already has its tier computed (it's in
    either "universe" or "filtered_out"), so the UI can screen once with all
    tiers and toggle tiers instantly. Must be a subset of the screened tiers.
    """
    task = TaskManager().get_task(task_id)
    if task is None or task.task_type != TASK_TYPE:
        raise LookupError("Screening result not found (backend restarted?) — please screen again")
    if task.status != TaskStatus.COMPLETED:
        raise ValueError("Screening has not finished yet")

    screen = task.result
    tiers = list(market_cap_tiers or screen["market_cap_tiers"])
    if not tiers:
        raise ValueError("Pick at least one market-cap tier")
    if not set(tiers) <= set(screen["market_cap_tiers"]):
        raise ValueError(f"Tier(s) {sorted(set(tiers) - set(screen['market_cap_tiers']))} "
                         "were not screened — please screen again")
    candidates = sorted(screen["universe"] + screen["filtered_out"],
                        key=lambda r: r["market_cap"], reverse=True)
    in_tier = [r for r in candidates if r["market_cap_tier"] in tiers]
    filtered_out = [r for r in candidates if r["market_cap_tier"] not in tiers]

    excluded = {t.upper() for t in (excluded_tickers or [])}
    universe = [r for r in in_tier if r["ticker"] not in excluded]
    if not universe:
        raise ValueError("ticker_universe is empty — keep at least one ticker")

    warnings = validate_as_of(screen["as_of_date"])["warnings"]
    config = {
        "as_of_date": screen["as_of_date"],
        "sectors": screen["sectors"],
        "market_cap_tiers": [t for t in MARKET_CAP_TIERS if t in tiers],
    }
    manifest = run_store.create_run(name, config)
    run_id = manifest["run_id"]
    run_store.update_step(run_id, "universe", run_store.STEP_RUNNING)

    artifact = {
        **config,
        "ticker_universe": [r["ticker"] for r in universe],
        "universe": universe,
        "excluded_by_user": sorted(excluded & {r["ticker"] for r in in_tier}),
        "filtered_out": filtered_out,
        "skipped": screen["skipped"],
        "sp500_total": screen["sp500_total"],
        "screened_at": screen["screened_at"],
        "warnings": warnings,
        "caveats": CAVEATS,
    }
    run_store.write_artifact(run_id, ARTIFACT, artifact)

    by_sector: Dict[str, int] = {}
    by_tier: Dict[str, int] = {}
    for r in universe:
        by_sector[r["gics_sector"]] = by_sector.get(r["gics_sector"], 0) + 1
        by_tier[r["market_cap_tier"]] = by_tier.get(r["market_cap_tier"], 0) + 1
    summary = {
        "ticker_count": len(universe),
        "total_market_cap": sum(r["market_cap"] for r in universe),
        "by_sector": by_sector,
        "by_tier": by_tier,
        "skipped_count": len(screen["skipped"]),
        "excluded_by_user": artifact["excluded_by_user"],
    }
    run_store.append_log(run_id, "universe_locked",
                         f"ticker_universe locked: {len(universe)} tickers @ {config['as_of_date']}",
                         {"tickers": artifact["ticker_universe"], "warnings": warnings})
    return run_store.update_step(run_id, "universe", run_store.STEP_COMPLETED,
                                 artifact=ARTIFACT, summary=summary)
