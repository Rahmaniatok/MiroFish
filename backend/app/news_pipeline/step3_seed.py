"""
Step 3 — feed txt_berita into MiroFish as the reality seed.

The original UI does this in one shot inside POST /api/graph/ontology/generate
(upload files + simulation prompt -> create project -> extract text -> LLM
ontology). Here only the seed half runs: create a MiroFish project and store
txt_berita exactly the way that endpoint stores an uploaded file

    uploads/projects/<project_id>/files/<random>.txt   (the file)
    uploads/projects/<project_id>/extracted_text.txt   ("=== name ===" + preprocessed text)
    project.json                                       files / total_text_length, status "created"

so the later steps (prompt, ontology, graph build, simulation) run on a
normal MiroFish project. project_id goes into run.json -> links.

Idempotent: re-running reuses the linked project and rewrites its seed. Once
the seed exists, step 2 refuses to rebuild the txt (the seed would go stale).
"""

import hashlib
import os
import shutil
from typing import Any, Dict

from ..models.project import ProjectManager, ProjectStatus
from ..services.text_processor import TextProcessor
from ..utils.file_parser import FileParser
from ..utils.logger import get_logger
from . import run_store
from .step2_news import STEP as NEWS_STEP, TXT_ARTIFACT

logger = get_logger('mirofish.news_pipeline.step3')

STEP = "seed"


def seed_filename(run_id: str) -> str:
    return f"txt_berita_{run_id}.txt"


def feed_seed(run_id: str) -> Dict[str, Any]:
    manifest = run_store.load_run(run_id)
    if manifest["steps"][NEWS_STEP]["status"] != run_store.STEP_COMPLETED:
        raise ValueError("Step 2 (news) is not completed — txt_berita is not ready yet")
    txt_path = os.path.join(run_store.PIPELINE_RUNS_DIR, run_id, TXT_ARTIFACT)
    if not os.path.exists(txt_path):
        raise ValueError("txt_berita.txt is missing — rebuild it in the News Room")

    project_id = manifest["links"].get("project_id")
    project = ProjectManager.get_project(project_id) if project_id else None
    if project is not None and project.status not in (ProjectStatus.CREATED, ProjectStatus.FAILED):
        # graph already built from this seed — nothing to redo
        if manifest["steps"][STEP]["status"] != run_store.STEP_COMPLETED:
            run_store.update_step(run_id, STEP, run_store.STEP_COMPLETED,
                                  summary=manifest["steps"][STEP].get("summary") or {"project_id": project_id})
        return run_store.load_run(run_id)

    run_store.update_step(run_id, STEP, run_store.STEP_RUNNING)
    try:
        if project is None:
            project = ProjectManager.create_project(name=f"{manifest['name']} [{run_id}]")
            run_store.append_log(run_id, "project_created", f"MiroFish project {project.project_id} created")
        else:
            # reseed an untouched project: clear the previous seed file
            for old in ProjectManager.get_project_files(project.project_id):
                os.remove(old)
            project.files = []

        filename = seed_filename(run_id)
        files_dir = os.path.join(ProjectManager.PROJECTS_DIR, project.project_id, 'files')
        os.makedirs(files_dir, exist_ok=True)
        saved = os.path.join(files_dir, f"{hashlib.sha1(run_id.encode()).hexdigest()[:8]}.txt")
        shutil.copyfile(txt_path, saved)
        size = os.path.getsize(saved)

        # same extraction as /api/graph/ontology/generate
        text = TextProcessor.preprocess_text(FileParser.extract_text(saved))
        all_text = f"\n\n=== {filename} ===\n{text}"
        ProjectManager.save_extracted_text(project.project_id, all_text)

        project.files.append({"filename": filename, "size": size})
        project.total_text_length = len(all_text)
        project.status = ProjectStatus.CREATED
        project.error = None
        ProjectManager.save_project(project)

        with open(txt_path, 'rb') as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        summary = {
            "project_id": project.project_id,
            "filename": filename,
            "bytes": size,
            "text_length": len(all_text),
            "txt_sha256": digest,
            "news_cap": (manifest["steps"][NEWS_STEP].get("summary") or {}).get("cap"),
        }
        manifest = run_store.load_run(run_id)
        manifest["links"]["project_id"] = project.project_id
        run_store.save_run(manifest)
        run_store.append_log(run_id, "seed_fed",
                             f"txt_berita ({size / 1024:.0f} KB) fed to MiroFish project {project.project_id} as reality seed")
        return run_store.update_step(run_id, STEP, run_store.STEP_COMPLETED,
                                     artifact=f"projects/{project.project_id}", summary=summary)
    except Exception as e:  # noqa: BLE001
        logger.exception("feeding reality seed failed")
        run_store.update_step(run_id, STEP, run_store.STEP_FAILED, error=str(e))
        raise
