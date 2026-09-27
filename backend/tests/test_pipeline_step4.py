"""News pipeline step 4 — simulation prompt from ticker_universe (offline)."""

import pytest

from app import create_app
from app.models.project import ProjectManager, ProjectStatus
from app.news_pipeline import run_store, step4_prompt
from tests.test_pipeline_step3 import run_id  # noqa: F401  (fixture: run with news completed)

HC = ["LLY", "JNJ", "MRK", "PFE", "ABBV", "BMY", "AMGN", "GILD", "VRTX", "REGN", "ABT", "MDT",
      "SYK", "BSX", "ISRG", "BDX", "UNH", "CVS", "CI", "ELV", "HUM", "TMO", "DHR", "IDXX"]

# The user's original prompt (healthcare Q1-2026 trial), [ticker_universe] filled in.
USER_PROMPT = """Stock universe (MANDATORY — only select from these 24 tickers, do not go outside this list):
 LLY, JNJ, MRK, PFE, ABBV, BMY, AMGN, GILD, VRTX, REGN, ABT, MDT, SYK, BSX, ISRG, BDX, UNH, CVS, CI, ELV, HUM, TMO, DHR, IDXX
Build 8 FIXED investor persona agents (do not generate any personas outside these 8),
 with the following definitions:
Value Investor — focuses on cheap valuations relative to fundamentals (P/E, P/B, margin of safety), skeptical of hype.
Growth Investor — chases high revenue/earnings growth, tolerant of expensive valuations if the narrative is strong.
Momentum Trader — reacts quickly to short-term price & volume trends.
Contrarian/Skeptic — looks for signals against the crowd, suspicious of consensus & news euphoria.
Macro Strategist — evaluates through the lens of interest rates, inflation, economic cycles, policy (including health policy/FDA/insurance).
Technical Analyst — relies on chart patterns and technical indicators, disregards fundamentals.
ESG/Sustainability Investor — considers governance & social impact in addition to financial figures.
Risk Manager/Quant — based on quantitative models, volatility, portfolio correlations, systemic risk.
Requested prediction:
 For the 4–6 week horizon following the end of the news period (March 31, 2026),
 simulate how each persona reacts to the attached Q1 2026 news,
 and determine 5–10 stocks (from the all tickers above) that each persona most recommends
 accumulating, based on the results of the simulated debate/social interaction
 (not generic opinions outside the context of this news).
Once the simulation is complete, synthesize the results into TWO forms:
 (a) the native MiroFish report/verdict as usual (with a confidence score per signal)
 (b) an additional summary in exactly the following JSON format:
{
  "personas": [
    { "persona": "Value Investor", "tickers": ["XXX", "YYY"] },
    { "persona": "Growth Investor", "tickers": ["XXX", "YYY"] },
    { "persona": "Momentum Trader", "tickers": ["XXX", "YYY"] },
    { "persona": "Contrarian/Skeptic", "tickers": ["XXX", "YYY"] },
    { "persona": "Macro Strategist", "tickers": ["XXX", "YYY"] },
    { "persona": "Technical Analyst", "tickers": ["XXX", "YYY"] },
    { "persona": "ESG/Sustainability Investor", "tickers": ["XXX", "YYY"] },
    { "persona": "Risk Manager/Quant", "tickers": ["XXX", "YYY"] }
  ]
}
"""


def test_healthcare_reproduces_user_prompt_exactly():
    got = step4_prompt.render_prompt({"ticker_universe": HC, "as_of_date": "2026-03-31", "sectors": ["Health Care"]})
    assert got == USER_PROMPT


def test_other_universe_fills_run_specific_parts():
    got = step4_prompt.render_prompt({"ticker_universe": ["XOM", "CVX", "COP"], "as_of_date": "2026-06-29",
                                      "sectors": ["Energy", "Utilities"]})
    assert "only select from these 3 tickers" in got
    assert "\n XOM, CVX, COP\n" in got
    assert "(June 29, 2026)" in got
    assert "attached March 31 – June 29, 2026 news" in got
    assert "(including energy policy/OPEC/oil & gas prices; energy & utility regulation/rate cases/power demand)" in got
    assert "determine 3 stocks" in got
    assert "Q1 2026" not in got and "health policy" not in got


def test_pick_range_and_labels():
    assert step4_prompt.pick_range(24) == "5–10"
    assert step4_prompt.pick_range(7) == "5–7"
    assert step4_prompt.pick_range(2) == "2"
    assert step4_prompt.news_label("2026-06-30") == "Q2 2026"
    assert step4_prompt.news_label("2026-01-15") == "October 17, 2025 – January 15, 2026"


def test_seed_endpoint_also_sets_prompt_on_project(run_id):  # noqa: F811
    r = create_app().test_client().post(f"/api/pipeline/runs/{run_id}/seed")
    assert r.status_code == 200, r.json
    run = r.json["data"]
    assert run["steps"]["prompt"]["status"] == "completed"
    assert run["current_step"] == 5  # next: simulation
    project = ProjectManager.get_project(run["links"]["project_id"])
    prompt = run_store.read_artifact(run_id, step4_prompt.ARTIFACT)
    assert project.simulation_requirement == prompt
    assert "only select from these 1 tickers" in prompt and "\n XOM\n" in prompt


def test_prompt_frozen_once_project_moved_on(run_id):  # noqa: F811
    create_app().test_client().post(f"/api/pipeline/runs/{run_id}/seed")
    pid = run_store.load_run(run_id)["links"]["project_id"]
    project = ProjectManager.get_project(pid)
    project.status = ProjectStatus.GRAPH_COMPLETED
    project.simulation_requirement = "something else"
    ProjectManager.save_project(project)
    with pytest.raises(ValueError, match="can no longer change"):
        step4_prompt.build_prompt(run_id)
