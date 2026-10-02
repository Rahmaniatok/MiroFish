"""
News pipeline step 1 (ticker_universe) + run store — offline.
_run_screen is stubbed, so no yfinance / Wikipedia calls.
"""

import time
from datetime import date, timedelta

import pytest

from app import create_app
from app.news_pipeline import run_store, step1_universe


_SCREEN_ROWS = [
    {"ticker": "XOM", "company_name": "Exxon", "gics_sector": "Energy", "gics_sub_industry": "Integrated Oil & Gas",
     "market_cap": 450e9, "market_cap_tier": "mega"},
    {"ticker": "COP", "company_name": "Conoco", "gics_sector": "Energy", "gics_sub_industry": "Oil & Gas Exploration & Production",
     "market_cap": 120e9, "market_cap_tier": "large"},
    {"ticker": "APA", "company_name": "APA", "gics_sector": "Energy", "gics_sub_industry": "Oil & Gas Exploration & Production",
     "market_cap": 8e9, "market_cap_tier": "mid"},
]


def _fake_run_screen(sectors, tiers, as_of_date, progress_callback=None):
    passed, n = [], 0
    for row in _SCREEN_ROWS:
        n += 1
        outcome = "passed" if row["market_cap_tier"] in tiers else "filtered"
        if outcome == "passed":
            passed.append(row)
        progress_callback({"processed": n, "in_scope": 4, "ticker": row["ticker"],
                           "outcome": outcome, "entry": row, "reason": None})
    progress_callback({"processed": 4, "in_scope": 4, "ticker": "BAD",
                       "outcome": "skipped", "entry": None, "reason": "no data"})
    return {"passed": passed, "skipped": [{"ticker": "BAD", "reason": "no data"}], "total": 503}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "PIPELINE_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(step1_universe, "_run_screen", _fake_run_screen)
    return create_app().test_client()


def _as_of(days_back):
    return (date.today() - timedelta(days=days_back)).isoformat()


def _screen(client, tiers):
    r = client.post('/api/pipeline/universe/screen',
                    json={"sectors": ["Energy"], "market_cap_tiers": tiers, "as_of_date": _as_of(60)})
    assert r.status_code == 202
    task_id = r.json["data"]["task_id"]
    for _ in range(100):
        task = client.get(f'/api/pipeline/universe/screen/{task_id}').json["data"]
        if task["status"] == "completed":
            return task_id, task
        time.sleep(0.02)
    raise AssertionError("screen never finished")


def test_validate_as_of_bounds():
    with pytest.raises(ValueError, match="future"):
        step1_universe.validate_as_of(_as_of(-1))
    with pytest.raises(ValueError, match="Finnhub"):
        step1_universe.validate_as_of(_as_of(400))
    assert step1_universe.validate_as_of(_as_of(60))["warnings"] == []
    assert "partly empty" in step1_universe.validate_as_of(_as_of(300))["warnings"][0]
    assert "step 8" in step1_universe.validate_as_of(_as_of(5))["warnings"][0]


def test_screen_rejects_bad_filters(client):
    r = client.post('/api/pipeline/universe/screen',
                    json={"sectors": ["Crypto"], "market_cap_tiers": ["mega"], "as_of_date": _as_of(60)})
    assert r.status_code == 400 and "Unknown sector" in r.json["error"]


def test_screen_streams_progress_and_result(client):
    _, task = _screen(client, ["mega", "large", "mid", "small"])
    detail = task["progress_detail"]
    assert [r["ticker"] for r in detail["passed"]] == ["XOM", "COP", "APA"]
    assert detail["skipped"] == [{"ticker": "BAD", "reason": "no data"}]
    assert task["result"]["sp500_total"] == 503


def test_lock_refilters_tiers_and_excludes_without_rescreen(client):
    task_id, _ = _screen(client, ["mega", "large", "mid", "small"])
    r = client.post('/api/pipeline/runs', json={
        "task_id": task_id, "name": "energy", "market_cap_tiers": ["large", "mega"],
        "excluded_tickers": ["cop"]})
    assert r.status_code == 201
    run = r.json["data"]
    assert run["config"]["market_cap_tiers"] == ["mega", "large"]  # canonical order
    assert run["current_step"] == 2
    assert run["steps"]["universe"]["status"] == "completed"
    assert run["steps"]["universe"]["summary"]["ticker_count"] == 1

    full = client.get(f'/api/pipeline/runs/{run["run_id"]}').json["data"]
    assert full["universe"]["ticker_universe"] == ["XOM"]
    assert full["universe"]["excluded_by_user"] == ["COP"]
    assert [r["ticker"] for r in full["universe"]["filtered_out"]] == ["APA"]

    events = [l["event"] for l in client.get(f'/api/pipeline/runs/{run["run_id"]}/log').json["data"]]
    assert events == ["run_created", "step_running", "universe_locked", "step_completed"]
    assert [x["run_id"] for x in client.get('/api/pipeline/runs').json["data"]] == [run["run_id"]]


def test_lock_rejects_unscreened_tier_and_empty_universe(client):
    task_id, _ = _screen(client, ["mega"])
    r = client.post('/api/pipeline/runs', json={"task_id": task_id, "market_cap_tiers": ["small"]})
    assert r.status_code == 400 and "not screened" in r.json["error"]
    r = client.post('/api/pipeline/runs', json={"task_id": task_id, "excluded_tickers": ["XOM"]})
    assert r.status_code == 400 and "empty" in r.json["error"]
    r = client.post('/api/pipeline/runs', json={"task_id": "nope"})
    assert r.status_code == 404


def test_run_survives_reload_and_rejects_path_ids(client):
    manifest = run_store.create_run("x", {"as_of_date": "2026-06-30"})
    run_store.update_step(manifest["run_id"], "universe", run_store.STEP_COMPLETED)
    run_store.update_step(manifest["run_id"], "news", run_store.STEP_FAILED, error="boom")
    reloaded = run_store.load_run(manifest["run_id"])
    assert reloaded["status"] == run_store.RUN_FAILED
    assert reloaded["current_step"] == 2
    assert reloaded["steps"]["news"]["error"] == "boom"
    assert client.get('/api/pipeline/runs/..%2Fetc').status_code == 404


def test_lock_filters_by_industry(client):
    task_id, _ = _screen(client, ["mega", "large", "mid", "small"])
    r = client.post('/api/pipeline/runs', json={"task_id": task_id, "market_cap_tiers": ["mega", "large", "mid"],
                                                 "industries": ["Oil & Gas Exploration & Production"]})
    assert r.status_code == 201
    run = r.json["data"]
    u = client.get(f'/api/pipeline/runs/{run["run_id"]}').json["data"]["universe"]
    assert u["ticker_universe"] == ["COP", "APA"]
    assert [x["ticker"] for x in u["industry_filtered_out"]] == ["XOM"]
    assert run["config"]["industries"] == ["Oil & Gas Exploration & Production"]
    assert run["steps"]["universe"]["summary"]["by_industry"] == {"Oil & Gas Exploration & Production": 2}


def test_lock_all_industries_by_default_and_rejects_unknown(client):
    task_id, _ = _screen(client, ["mega", "large", "mid", "small"])
    run = client.post('/api/pipeline/runs', json={"task_id": task_id}).json["data"]
    assert run["config"]["industries"] is None
    assert "all industries" in run_store.read_log(run["run_id"])[-2]["message"]
    r = client.post('/api/pipeline/runs', json={"task_id": task_id, "industries": ["Semiconductors"]})
    assert r.status_code == 400 and "Semiconductors" in r.json["error"]

