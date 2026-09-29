"""
Step 6 — convert the MiroFish report into the persona JSON with the .env LLM.

MiroFish's report agent writes prose even when the simulation prompt asks for
a JSON summary (healthcare trial report_af259d80fb30: recommendations buried
in paragraphs, 3-4 tickers per persona instead of 5-10, an unexecuted
tool-call section, repeated text). So an LLM reads the report and fills
prompts/report_json_format.json, and a deterministic validation pass keeps
the small model honest:

  - persona names normalised onto the 8 fixed personas of the format
  - ticker symbols cleaned ("$LLY", "lly" -> "LLY"), de-duplicated
  - tickers outside ticker_universe dropped
  - tickers never mentioned anywhere in the report dropped (hallucinated)
  - missing personas -> [] + warning; extra ones (e.g. "Konsensus") ignored
  - fewer tickers than asked is kept as-is (never padded) + warning

    06_report.json            exactly the format: {"personas": [{persona, tickers}]}
    06_report_json_meta.json  report id, raw LLM output, per-persona validation, warnings

Runs as a background job (the LLM can take minutes); resumable by re-running.
"""

import json
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..services.report_agent import ReportManager
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from . import run_store
from .step1_universe import ARTIFACT as UNIVERSE_ARTIFACT

logger = get_logger('mirofish.news_pipeline.step6')

STEP = "report_json"
ARTIFACT = "06_report.json"
META_ARTIFACT = "06_report_json_meta.json"
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), 'prompts')
FORMAT_PATH = os.path.join(PROMPTS_DIR, 'report_json_format.json')
SYSTEM_PATH = os.path.join(PROMPTS_DIR, 'report_to_json_system.txt')

MAX_REPORT_CHARS = 60000   # ~15k tokens; reports seen so far are 8-16k chars
MIN_EXPECTED, MAX_EXPECTED = 5, 10

_jobs: Dict[str, threading.Thread] = {}
_jobs_lock = threading.Lock()


class JobConflictError(RuntimeError):
    pass


def personas() -> List[str]:
    with open(FORMAT_PATH, 'r', encoding='utf-8') as f:
        return [p["persona"] for p in json.load(f)["personas"]]


def _persona_key(name: str) -> str:
    """'1. value investor' / 'Value-Investor' -> 'valueinvestor'."""
    name = re.sub(r"^\s*\d+[.)]\s*", "", str(name or ""))
    return re.sub(r"[^a-z]", "", name.lower())


def _clean_ticker(value: Any) -> Optional[str]:
    t = re.sub(r"[^A-Z.\-]", "", str(value or "").upper().replace("$", ""))
    return t.replace(".", "-") or None


def _mentioned(ticker: str, text: str) -> bool:
    variants = {ticker, ticker.replace("-", ".")}
    return any(re.search(rf"(?<![A-Za-z]){re.escape(v)}(?![A-Za-z])", text) for v in variants)


def validate(raw: Any, universe: List[str], report_text: str) -> Dict[str, Any]:
    """Normalise the LLM output onto the fixed format. Pure; returns {result, validation, warnings}."""
    names = personas()
    by_key = {_persona_key(n): n for n in names}
    universe_set = set(universe)
    entries = raw.get("personas") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError("LLM output has no 'personas' list")

    found: Dict[str, List[Any]] = {}
    ignored = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = by_key.get(_persona_key(entry.get("persona")))
        if name is None:
            ignored.append(str(entry.get("persona")))
            continue
        tickers = entry.get("tickers")
        found.setdefault(name, []).extend(tickers if isinstance(tickers, list) else [])

    result, validation, warnings = [], {}, []
    for name in names:
        kept, out_of_universe, ungrounded = [], [], []
        for value in found.get(name, []):
            t = _clean_ticker(value)
            if not t or t in ("XXX", "YYY") or t in kept:
                continue
            if t not in universe_set:
                out_of_universe.append(t)
            elif not _mentioned(t, report_text):
                ungrounded.append(t)
            else:
                kept.append(t)
        result.append({"persona": name, "tickers": kept})
        validation[name] = {"kept": kept, "dropped_out_of_universe": out_of_universe,
                            "dropped_not_in_report": ungrounded, "missing": name not in found}
        if name not in found:
            warnings.append(f"{name}: missing from the LLM output — left empty")
        elif not kept:
            warnings.append(f"{name}: no valid tickers")
        elif len(kept) < MIN_EXPECTED:
            warnings.append(f"{name}: {len(kept)} ticker(s) — the report gave fewer than {MIN_EXPECTED}")
        elif len(kept) > MAX_EXPECTED:
            warnings.append(f"{name}: {len(kept)} tickers — more than {MAX_EXPECTED}")
        if out_of_universe:
            warnings.append(f"{name}: dropped {', '.join(out_of_universe)} (not in ticker_universe)")
        if ungrounded:
            warnings.append(f"{name}: dropped {', '.join(ungrounded)} (never mentioned in the report)")
    if ignored:
        warnings.append(f"Ignored extra persona(s) from the LLM: {', '.join(ignored)}")
    return {"result": {"personas": result}, "validation": validation, "warnings": warnings}


def build_messages(universe: List[str], report_text: str) -> List[Dict[str, str]]:
    with open(FORMAT_PATH, 'r', encoding='utf-8') as f:
        fmt = f.read().strip()
    with open(SYSTEM_PATH, 'r', encoding='utf-8') as f:
        system = f.read().replace("{format}", fmt).replace("{universe}", ", ".join(universe))
    if len(report_text) > MAX_REPORT_CHARS:
        report_text = report_text[:MAX_REPORT_CHARS] + "\n[... report truncated ...]"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Stock universe: {', '.join(universe)}\n\nMiroFish report:\n\n{report_text}"},
    ]


def _report_text(run_id: str) -> Dict[str, Any]:
    manifest = run_store.load_run(run_id)
    if manifest["steps"]["simulation"]["status"] != run_store.STEP_COMPLETED:
        raise ValueError("The MiroFish report is not completed yet (step 03)")
    report_id = manifest["links"].get("report_id")
    report = ReportManager.get_report(report_id) if report_id else None
    if report is None:
        raise ValueError(f"MiroFish report {report_id} not found")
    text = report.markdown_content or ""
    if not text.strip():
        path = os.path.join(ReportManager.REPORTS_DIR, report_id, 'full_report.md')
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
    if not text.strip():
        raise ValueError(f"MiroFish report {report_id} is empty")
    return {"report_id": report_id, "text": text}


def convert(run_id: str, client: Optional[LLMClient] = None) -> Dict[str, Any]:
    """Synchronous conversion (the job wraps this)."""
    universe = run_store.read_artifact(run_id, UNIVERSE_ARTIFACT)["ticker_universe"]
    report = _report_text(run_id)
    client = client or LLMClient()
    raw = client.chat_json(build_messages(universe, report["text"]), temperature=0.0,
                           max_tokens=2048, max_attempts=2)
    checked = validate(raw, universe, report["text"])
    run_store.write_artifact(run_id, ARTIFACT, checked["result"])
    run_store.write_artifact(run_id, META_ARTIFACT, {
        "report_id": report["report_id"],
        "model": getattr(client, "model", None),
        "generated_at": datetime.now().isoformat(timespec='seconds'),
        "report_chars": len(report["text"]),
        "raw_llm_output": raw,
        "validation": checked["validation"],
        "warnings": checked["warnings"],
    })
    total = sum(len(p["tickers"]) for p in checked["result"]["personas"])
    run_store.append_log(run_id, "report_json_built",
                         f"Report {report['report_id']} -> JSON: {total} picks over "
                         f"{sum(1 for p in checked['result']['personas'] if p['tickers'])}/8 personas, "
                         f"{len(checked['warnings'])} warning(s)")
    return run_store.update_step(run_id, STEP, run_store.STEP_COMPLETED, artifact=ARTIFACT, summary={
        "report_id": report["report_id"], "picks": total, "warnings": len(checked["warnings"]),
    })


def start(run_id: str) -> Dict[str, Any]:
    _report_text(run_id)  # fail fast (400) before starting a thread
    with _jobs_lock:
        if run_id in _jobs:
            return status(run_id)
        if _jobs:
            raise JobConflictError(f"Another run is converting its report ({next(iter(_jobs))})")
        thread = threading.Thread(target=_worker, args=(run_id,), daemon=True)
        _jobs[run_id] = thread
    run_store.update_step(run_id, STEP, run_store.STEP_RUNNING)
    thread.start()
    return status(run_id)


def _worker(run_id: str) -> None:
    try:
        convert(run_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("report -> JSON failed")
        run_store.update_step(run_id, STEP, run_store.STEP_FAILED, error=str(e)[:500])
    finally:
        with _jobs_lock:
            _jobs.pop(run_id, None)


def status(run_id: str) -> Dict[str, Any]:
    manifest = run_store.load_run(run_id)
    state = manifest["steps"][STEP]["status"]
    with _jobs_lock:
        live = run_id in _jobs
    if state == run_store.STEP_RUNNING and not live:
        state = "interrupted"  # backend restarted mid-conversion; start again
    return {
        "status": state,
        "error": manifest["steps"][STEP].get("error"),
        "result": run_store.read_artifact(run_id, ARTIFACT),
        "meta": run_store.read_artifact(run_id, META_ARTIFACT),
        "personas": personas(),
    }
