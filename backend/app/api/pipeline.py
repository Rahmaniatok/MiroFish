"""
News pipeline API (/api/pipeline) — resumable 8-step runs, see
app/news_pipeline/run_store.py. Responses follow the project convention
{"success": true, "data": ...} / {"success": false, "error": ...}.
"""

from flask import Response, jsonify, request

from . import pipeline_bp
from ..news_pipeline import run_store, step1_universe, step2_news, step3_seed, step4_prompt, step5_mirofish
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

def _synced(manifest):
    """Pull MiroFish (step 5) progress into the run; never let it break a read."""
    try:
        return step5_mirofish.sync(manifest["run_id"])
    except Exception:  # noqa: BLE001
        logger.exception("MiroFish sync failed for %s", manifest["run_id"])
        return manifest


@pipeline_bp.route('/runs', methods=['GET'])
def list_runs():
    return _ok([_synced(m) for m in run_store.list_runs()])


@pipeline_bp.route('/runs/<run_id>', methods=['GET'])
def get_run(run_id):
    try:
        manifest = _synced(run_store.load_run(run_id))
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


# ---------------------------------------------------------------- step 2 ----

def _news_call(fn, *args):
    try:
        return _ok(fn(*args))
    except run_store.RunNotFoundError as e:
        return _err(f"Run not found: {e}", 404)
    except LookupError as e:
        return _err(str(e), 404)
    except step2_news.JobConflictError as e:
        return _err(str(e), 409)
    except ValueError as e:
        return _err(str(e))


@pipeline_bp.route('/runs/<run_id>/news', methods=['GET'])
def news_status(run_id):
    return _news_call(step2_news.status, run_id)


@pipeline_bp.route('/runs/<run_id>/news/start', methods=['POST'])
def news_start(run_id):
    """Start or resume fetching (tickers already fetched are skipped)."""
    return _news_call(step2_news.start, run_id)


@pipeline_bp.route('/runs/<run_id>/news/pause', methods=['POST'])
def news_pause(run_id):
    return _news_call(step2_news.pause, run_id)


@pipeline_bp.route('/runs/<run_id>/news/articles', methods=['GET'])
def news_articles(run_id):
    return _news_call(step2_news.articles, run_id, request.args.get('ticker', ''),
                      request.args.get('view', 'all'))


@pipeline_bp.route('/runs/<run_id>/news/compact', methods=['POST'])
def news_compact(run_id):
    """Rebuild txt_berita with another per-ticker cap (no refetch)."""
    body = request.get_json(silent=True) or {}
    return _news_call(step2_news.compact, run_id, body.get('cap'))


@pipeline_bp.route('/runs/<run_id>/news/txt', methods=['GET'])
def news_txt(run_id):
    try:
        text = step2_news.txt(run_id)
    except run_store.RunNotFoundError:
        return _err(f"Run not found: {run_id}", 404)
    if text is None:
        return _err("txt_berita has not been generated yet", 404)
    if request.args.get('download'):
        return Response(text, mimetype='text/plain; charset=utf-8', headers={
            'Content-Disposition': f'attachment; filename="txt_berita_{run_id}.txt"'})
    return _ok({"text": text, "bytes": len(text.encode('utf-8')), "lines": text.count("\n")})


# ---------------------------------------------------------------- step 3 ----

@pipeline_bp.route('/runs/<run_id>/seed', methods=['POST'])
def seed_feed(run_id):
    """
    Feed txt_berita into a MiroFish project as its reality seed, then set the
    simulation prompt (step 4) on it — both deterministic and idempotent.
    """
    def seed_and_prompt(rid):
        step3_seed.feed_seed(rid)
        return step4_prompt.build_prompt(rid)
    return _news_call(seed_and_prompt, run_id)


# ---------------------------------------------------------------- step 4 ----

@pipeline_bp.route('/runs/<run_id>/prompt', methods=['GET'])
def prompt_get(run_id):
    return _news_call(step4_prompt.get_prompt, run_id)


@pipeline_bp.route('/runs/<run_id>/prompt', methods=['POST'])
def prompt_build(run_id):
    """(Re)build the simulation prompt from ticker_universe."""
    return _news_call(step4_prompt.build_prompt, run_id)

