"""
News pipeline API (/api/pipeline) — resumable 8-step runs, see
app/news_pipeline/run_store.py. Responses follow the project convention
{"success": true, "data": ...} / {"success": false, "error": ...}.
"""

from flask import jsonify, request

from . import pipeline_bp
from ..news_pipeline import run_store, step1_universe
from ..utils.logger import get_logger

logger = get_logger('mirofish.api.pipeline')


def _ok(data, status=200):
    return jsonify({"success": True, "data": data}), status


def _err(message, status=400):
    return jsonify({"success": False, "error": message}), status


# ---------------------------------------------------------------- step 1 ----

@pipeline_bp.route('/universe/options', methods=['GET'])
def universe_options():
    return _ok(step1_universe.get_options())


@pipeline_bp.route('/universe/validate-as-of', methods=['GET'])
def validate_as_of():
    try:
        return _ok(step1_universe.validate_as_of(request.args.get('as_of_date', '')))
    except ValueError as e:
        return _err(str(e))


@pipeline_bp.route('/universe/constituents', methods=['GET'])
def universe_constituents():
    sectors = [s for s in request.args.get('sectors', '').split(',') if s]
    try:
        return _ok(step1_universe.list_constituents(sectors or None))
    except Exception as e:  # noqa: BLE001
        logger.exception("constituents failed")
        return _err(str(e), 500)


@pipeline_bp.route('/universe/screen', methods=['POST'])
def universe_screen():
    body = request.get_json(silent=True) or {}
    try:
        task_id = step1_universe.start_screen(
            body.get('sectors') or [], body.get('market_cap_tiers') or [], body.get('as_of_date', ''))
    except ValueError as e:
        return _err(str(e))
    return _ok({"task_id": task_id}, 202)


@pipeline_bp.route('/universe/screen/<task_id>', methods=['GET'])
def universe_screen_status(task_id):
    task = step1_universe.get_screen(task_id)
    if task is None:
        return _err("Screening task not found (backend restarted?) — please screen again", 404)
    return _ok(task)


@pipeline_bp.route('/runs', methods=['POST'])
def create_run():
    """Lock a finished screen as ticker_universe -> new run with step 1 completed."""
    body = request.get_json(silent=True) or {}
    try:
        manifest = step1_universe.lock_universe(
            body.get('task_id', ''), (body.get('name') or '').strip(),
            body.get('excluded_tickers') or [], body.get('market_cap_tiers') or None)
    except LookupError as e:
        return _err(str(e), 404)
    except ValueError as e:
        return _err(str(e))
    return _ok(manifest, 201)


# ------------------------------------------------------------------ runs ----

@pipeline_bp.route('/runs', methods=['GET'])
def list_runs():
    return _ok(run_store.list_runs())


@pipeline_bp.route('/runs/<run_id>', methods=['GET'])
def get_run(run_id):
    try:
        manifest = run_store.load_run(run_id)
    except run_store.RunNotFoundError:
        return _err(f"Run not found: {run_id}", 404)
    return _ok({**manifest, "universe": run_store.read_artifact(run_id, step1_universe.ARTIFACT)})


@pipeline_bp.route('/runs/<run_id>/log', methods=['GET'])
def get_run_log(run_id):
    try:
        run_store.load_run(run_id)
    except run_store.RunNotFoundError:
        return _err(f"Run not found: {run_id}", 404)
    return _ok(run_store.read_log(run_id, request.args.get('limit', type=int)))
