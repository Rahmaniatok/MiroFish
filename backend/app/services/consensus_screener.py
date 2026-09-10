"""
Phase 5b — cheap, LLM-free consensus scoring + compliance hard filter.

Phase 3-4 build a full multi-round debate room (6 personas, LLM prose, several
model calls per ticker). That is far too expensive to run across the dozens of
candidate tickers Phase 1d's `screen_universe` can emit. Phase 5b takes the
*deterministic core* of that pipeline only:

    build_seed_from_ticker(ticker)            # Phase 2b  - yfinance + entity graph, no LLM
      -> _index_seed_entities(...)            # Phase 3a  - fold graph into a metric lookup
      -> _derive_investor_stance(key, idx)    # Phase 3a + 5a  - (stance, score) per archetype
      -> _derive_sharia_verdict(idx)          # Phase 3d  - compliant / non_compliant / ...

`_derive_investor_stance` and `_derive_sharia_verdict` are PURE functions: they
read only the indexed seed dict and return a plain dict. They import nothing
from the LLM path and never touch `create_chat_completion` / the OpenAI client /
Zep. `compute_consensus` therefore makes exactly zero model calls, and
`screen_and_rank` stays cheap enough to run over a whole screened universe.

Placement rationale
-------------------
This module lives in `app/services/` (not `app/data_layer/`): it orchestrates
Phase 2b's `seed_builder` and Phase 3/5a's `oasis_profile_generator`, both of
which are service-layer modules. The data layer is deliberately kept isolated
from persona / scoring code (see `universe.py`'s module docstring and the
project notes) - it must not grow a dependency on the seed graph or the
archetype derivations. This is the services-layer aggregator that sits on top
of the data layer.
"""

import time
from typing import Any, Dict, List, Optional

from ..utils.logger import get_logger
from .oasis_profile_generator import (
    INVESTOR_ARCHETYPES,
    _derive_investor_stance,
    _derive_sharia_verdict,
    _index_seed_entities,
)
from .seed_builder import SeedBuildError, build_seed_from_ticker

logger = get_logger("mirofish.consensus")

# The 5 investor archetype keys, in their canonical order (value / growth /
# technical / quality / macro). Gamma ("sharia") is deliberately NOT here - its
# verdict is a hard filter, never a term in the consensus mean.
_INVESTOR_KEYS: List[str] = [a["key"] for a in INVESTOR_ARCHETYPES]

# A ticker only clears the compliance gate on an explicit "compliant" verdict.
# "non_compliant" is obviously out; "indeterminate" / "unknown" also do NOT pass
# - the filter is conservative by design (we cannot assert compliance we could
# not compute), consistent with Phase 3d treating `overall_compliant` as a
# PARTIAL screen.
_PASSING_VERDICTS = frozenset({"compliant"})


def compute_consensus(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """One ticker -> its LLM-free consensus score + compliance verdict.

    Pipeline (no LLM anywhere):
      1. `build_seed_from_ticker(ticker, as_of_date)` - Phase 2b entity graph.
      2. `_derive_investor_stance(key, idx)` for each of the 5 investor
         archetypes, run directly on the indexed seed (Phase 3a + 5a). We do
         NOT call `generate_profiles_from_entities` - that would spin up the
         LLM persona path, and we only need the deterministic scores.
      3. `_derive_sharia_verdict(idx)` - Gamma's compliance check (Phase 3d).

    Returns a dict:
      {
        "ticker": str,
        "as_of_date": str | None,           # echoes the argument (None = live)
        "consensus_score": float,           # simple mean of the 5 archetype scores, in [-1, 1]
        "per_archetype_scores": {key: float, ...},   # all 5, for transparency / debugging
        "per_archetype_stances": {key: str, ...},    # bullish / neutral / bearish, ditto
        "passes_compliance": bool,          # True only on an explicit "compliant" verdict
        "compliance_verdict": str,          # compliant / non_compliant / indeterminate / unknown
        "compliance_reason": str | None,    # Gamma's rationale; None iff passes_compliance is True
      }

    Raises:
        SeedBuildError: propagated from `build_seed_from_ticker` when the ticker
            cannot be resolved at all (bad symbol, data source down, price AND
            fundamental both failed). `screen_and_rank` catches this per-ticker;
            direct callers get the exception.
    """
    seed = build_seed_from_ticker(ticker, as_of_date)
    idx = _index_seed_entities(seed.entities)

    per_archetype_scores: Dict[str, float] = {}
    per_archetype_stances: Dict[str, str] = {}
    for key in _INVESTOR_KEYS:
        stance_info = _derive_investor_stance(key, idx)
        per_archetype_scores[key] = float(stance_info["score"])
        per_archetype_stances[key] = stance_info["stance"]

    # Unweighted mean of the 5 archetype scores. NOTE: archetype weighting
    # (e.g. lean on Quality/Value in a late cycle, on Growth/Technical in a
    # momentum regime, or weight by each archetype's historical hit rate) is a
    # plausible future refinement - but we have NOT decided on a weighting
    # scheme, so this stays a plain average for now. Any change here should be
    # a deliberate, separately-reviewed step.
    consensus_score = sum(per_archetype_scores.values()) / len(_INVESTOR_KEYS)

    verdict_info = _derive_sharia_verdict(idx)
    verdict = verdict_info["verdict"]
    passes_compliance = verdict in _PASSING_VERDICTS

    return {
        "ticker": (ticker or "").strip().upper(),
        "as_of_date": as_of_date,
        "consensus_score": consensus_score,
        "per_archetype_scores": per_archetype_scores,
        "per_archetype_stances": per_archetype_stances,
        "passes_compliance": passes_compliance,
        "compliance_verdict": verdict,
        "compliance_reason": None if passes_compliance else verdict_info["rationale"],
    }


def _run_consensus_screen(
    tickers: List[str],
    as_of_date: Optional[str],
    top_k: int,
) -> Dict[str, Any]:
    """Screening main loop. Returns {"ranked", "excluded_non_compliant", "skipped"}.

    `screen_and_rank` is the thin public wrapper (it returns "ranked" only);
    the CLI / tests want the excluded + skipped breakdown too.
    """
    if top_k < 0:
        raise ValueError(f"top_k must be >= 0, got {top_k}")

    total = len(tickers)
    logger.info(
        "consensus screen: %d ticker(s) | as_of_date=%s | top_k=%d",
        total, as_of_date or "live", top_k,
    )

    passed: List[Dict[str, Any]] = []
    excluded_non_compliant: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    seen: set = set()

    for i, raw_ticker in enumerate(tickers, start=1):
        ticker = (raw_ticker or "").strip().upper()
        logger.info("processing %d/%d: %s", i, total, ticker or "<empty>")

        if not ticker:
            skipped.append({"ticker": str(raw_ticker), "reason": "empty ticker"})
            continue
        if ticker in seen:
            logger.info("skip %s: duplicate in input list", ticker)
            continue
        seen.add(ticker)

        # One bad ticker must not crash the batch - same lenient pattern as
        # Phase 1d's `screen_universe` (skip + log a reason, keep going).
        try:
            result = compute_consensus(ticker, as_of_date)
        except SeedBuildError as exc:
            reason = f"seed build failed: {exc}"
            logger.warning("skip %s: %s", ticker, reason)
            skipped.append({"ticker": ticker, "reason": reason})
            continue
        except Exception as exc:  # noqa: BLE001 - defensive; never let one ticker abort the run
            reason = f"unexpected error ({type(exc).__name__}: {exc})"
            logger.warning("skip %s: %s", ticker, reason)
            skipped.append({"ticker": ticker, "reason": reason})
            continue

        # HARD FILTER: a non-compliant (or indeterminate / unknown) ticker is
        # dropped entirely - it never enters the ranked list, regardless of how
        # high its consensus score is.
        if not result["passes_compliance"]:
            logger.info(
                "exclude %s: compliance verdict=%s (consensus_score=%.4f would have ranked it otherwise)",
                ticker, result["compliance_verdict"], result["consensus_score"],
            )
            excluded_non_compliant.append(result)
            continue

        passed.append(result)

    passed.sort(key=lambda r: r["consensus_score"], reverse=True)
    ranked = passed[:top_k]

    logger.info(
        "consensus screen done: %d passed compliance, %d excluded non-compliant, "
        "%d skipped; returning top %d",
        len(passed), len(excluded_non_compliant), len(skipped), len(ranked),
    )
    return {
        "ranked": ranked,
        "excluded_non_compliant": excluded_non_compliant,
        "skipped": skipped,
        "total": total,
    }


def screen_and_rank(
    tickers: List[str],
    as_of_date: Optional[str] = None,
    top_k: int = 25,
) -> List[Dict[str, Any]]:
    """Rank a candidate list of tickers by LLM-free consensus score, after a
    Sharia-compliance hard filter.

    Args:
        tickers: candidate tickers (e.g. the output of Phase 1d's
            `screen_universe`, mapped to the `ticker` field). Duplicates and
            empty entries are ignored.
        as_of_date: passed straight through to `build_seed_from_ticker`. None =
            live. (Price is lookahead-safe; fundamentals are NOT point-in-time -
            see the data-layer notes.)
        top_k: max number of results to return. Fewer are returned if fewer
            tickers clear the compliance filter.

    Returns:
        A list of `compute_consensus` dicts, compliance-passing only, sorted by
        `consensus_score` descending, truncated to `top_k`. Non-compliant
        tickers are ABSENT from this list (not sorted to the bottom). Tickers
        that could not be scored at all are skipped with a logged reason.
    """
    return _run_consensus_screen(tickers, as_of_date, top_k)["ranked"]


def _fmt_score(x: float) -> str:
    return f"{x:+.3f}"


if __name__ == "__main__":
    # Live demo (hits yfinance, still zero LLM calls):
    #   cd backend && python -m app.services.consensus_screener
    EXAMPLE_TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "GS", "KO", "XOM", "WMT", "PFE"]

    t0 = time.monotonic()
    outcome = _run_consensus_screen(EXAMPLE_TICKERS, as_of_date=None, top_k=25)
    elapsed = time.monotonic() - t0

    ranked = outcome["ranked"]
    excluded = outcome["excluded_non_compliant"]
    skipped = outcome["skipped"]

    print("\n" + "=" * 92)
    print("Phase 5b — consensus screen + compliance hard filter (NO LLM)")
    print(f"  input tickers : {EXAMPLE_TICKERS}")
    print(f"  elapsed       : {elapsed:.1f}s")
    print("=" * 92)

    hdr = f"  {'#':>2}  {'TICKER':<7} {'CONSENSUS':>10}  " + "  ".join(f"{k[:4].upper():>6}" for k in _INVESTOR_KEYS)
    print("\nRanked (compliance-passing only, best first):")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for rank, r in enumerate(ranked, start=1):
        cells = "  ".join(_fmt_score(r["per_archetype_scores"][k]).rjust(6) for k in _INVESTOR_KEYS)
        print(f"  {rank:>2}  {r['ticker']:<7} {_fmt_score(r['consensus_score']):>10}  {cells}")

    if excluded:
        print("\nExcluded by the compliance hard filter (NOT in the ranked list):")
        for r in excluded:
            print(
                f"  {r['ticker']:<7} verdict={r['compliance_verdict']:<14} "
                f"consensus_score={_fmt_score(r['consensus_score'])} (would-have-ranked)  "
                f"reason: {r['compliance_reason']}"
            )

    if skipped:
        print("\nSkipped (could not score):")
        for s in skipped:
            print(f"  {s['ticker']:<7} {s['reason']}")
