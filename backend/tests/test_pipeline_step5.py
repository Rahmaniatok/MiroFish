"""
News pipeline step 5 — ontology for an existing project (original MiroFish
API) and syncing MiroFish progress back into the run. Offline.
"""

import time
from types import SimpleNamespace

import pytest

from app import create_app
from app.api import graph as graph_api
from app.models.project import ProjectManager, ProjectStatus
from app.news_pipeline import run_store, step5_mirofish
from app.utils.llm_client import LLMResponseError
from tests.test_pipeline_step3 import run_id  # noqa: F401  (fixture: run with news completed)


@pytest.fixture
def seeded(run_id):  # noqa: F811
    """Run with seed + prompt done (as after 'USE AS REALITY SEED')."""
    create_app().test_client().post(f"/api/pipeline/runs/{run_id}/seed")
    return run_id, run_store.load_run(run_id)["links"]["project_id"]


def _wait_task(client, task_id):
    for _ in range(200):
        task = client.get(f"/api/graph/task/{task_id}").json["data"]
        if task["status"] in ("completed", "failed"):
            return task
        time.sleep(0.02)
    raise AssertionError("ontology task never finished")


class _Gen:
    calls = []

    def generate(self, **kwargs):
        _Gen.calls.append(kwargs)
        return {"entity_types": [{"name": "Company"}], "edge_types": [{"name": "MENTIONS"}],
                "analysis_summary": "news about XOM"}


def test_existing_project_ontology_uses_stored_seed_and_prompt(seeded, monkeypatch):
    run_id, pid = seeded
    _Gen.calls = []
    monkeypatch.setattr(graph_api, "OntologyGenerator", _Gen)
    client = create_app().test_client()

    r = client.post(f"/api/graph/project/{pid}/ontology")
    assert r.status_code == 202, r.json
    assert _wait_task(client, r.json["data"]["task_id"])["status"] == "completed"
    call = _Gen.calls[0]
    assert "Exxon beats earnings" in call["document_texts"][0]
    assert call["simulation_requirement"].startswith("Stock universe (MANDATORY")
    project = ProjectManager.get_project(pid)
    assert project.status == ProjectStatus.ONTOLOGY_GENERATED
    assert project.ontology["entity_types"] == [{"name": "Company"}]

    # second call reuses, no new LLM call
    r = client.post(f"/api/graph/project/{pid}/ontology")
    assert r.json["data"]["reused"] is True and len(_Gen.calls) == 1


def test_existing_project_ontology_failure_marks_project_failed(seeded, monkeypatch):
    _, pid = seeded

    class Failing:
        def generate(self, **kwargs):
            raise LLMResponseError("LLM JSON output was truncated at the token limit", finish_reason="length")

    monkeypatch.setattr(graph_api, "OntologyGenerator", Failing)
    client = create_app().test_client()
    r = client.post(f"/api/graph/project/{pid}/ontology")
    task = _wait_task(client, r.json["data"]["task_id"])
    assert task["status"] == "failed" and "token limit" in task["error"]
    project = ProjectManager.get_project(pid)
    assert project.status == ProjectStatus.FAILED and "token limit" in project.error


def test_existing_project_ontology_second_call_joins_running_task(seeded, monkeypatch):
    _, pid = seeded
    import threading
    release = threading.Event()

    class Slow:
        def generate(self, **kwargs):
            release.wait(5)
            return {"entity_types": [], "edge_types": []}

    monkeypatch.setattr(graph_api, "OntologyGenerator", Slow)
    client = create_app().test_client()
    first = client.post(f"/api/graph/project/{pid}/ontology").json["data"]["task_id"]
    second = client.post(f"/api/graph/project/{pid}/ontology").json["data"]["task_id"]
    release.set()
    assert first == second
    assert _wait_task(client, first)["status"] == "completed"


def test_existing_project_ontology_404():
    r = create_app().test_client().post("/api/graph/project/proj_nope/ontology")
    assert r.status_code == 404


def _fake_sim(monkeypatch, sim_status="running", rounds=(3, 10), report_status=None):
    sim = SimpleNamespace(simulation_id="sim_x", status=sim_status, created_at="2026-09-27T10:00:00", error=None)
    report = SimpleNamespace(report_id="report_x", status=report_status, error=None) if report_status else None
    monkeypatch.setattr(step5_mirofish, "SimulationManager",
                        lambda: SimpleNamespace(list_simulations=lambda project_id=None: [sim]))
    monkeypatch.setattr(step5_mirofish.ReportManager, "get_report_by_simulation",
                        classmethod(lambda cls, sid: report))
    monkeypatch.setattr(step5_mirofish.SimulationRunner, "get_run_state",
                        classmethod(lambda cls, sid: SimpleNamespace(current_round=rounds[0], total_rounds=rounds[1],
                                                                      runner_status=sim_status)))


def _set_project(pid, status, graph_id=None):
    project = ProjectManager.get_project(pid)
    project.status = status
    project.ontology = {"entity_types": [], "edge_types": []}
    project.graph_id = graph_id
    ProjectManager.save_project(project)


def test_sync_tracks_stages_links_and_completion(seeded, monkeypatch):
    run_id, pid = seeded
    monkeypatch.setattr(step5_mirofish, "SimulationManager",
                        lambda: SimpleNamespace(list_simulations=lambda project_id=None: []))
    assert step5_mirofish.sync(run_id)["steps"]["simulation"]["status"] == "pending"

    _set_project(pid, ProjectStatus.GRAPH_BUILDING)
    m = step5_mirofish.sync(run_id)
    assert m["steps"]["simulation"]["status"] == "running"
    assert m["steps"]["simulation"]["summary"]["stages"] == {
        "ontology": "completed", "graph": "running", "simulation": "pending", "report": "pending"}

    _set_project(pid, ProjectStatus.GRAPH_COMPLETED, graph_id="mirofish_g1")
    _fake_sim(monkeypatch, "running", (3, 10))
    m = step5_mirofish.sync(run_id)
    assert m["links"]["graph_id"] == "mirofish_g1" and m["links"]["simulation_id"] == "sim_x"
    assert m["steps"]["simulation"]["summary"]["current_round"] == 3

    _fake_sim(monkeypatch, "completed", (10, 10), report_status="completed")
    m = step5_mirofish.sync(run_id)
    assert m["steps"]["simulation"]["status"] == "completed"
    assert m["links"]["report_id"] == "report_x"
    assert m["current_step"] == 6  # next backend step: report_json

    events = [l["event"] for l in run_store.read_log(run_id)]
    for e in ("mirofish_ontology_completed", "mirofish_graph_running", "mirofish_graph_completed",
              "mirofish_simulation_running", "mirofish_simulation_completed", "mirofish_report_completed"):
        assert e in events
    # idempotent: another sync adds no log lines
    n = len(run_store.read_log(run_id))
    step5_mirofish.sync(run_id)
    assert len(run_store.read_log(run_id)) == n


def test_sync_reports_failure(seeded, monkeypatch):
    run_id, pid = seeded
    _set_project(pid, ProjectStatus.GRAPH_COMPLETED, graph_id="g")
    _fake_sim(monkeypatch, "failed", (2, 10))
    m = step5_mirofish.sync(run_id)
    assert m["steps"]["simulation"]["status"] == "failed"
    assert m["status"] == "failed"
