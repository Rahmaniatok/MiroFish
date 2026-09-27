"""News pipeline step 3 — txt_berita becomes a MiroFish project's reality seed (offline)."""

import os
from datetime import date, timedelta

import pytest

from app import create_app
from app.models.project import ProjectManager, ProjectStatus
from app.news_pipeline import run_store, step2_news, step3_seed

AS_OF = (date.today() - timedelta(days=60)).isoformat()
TXT = "Stock news for 1 S&P 500 companies\n\n=== XOM — ExxonMobil (Energy, mega cap) ===\n2026-06-01 | XOM | Exxon beats earnings — strong quarter\n"


@pytest.fixture
def run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "PIPELINE_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path / "projects"))
    rid = run_store.create_run("energy", {"as_of_date": AS_OF})["run_id"]
    run_store.write_artifact(rid, "01_universe.json", {
        "as_of_date": AS_OF, "sectors": ["Energy"], "market_cap_tiers": ["mega"], "ticker_universe": ["XOM"],
        "universe": [{"ticker": "XOM", "company_name": "ExxonMobil", "gics_sector": "Energy",
                      "market_cap": 5e11, "market_cap_tier": "mega"}]})
    run_store.update_step(rid, "universe", run_store.STEP_COMPLETED)
    run_store.write_artifact(rid, step2_news.TXT_ARTIFACT, TXT)
    run_store.write_artifact(rid, step2_news._raw_name("XOM"), {
        "ticker": "XOM", "window_start": AS_OF, "window_end": AS_OF, "articles": []})
    run_store.update_step(rid, "news", run_store.STEP_COMPLETED, summary={"cap": 30})
    return rid


def test_seed_creates_project_like_upload_endpoint(run_id):
    run = step3_seed.feed_seed(run_id)
    pid = run["links"]["project_id"]
    assert run["steps"]["seed"]["status"] == "completed"
    assert run["current_step"] == 4  # next internal step: prompt

    project = ProjectManager.get_project(pid)
    assert project.status == ProjectStatus.CREATED
    assert project.files == [{"filename": step3_seed.seed_filename(run_id), "size": len(TXT.encode())}]
    extracted = ProjectManager.get_extracted_text(pid)
    assert extracted.startswith(f"\n\n=== {step3_seed.seed_filename(run_id)} ===\n")
    assert "Exxon beats earnings" in extracted
    assert project.total_text_length == len(extracted)
    assert len(ProjectManager.get_project_files(pid)) == 1


def test_seed_is_idempotent_and_locks_txt_rebuild(run_id):
    first = step3_seed.feed_seed(run_id)["links"]["project_id"]
    second = step3_seed.feed_seed(run_id)["links"]["project_id"]
    assert first == second
    assert len(ProjectManager.get_project_files(first)) == 1
    with pytest.raises(ValueError, match="reality seed"):
        step2_news.compact(run_id, 10)
    assert step2_news.status(run_id)["seed"] == {"status": "completed", "project_id": first}


def test_seed_does_not_touch_a_project_that_moved_on(run_id):
    pid = step3_seed.feed_seed(run_id)["links"]["project_id"]
    project = ProjectManager.get_project(pid)
    project.status = ProjectStatus.GRAPH_COMPLETED
    ProjectManager.save_project(project)
    before = ProjectManager.get_extracted_text(pid)
    run_store.write_artifact(run_id, step2_news.TXT_ARTIFACT, "changed")
    step3_seed.feed_seed(run_id)
    assert ProjectManager.get_extracted_text(pid) == before


def test_seed_requires_news_step(run_id):
    run_store.update_step(run_id, "news", run_store.STEP_PENDING)
    r = create_app().test_client().post(f"/api/pipeline/runs/{run_id}/seed")
    assert r.status_code == 400 and "Step 2" in r.json["error"]
