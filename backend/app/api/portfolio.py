"""
Phase 7a — portfolio API.

Thin HTTP wrappers over the Phase 5/6 services (`build_portfolio`,
`PortfolioAgent`). Every route only validates the request and serializes the
result of an existing service call — no business logic is re-implemented
here.
"""

from flask import jsonify, request

from . import portfolio_bp
from ..services import (
    DEFAULT_DEBATE_ROUNDS,
    VALID_MODELS,
    MODEL_MAX_SHARPE,
    DebateError,
    PortfolioAgent,
    PortfolioAgentError,
    SeedBuildError,
    build_portfolio,
    build_seed_from_ticker,
)
from ..utils.logger import get_logger

logger = get_logger('mirofish.api')


def _err(message: str, status: int):
    return jsonify({"success": False, "error": message}), status


@portfolio_bp.route('/graph', methods=['GET'])
def graph():
    """
    GET /api/portfolio/graph?ticker=MSFT&as_of_date=2024-06-01

    Phase 7b (dashboard workbench) needs the per-ticker entity graph that
    Phase 7a's original 4 endpoints didn't expose. Thin wrapper over Phase
    2b/2c's `build_seed_from_ticker()` — returns its `FilteredEntities.to_dict()`
    verbatim (entities/entity_types/total_count/filtered_count), unadapted;
    the frontend's graph adapter reshapes it for GraphPanel.
    """
    ticker = (request.args.get('ticker') or '').strip()
    if not ticker:
        return _err("ticker query parameter is required", 400)
    as_of_date = request.args.get('as_of_date') or None

    try:
        seed = build_seed_from_ticker(ticker, as_of_date)
    except SeedBuildError as e:
        return _err(str(e), 400)
    except Exception:
        logger.exception("build_seed_from_ticker failed")
        return _err("internal error while building the entity graph", 500)

    return jsonify(seed.to_dict())


@portfolio_bp.route('/build', methods=['POST'])
def build():
    """
    POST /api/portfolio/build
    body: {candidate_tickers: list[str], as_of_date: str|null, top_k: int,
           max_weight: float, model: "max_sharpe"|"min_variance"|"hrp" (optional)}

    Returns the full `build_portfolio()` result dict as JSON.
    """
    data = request.get_json(silent=True) or {}

    candidate_tickers = data.get('candidate_tickers')
    if (
        not isinstance(candidate_tickers, list)
        or not candidate_tickers
        or not all(isinstance(t, str) and t.strip() for t in candidate_tickers)
    ):
        return _err(
            "candidate_tickers is required and must be a non-empty list of ticker strings",
            400,
        )

    as_of_date = data.get('as_of_date')
    if as_of_date is not None and not isinstance(as_of_date, str):
        return _err("as_of_date must be a string or null", 400)

    top_k = data.get('top_k', 25)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        return _err("top_k must be a positive integer", 400)

    max_weight = data.get('max_weight', 0.20)
    if (
        not isinstance(max_weight, (int, float))
        or isinstance(max_weight, bool)
        or not (0 < max_weight <= 1)
    ):
        return _err("max_weight must be a number in (0, 1]", 400)

    model = data.get('model', MODEL_MAX_SHARPE)
    if not isinstance(model, str) or model not in VALID_MODELS:
        return _err(f"model must be one of {list(VALID_MODELS)}, got {model!r}", 400)

    try:
        result = build_portfolio(
            [t.strip().upper() for t in candidate_tickers],
            as_of_date=as_of_date,
            top_k=top_k,
            max_weight=float(max_weight),
            model=model,
        )
    except SeedBuildError as e:
        return _err(str(e), 400)
    except ValueError as e:
        return _err(str(e), 400)
    except Exception:
        logger.exception("build_portfolio failed")
        return _err("internal error while building portfolio", 500)

    return jsonify(result)


@portfolio_bp.route('/ask', methods=['POST'])
def ask():
    """
    POST /api/portfolio/ask
    body: {question: str, portfolio: dict}

    Returns {"answer": str}.
    """
    data = request.get_json(silent=True) or {}

    question = data.get('question')
    if not isinstance(question, str) or not question.strip():
        return _err("question is required and must be a non-empty string", 400)

    portfolio = data.get('portfolio')
    if not isinstance(portfolio, dict):
        return _err("portfolio is required and must be an object (a build_portfolio() result)", 400)

    try:
        answer_text = PortfolioAgent().answer(question, portfolio)
    except PortfolioAgentError as e:
        return _err(str(e), 502)
    except Exception:
        logger.exception("PortfolioAgent.answer failed")
        return _err("internal error while answering question", 500)

    return jsonify({"answer": answer_text})


@portfolio_bp.route('/debate', methods=['POST'])
def debate():
    """
    POST /api/portfolio/debate
    body: {ticker: str, portfolio: dict, rounds: int, use_llm: bool (optional, default true)}

    Returns the DebateTranscript as JSON (its `to_dict()`).

    `use_llm` is not in the original spec but is a pass-through of
    `PortfolioAgent.get_debate`'s own parameter (default True), kept so callers
    (and this module's own offline tests) can request the deterministic
    template path without a live LLM.
    """
    data = request.get_json(silent=True) or {}

    ticker = data.get('ticker')
    if not isinstance(ticker, str) or not ticker.strip():
        return _err("ticker is required and must be a non-empty string", 400)

    portfolio = data.get('portfolio')
    if not isinstance(portfolio, dict):
        return _err("portfolio is required and must be an object (a build_portfolio() result)", 400)

    rounds = data.get('rounds', DEFAULT_DEBATE_ROUNDS)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        return _err("rounds must be a positive integer", 400)

    use_llm = data.get('use_llm', True)
    if not isinstance(use_llm, bool):
        return _err("use_llm must be a boolean", 400)

    try:
        transcript = PortfolioAgent().get_debate(ticker, portfolio, rounds=rounds, use_llm=use_llm)
    except PortfolioAgentError as e:
        return _err(str(e), 400)
    except SeedBuildError as e:
        return _err(str(e), 400)
    except DebateError as e:
        return _err(str(e), 400)
    except Exception:
        logger.exception("PortfolioAgent.get_debate failed")
        return _err("internal error while running debate", 500)

    return jsonify(transcript.to_dict())
