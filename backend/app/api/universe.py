"""
Phase 7a — universe screening API.

Thin HTTP wrapper over Phase 1d's `screen_universe`. This is what the
frontend calls to get `candidate_tickers` before calling
`/api/portfolio/build`. No business logic here — the route only parses query
params and serializes whatever `screen_universe` returns.
"""

from typing import List, Optional

from flask import jsonify, request

from . import universe_bp
from ..data_layer.universe import screen_universe
from ..utils.logger import get_logger

logger = get_logger('mirofish.api')


def _err(message: str, status: int):
    return jsonify({"success": False, "error": message}), status


def _split_csv(raw: Optional[str]) -> Optional[List[str]]:
    if raw is None or raw.strip() == "":
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


@universe_bp.route('/screen', methods=['GET'])
def screen():
    """
    GET /api/universe/screen?sectors=Information Technology,Health Care
                             &market_cap_tiers=mega,large
                             &as_of_date=2024-06-01

    Returns the screened candidate list (JSON array) — each item:
    {"ticker", "company_name", "gics_sector", "market_cap", "market_cap_tier"}.
    """
    sectors = _split_csv(request.args.get('sectors'))
    market_cap_tiers = _split_csv(request.args.get('market_cap_tiers'))
    as_of_date = request.args.get('as_of_date') or None

    try:
        candidates = screen_universe(
            sectors=sectors,
            market_cap_tiers=market_cap_tiers,
            as_of_date=as_of_date,
        )
    except ValueError as e:
        return _err(str(e), 400)
    except Exception:
        logger.exception("universe screen failed")
        return _err("internal error while screening universe", 500)

    return jsonify(candidates)
