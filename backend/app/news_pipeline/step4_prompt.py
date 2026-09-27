"""
Step 4 — build the MiroFish "Simulation prompt" (simulation_requirement) from
ticker_universe and store it on the run's MiroFish project.

The text lives in prompts/simulation_prompt.txt (the user's format, written
for the healthcare Q1-2026 trial). Only the run-specific parts are filled in:

    {n_tickers}     number of tickers                 (was "24")
    {ticker_list}   the tickers, comma separated      (was "[ticker_universe]")
    {as_of_long}    end of the news window, as_of     (was "March 31, 2026")
    {news_label}    "Q1 2026" when as_of is a quarter end, else the date range
    {macro_policy}  sector policy lens                (was "health policy/FDA/insurance")
    {pick_range}    "5–10", narrowed for universes smaller than 10

Deterministic (no LLM): the same run always yields the same prompt.
"""

import os
from datetime import date
from typing import Any, Dict

from ..data_layer.news_data import news_window
from ..models.project import ProjectManager, ProjectStatus
from ..utils.logger import get_logger
from . import run_store
from .step1_universe import ARTIFACT as UNIVERSE_ARTIFACT

logger = get_logger('mirofish.news_pipeline.step4')

STEP = "prompt"
ARTIFACT = "04_simulation_prompt.txt"
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'prompts', 'simulation_prompt.txt')

# Macro Strategist's policy lens per GICS sector; Health Care keeps the
# user's original wording verbatim.
SECTOR_POLICY = {
    "Health Care": "health policy/FDA/insurance",
    "Energy": "energy policy/OPEC/oil & gas prices",
    "Information Technology": "tech regulation/export controls/AI policy",
    "Communication Services": "tech & media regulation/antitrust/advertising cycle",
    "Financials": "monetary policy/bank regulation/credit spreads",
    "Consumer Discretionary": "consumer spending/tariffs/labor market",
    "Consumer Staples": "consumer spending/input costs/tariffs",
    "Industrials": "fiscal & infrastructure policy/tariffs/supply chains",
    "Materials": "commodity prices/tariffs/China demand",
    "Real Estate": "interest rates/housing & property policy",
    "Utilities": "energy & utility regulation/rate cases/power demand",
}


def _long_date(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def news_label(as_of_date: str) -> str:
    start, end = news_window(as_of_date)
    quarter_ends = {(3, 31): 1, (6, 30): 2, (9, 30): 3, (12, 31): 4}
    q = quarter_ends.get((end.month, end.day))
    if q:
        return f"Q{q} {end.year}"
    if start.year == end.year:
        return f"{start.strftime('%B')} {start.day} – {_long_date(end)}"
    return f"{_long_date(start)} – {_long_date(end)}"


def pick_range(n_tickers: int) -> str:
    low, high = min(5, n_tickers), min(10, n_tickers)
    return str(high) if low == high else f"{low}–{high}"


def render_prompt(universe: Dict[str, Any]) -> str:
    tickers = universe["ticker_universe"]
    as_of = date.fromisoformat(universe["as_of_date"])
    policies = [SECTOR_POLICY.get(s, "sector policy") for s in universe["sectors"]]
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        template = f.read()
    return template.format(
        n_tickers=len(tickers),
        ticker_list=", ".join(tickers),
        as_of_long=_long_date(as_of),
        news_label=news_label(universe["as_of_date"]),
        macro_policy="; ".join(dict.fromkeys(policies)),
        pick_range=pick_range(len(tickers)),
    ).rstrip() + "\n"


def build_prompt(run_id: str) -> Dict[str, Any]:
    """Render the prompt and set it as simulation_requirement of the seed project."""
    manifest = run_store.load_run(run_id)
    if manifest["steps"]["seed"]["status"] != run_store.STEP_COMPLETED:
        raise ValueError("Step 3 (reality seed) is not completed yet")
    project_id = manifest["links"].get("project_id")
    project = ProjectManager.get_project(project_id) if project_id else None
    if project is None:
        raise ValueError(f"MiroFish project {project_id} not found — feed the reality seed again")

    prompt = render_prompt(run_store.read_artifact(run_id, UNIVERSE_ARTIFACT))
    if project.status not in (ProjectStatus.CREATED, ProjectStatus.FAILED):
        # ontology/graph already built from the stored prompt: keep them consistent
        if project.simulation_requirement != prompt:
            raise ValueError(f"MiroFish project {project_id} is already {project.status.value}; "
                             "its simulation prompt can no longer change")
    else:
        project.simulation_requirement = prompt
        ProjectManager.save_project(project)

    run_store.write_artifact(run_id, ARTIFACT, prompt)
    run_store.append_log(run_id, "prompt_built",
                         f"Simulation prompt set on {project_id} ({len(prompt)} chars)")
    return run_store.update_step(run_id, STEP, run_store.STEP_COMPLETED, artifact=ARTIFACT, summary={
        "project_id": project_id,
        "chars": len(prompt),
        "news_label": news_label(manifest["config"]["as_of_date"]),
    })


def get_prompt(run_id: str) -> Dict[str, Any]:
    run_store.load_run(run_id)
    return {"text": run_store.read_artifact(run_id, ARTIFACT)}
