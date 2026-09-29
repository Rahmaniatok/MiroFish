"""News pipeline step 7 — consensus by persona votes (offline)."""

import pytest

from app import create_app
from app.news_pipeline import run_store, step7_consensus as s7
from tests.test_pipeline_step3 import run_id  # noqa: F401

HC = "LLY JNJ MRK PFE ABBV BMY AMGN GILD VRTX REGN ABT MDT SYK BSX ISRG BDX UNH CVS CI ELV HUM TMO DHR IDXX".split()
# step-6 output of the real healthcare trial report (report_af259d80fb30)
HC_JSON = {"personas": [
    {"persona": "Value Investor", "tickers": ["LLY", "JNJ", "MRK", "PFE"]},
    {"persona": "Growth Investor", "tickers": ["ABBV", "BMY", "GILD"]},
    {"persona": "Momentum Trader", "tickers": ["ISRG", "BDX", "UNH"]},
    {"persona": "Contrarian/Skeptic", "tickers": ["MDT", "SYK", "BSX"]},
    {"persona": "Macro Strategist", "tickers": ["UNH", "CVS", "CI"]},
    {"persona": "Technical Analyst", "tickers": ["TMO", "DHR", "IDXX"]},
    {"persona": "ESG/Sustainability Investor", "tickers": ["ISRG", "BDX", "UNH"]},
    {"persona": "Risk Manager/Quant", "tickers": ["ABT", "MDT", "SYK"]},
]}


def test_votes_and_ranking_on_healthcare_run():
    r = s7.compute(HC_JSON, HC, 5)
    assert r["consensus"] == ["UNH", "BDX", "ISRG", "MDT", "SYK"]
    top = {x["ticker"]: x for x in r["ranking"]}
    assert top["UNH"]["votes"] == 3 and top["UNH"]["rank"] == 1
    assert top["UNH"]["personas"] == ["Momentum Trader", "Macro Strategist", "ESG/Sustainability Investor"]
    assert {top[t]["rank"] for t in ("BDX", "ISRG", "MDT", "SYK")} == {2}
    assert top["ABBV"]["rank"] == 6 and top["AMGN"]["votes"] == 0
    assert len(r["ranking"]) == len(HC)  # zero-vote tickers listed too
    assert not r["tie_extended"] and not r["fewer_than_n"]


def test_ties_at_cutoff_are_all_included():
    r = s7.compute(HC_JSON, HC, 3)
    assert r["consensus"] == ["UNH", "BDX", "ISRG", "MDT", "SYK"]
    assert r["tie_extended"] and r["cutoff_votes"] == 2


def test_min_votes_floor_stops_one_vote_flood():
    r = s7.compute(HC_JSON, HC, 6)
    assert r["consensus"] == ["UNH", "BDX", "ISRG", "MDT", "SYK"] and r["fewer_than_n"]
    r1 = s7.compute(HC_JSON, HC, 6, min_votes=1)
    assert len(r1["consensus"]) == 19 and r1["tie_extended"]   # explicit opt-in only
    assert s7.compute(HC_JSON, HC, 1)["consensus"] == ["UNH"]


def test_duplicate_ticker_in_one_persona_is_one_vote():
    rj = {"personas": [{"persona": "A", "tickers": ["LLY", "LLY"]}, {"persona": "B", "tickers": ["LLY"]}]}
    assert s7.compute(rj, ["LLY"], 1)["ranking"][0]["votes"] == 2


def test_bad_params():
    with pytest.raises(ValueError):
        s7.compute(HC_JSON, HC, 0)
    with pytest.raises(ValueError):
        s7.compute(HC_JSON, HC, 5, min_votes=0)


def test_api_build(run_id):  # noqa: F811
    run_store.write_artifact(run_id, "01_universe.json", {**run_store.read_artifact(run_id, "01_universe.json"),
                                                          "ticker_universe": HC})
    run_store.write_artifact(run_id, "06_report.json", HC_JSON)
    client = create_app().test_client()
    r = client.post(f"/api/pipeline/runs/{run_id}/consensus", json={"n": 5})
    assert r.status_code == 400 and "04" in r.json["error"]          # report_json not completed

    run_store.update_step(run_id, "report_json", run_store.STEP_COMPLETED)
    r = client.post(f"/api/pipeline/runs/{run_id}/consensus", json={"n": 3})
    assert r.status_code == 200
    assert r.json["data"]["steps"]["consensus"]["summary"]["consensus"] == ["UNH", "BDX", "ISRG", "MDT", "SYK"]
    got = client.get(f"/api/pipeline/runs/{run_id}/consensus").json["data"]
    assert got["result"]["n"] == 3 and got["result"]["tie_extended"]
    assert "tie at 2 votes" in run_store.read_log(run_id)[-2]["message"]

    # still recomputable after step 8 (step 8 then reports itself as stale)
    run_store.update_step(run_id, "performance", run_store.STEP_COMPLETED)
    assert client.post(f"/api/pipeline/runs/{run_id}/consensus", json={"n": 5}).status_code == 200
