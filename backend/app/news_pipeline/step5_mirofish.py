"""
Step 5 — run MiroFish. The user drives the ORIGINAL MiroFish UI
(/process/<project_id> -> simulation -> report) on the project that steps 3-4
prepared; this module only reads that progress back into the run:

    ontology   project.ontology present
    graph      project status graph_completed (+ graph_id)
    simulation latest simulation of the project (+ run_state rounds)
    report     report of that simulation

and records graph_id / simulation_id / report_id in run.json -> links, so a
resumed run (and step 6) finds them. The backend "simulation" step is
completed once the report is completed.
"""

from typing import Any, Dict, Optional

from ..models.project import ProjectManager, ProjectStatus
from ..services.report_agent import ReportManager
from ..services.simulation_manager import SimulationManager
from ..services.simulation_runner import SimulationRunner
from ..utils.logger import get_logger
from . import run_store

logger = get_logger('mirofish.news_pipeline.step5')

STEP = "simulation"
STAGES = ("ontology", "graph", "simulation", "report")


def _value(v: Any) -> Any:
    return getattr(v, "value", v)


def _pick_report(simulation_id: str):
    """
    Newest completed report of the simulation, else its newest report.
    (ReportManager.get_report_by_simulation returns whichever it meets first on
    disk — after a failed report is retried there are two, and the failed one
    could win.)
    """
    reports = ReportManager.list_reports(simulation_id=simulation_id)   # newest first
    if not reports:
        return None
    return next((r for r in reports if _value(r.status) == "completed"), reports[0])


def _pick_simulation(project_id: str):
    """Latest simulation of the project that has a report, else the latest one."""
    sims = SimulationManager().list_simulations(project_id=project_id)
    if not sims:
        return None, None
    sims.sort(key=lambda s: s.created_at, reverse=True)
    for sim in sims:
        report = _pick_report(sim.simulation_id)
        if report is not None:
            return sim, report
    return sims[0], None


def inspect(project_id: str) -> Optional[Dict[str, Any]]:
    project = ProjectManager.get_project(project_id)
    if project is None:
        return None
    status = _value(project.status)
    stages = {s: "pending" for s in STAGES}
    info: Dict[str, Any] = {"project_id": project_id, "project_status": status,
                            "graph_id": project.graph_id, "simulation_id": None,
                            "report_id": None, "error": project.error if status == "failed" else None}

    if project.ontology:
        stages["ontology"] = "completed"
    elif status == "failed":
        stages["ontology"] = "failed"
    if status == ProjectStatus.GRAPH_BUILDING.value:
        stages["graph"] = "running"
    elif status == ProjectStatus.GRAPH_COMPLETED.value and project.graph_id:
        stages["graph"] = "completed"
    elif status == "failed" and project.ontology:
        stages["graph"] = "failed"

    sim, report = _pick_simulation(project_id)
    if sim is not None:
        sim_status = _value(sim.status)
        info.update(simulation_id=sim.simulation_id, simulation_status=sim_status)
        run_state = SimulationRunner.get_run_state(sim.simulation_id)
        if run_state is not None:
            info.update(current_round=run_state.current_round, total_rounds=run_state.total_rounds,
                        runner_status=_value(run_state.runner_status))
        if sim_status == "completed" or (run_state and _value(run_state.runner_status) == "completed"):
            stages["simulation"] = "completed"
        elif sim_status == "failed":
            stages["simulation"] = "failed"
            info["error"] = sim.error
        else:
            stages["simulation"] = "running"
    if report is not None:
        report_status = _value(report.status)
        info.update(report_id=report.report_id, report_status=report_status)
        stages["report"] = {"completed": "completed", "failed": "failed"}.get(report_status, "running")
        if report_status == "failed":
            info["error"] = report.error
    info["stages"] = stages
    return info


def sync(run_id: str) -> Dict[str, Any]:
    """Refresh the run's links + simulation step from the MiroFish files. Cheap; safe to call often."""
    manifest = run_store.load_run(run_id)
    project_id = manifest["links"].get("project_id")
    if not project_id or manifest["steps"]["prompt"]["status"] != run_store.STEP_COMPLETED:
        return manifest
    info = inspect(project_id)
    if info is None:
        return manifest

    links = {k: info.get(k) for k in ("graph_id", "simulation_id", "report_id")}
    if any(manifest["links"].get(k) != v for k, v in links.items() if v):
        manifest = run_store.load_run(run_id)
        manifest["links"].update({k: v for k, v in links.items() if v})
        run_store.save_run(manifest)

    stages = info["stages"]
    if stages["report"] == "completed":
        status = run_store.STEP_COMPLETED
    elif "failed" in stages.values():
        status = run_store.STEP_FAILED
    elif any(v != "pending" for v in stages.values()):
        status = run_store.STEP_RUNNING
    else:
        status = run_store.STEP_PENDING

    step = manifest["steps"][STEP]
    old_stages = (step.get("summary") or {}).get("stages", {})
    for name in STAGES:
        if stages[name] != old_stages.get(name, "pending") and stages[name] != "pending":
            run_store.append_log(run_id, f"mirofish_{name}_{stages[name]}",
                                 f"MiroFish {name}: {stages[name]}"
                                 + (f" ({info.get(name + '_id')})" if info.get(name + '_id') else ""))
    if status != step["status"]:
        return run_store.update_step(run_id, STEP, status, summary=info,
                                     error=info.get("error") if status == run_store.STEP_FAILED else None)
    if info != step.get("summary"):
        return run_store.patch_step(run_id, STEP, summary=info)
    return manifest
