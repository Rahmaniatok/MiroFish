"""
Step 2 — for every ticker in ticker_universe fetch 90 days of Finnhub news
(up to as_of) and compact it into txt_berita, the reality seed for step 3.

Resumable per ticker: each ticker's raw news lands in
02_news/raw/<TICKER>.json the moment it is fetched, so pause / crash /
backend restart only loses the ticker in flight. Starting again skips every
ticker that already has a raw file (and get_company_news is cached too).

    02_news/raw/<TICKER>.json   all articles for the window (untrimmed)
    02_news/compaction.json     cap + per-ticker stats of the last txt build
    02_news/txt_berita.txt      the compact seed text

Only one fetch job runs at a time in the process: the Finnhub free tier
allows 60 calls/minute per key, shared by every run.
"""

import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..data_layer.news_data import NewsFetchError, NewsFetchStopped, get_company_news, news_window
from ..utils.logger import get_logger
from . import news_compactor as nc
from . import run_store
from .step1_universe import ARTIFACT as UNIVERSE_ARTIFACT

logger = get_logger('mirofish.news_pipeline.step2')

STEP = "news"
RAW_DIR = "02_news/raw"
TXT_ARTIFACT = "02_news/txt_berita.txt"
COMPACTION_ARTIFACT = "02_news/compaction.json"


class JobConflictError(RuntimeError):
    pass


class _Job:
    def __init__(self, run_id: str, total: int):
        self.run_id = run_id
        self.stop = threading.Event()
        self.live: Dict[str, Any] = {
            "current": None, "current_calls": 0, "current_articles": 0,
            "last_window": None, "total": total, "calls": 0,
            "started_at": datetime.now().isoformat(timespec='seconds'),
        }


_jobs: Dict[str, _Job] = {}
_jobs_lock = threading.Lock()
# (run_id, ticker, cap, raw mtime) -> decided rows; selection is pure, raw files are immutable
_selection_memo: Dict[tuple, List[Dict[str, Any]]] = {}


# ------------------------------------------------------------- helpers ----

def _raw_name(ticker: str) -> str:
    return f"{RAW_DIR}/{ticker}.json"


def _raw_path(run_id: str, ticker: str) -> str:
    return os.path.join(run_store.PIPELINE_RUNS_DIR, run_id, _raw_name(ticker))


def _universe(run_id: str) -> Dict[str, Any]:
    universe = run_store.read_artifact(run_id, UNIVERSE_ARTIFACT)
    if not universe:
        raise ValueError("Step 1 (ticker universe) is not completed for this run")
    return universe


def _fetched_tickers(run_id: str, tickers: List[str]) -> List[str]:
    return [t for t in tickers if os.path.exists(_raw_path(run_id, t))]


def current_cap(run_id: str, n_tickers: int) -> int:
    saved = run_store.read_artifact(run_id, COMPACTION_ARTIFACT)
    return saved["cap"] if saved else nc.auto_cap(n_tickers)


def _selection(run_id: str, ticker: str, company_name: str, cap: int) -> List[Dict[str, Any]]:
    path = _raw_path(run_id, ticker)
    key = (run_id, ticker, cap, os.path.getmtime(path))
    if key not in _selection_memo:
        if len(_selection_memo) > 2000:
            _selection_memo.clear()
        raw = run_store.read_artifact(run_id, _raw_name(ticker))
        _selection_memo[key] = nc.select_for_ticker(raw, company_name, cap)
    return _selection_memo[key]


# ----------------------------------------------------------------- job ----

def start(run_id: str) -> Dict[str, Any]:
    run_store.load_run(run_id)  # 404 early for unknown runs
    universe = _universe(run_id)
    tickers = universe["ticker_universe"]
    with _jobs_lock:
        if run_id in _jobs:
            return status(run_id)
        busy = [r for r in _jobs if r != run_id]
        if busy:
            raise JobConflictError(
                f"Another run is fetching news ({busy[0]}); Finnhub allows one job at a time — pause it first")
        job = _Job(run_id, len(tickers))
        _jobs[run_id] = job

    already = _fetched_tickers(run_id, tickers)
    run_store.update_step(run_id, STEP, run_store.STEP_RUNNING)
    if already:
        run_store.append_log(run_id, "news_resume",
                             f"Resuming: {len(already)}/{len(tickers)} tickers already fetched, skipping them")
    threading.Thread(target=_worker, args=(job, universe), daemon=True).start()
    return status(run_id)


def pause(run_id: str) -> Dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(run_id)
    if job:
        job.stop.set()
        run_store.append_log(run_id, "news_pause_requested", "Pause requested — stopping after the current call")
    return status(run_id)


def _worker(job: _Job, universe: Dict[str, Any]) -> None:
    run_id, as_of = job.run_id, universe["as_of_date"]
    errors: Dict[str, str] = {}
    try:
        for ticker in universe["ticker_universe"]:
            if os.path.exists(_raw_path(run_id, ticker)):
                continue
            job.live.update(current=ticker, current_calls=0, current_articles=0, last_window=None)

            def on_call(info, _job=job):
                _job.live["calls"] += 1
                _job.live["current_calls"] += 1
                _job.live["last_window"] = info

            try:
                raw = get_company_news(ticker, as_of, on_call=on_call, should_stop=job.stop.is_set)
            except NewsFetchError as e:
                errors[ticker] = str(e)
                run_store.append_log(run_id, "news_error", f"{ticker}: {e}")
                continue
            from_cache = raw.pop("from_cache", False)
            run_store.write_artifact(run_id, _raw_name(ticker), raw)
            job.live["current_articles"] = len(raw["articles"])
            note = " (cache)" if from_cache else f" ({raw['calls']} calls)"
            trunc = f", {len(raw['truncated_days'])} day(s) still capped" if raw["truncated_days"] else ""
            run_store.append_log(run_id, "news_fetched", f"{ticker}: {len(raw['articles'])} articles{note}{trunc}")

        if errors:
            run_store.update_step(run_id, STEP, run_store.STEP_FAILED,
                                  error=f"{len(errors)} ticker(s) failed: {', '.join(errors)} — press Resume to retry them",
                                  summary={"errors": errors})
            return
        summary = compact(run_id)
        run_store.update_step(run_id, STEP, run_store.STEP_COMPLETED, artifact=TXT_ARTIFACT, summary=summary)
    except NewsFetchStopped:
        run_store.update_step(run_id, STEP, run_store.STEP_PAUSED)
    except Exception as e:  # noqa: BLE001
        logger.exception("news step failed")
        run_store.update_step(run_id, STEP, run_store.STEP_FAILED, error=str(e))
    finally:
        with _jobs_lock:
            _jobs.pop(run_id, None)


# ---------------------------------------------------------- compaction ----

def compact(run_id: str, cap: Optional[int] = None) -> Dict[str, Any]:
    """(Re)build txt_berita from the raw files with the given per-ticker cap."""
    universe = _universe(run_id)
    tickers = universe["ticker_universe"]
    manifest = run_store.load_run(run_id)
    if manifest["steps"]["seed"]["status"] == run_store.STEP_COMPLETED:
        raise ValueError(f"txt_berita is already the reality seed of MiroFish project "
                         f"{manifest['links'].get('project_id')} — it can no longer be rebuilt")
    missing = [t for t in tickers if not os.path.exists(_raw_path(run_id, t))]
    if missing:
        raise ValueError(f"News not fetched yet for {len(missing)} ticker(s): {', '.join(missing[:8])}")
    cap = int(cap) if cap else current_cap(run_id, len(tickers))
    if not 1 <= cap <= 200:
        raise ValueError("cap must be between 1 and 200")

    names = {r["ticker"]: r["company_name"] for r in universe["universe"]}
    selections = {t: _selection(run_id, t, names[t], cap) for t in tickers}
    text = nc.render_txt(universe, selections, cap)
    run_store.write_artifact(run_id, TXT_ARTIFACT, text)

    per_ticker = {t: nc.ticker_stats(selections[t]) for t in tickers}
    totals = {
        "raw": sum(s["raw"] for s in per_ticker.values()),
        "relevant": sum(s["relevant"] for s in per_ticker.values()),
        "kept": sum(s["kept"] for s in per_ticker.values()),
        "lines": sum(1 for l in text.splitlines() if " | " in l),
        "bytes": len(text.encode("utf-8")),
        "tickers_without_news": [t for t, s in per_ticker.items() if s["kept"] == 0],
    }
    run_store.write_artifact(run_id, COMPACTION_ARTIFACT, {
        "cap": cap,
        "auto_cap": nc.auto_cap(len(tickers)),
        "generated_at": datetime.now().isoformat(timespec='seconds'),
        "totals": totals,
        "per_ticker": per_ticker,
    })
    run_store.append_log(run_id, "txt_generated",
                         f"txt_berita built: {totals['lines']} articles, {totals['bytes'] / 1024:.0f} KB "
                         f"(cap {cap}/ticker, {totals['raw']} raw articles)")
    summary = {"cap": cap, **totals}
    # a rebuild after completion must keep run.json's step summary in sync
    if run_store.load_run(run_id)["steps"][STEP]["status"] == run_store.STEP_COMPLETED:
        run_store.patch_step(run_id, STEP, summary=summary)
    return summary


# ---------------------------------------------------------------- reads ----

def status(run_id: str) -> Dict[str, Any]:
    manifest = run_store.load_run(run_id)
    universe = _universe(run_id)
    tickers = universe["ticker_universe"]
    step = manifest["steps"][STEP]
    with _jobs_lock:
        job = _jobs.get(run_id)
    state = step["status"]
    if state == run_store.STEP_RUNNING and job is None:
        state = "interrupted"  # backend restarted mid-fetch; Resume continues
    if job is not None and job.stop.is_set():
        state = "pausing"

    fetched = set(_fetched_tickers(run_id, tickers))
    cap = current_cap(run_id, len(tickers))
    errors = (step.get("summary") or {}).get("errors", {}) if step["status"] == run_store.STEP_FAILED else {}
    rows = []
    totals = {"raw": 0, "relevant": 0, "kept": 0}
    for info in universe["universe"]:
        t = info["ticker"]
        row = {"ticker": t, "company_name": info["company_name"], "gics_sector": info["gics_sector"],
               "market_cap_tier": info["market_cap_tier"]}
        if t in fetched:
            stats = nc.ticker_stats(_selection(run_id, t, info["company_name"], cap))
            row.update(state="fetched", **stats)
            for k in totals:
                totals[k] += stats[k]
        elif job and job.live["current"] == t:
            row.update(state="fetching", calls=job.live["current_calls"])
        elif t in errors:
            row.update(state="error", error=errors[t])
        else:
            row["state"] = "pending"
        rows.append(row)

    compaction = run_store.read_artifact(run_id, COMPACTION_ARTIFACT)
    return {
        "run_id": run_id,
        "status": state,
        "step": step,
        "as_of_date": universe["as_of_date"],
        "window_start": news_window(universe["as_of_date"])[0].isoformat(),
        "fetched_count": len(fetched),
        "ticker_count": len(tickers),
        "cap": cap,
        "auto_cap": nc.auto_cap(len(tickers)),
        "totals": totals,
        "live": dict(job.live) if job else None,
        "tickers": rows,
        "txt": ({k: compaction[k] for k in ("cap", "generated_at", "totals")} if compaction else None),
        "drop_labels": nc.DROP_LABELS,
        # step 3 locks the txt once it has been fed to MiroFish
        "seed": {"status": manifest["steps"]["seed"]["status"],
                 "project_id": manifest["links"].get("project_id")},
    }


def articles(run_id: str, ticker: str, view: str = "all") -> List[Dict[str, Any]]:
    universe = _universe(run_id)
    info = next((r for r in universe["universe"] if r["ticker"] == ticker.upper()), None)
    if info is None:
        raise LookupError(f"{ticker} is not in this run's ticker_universe")
    if not os.path.exists(_raw_path(run_id, info["ticker"])):
        return []
    rows = _selection(run_id, info["ticker"], info["company_name"],
                      current_cap(run_id, len(universe["ticker_universe"])))
    if view == "kept":
        rows = [r for r in rows if r["decision"] == nc.KEPT]
    fields = ("article_id", "headline", "clean_summary", "publisher", "url", "published_at",
              "score", "tags", "decision", "week")
    return [{k: r[k] for k in fields} for r in rows]


def txt(run_id: str) -> Optional[str]:
    return run_store.read_artifact(run_id, TXT_ARTIFACT)
