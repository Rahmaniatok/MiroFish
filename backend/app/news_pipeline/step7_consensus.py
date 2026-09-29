"""
Step 7 — consensus from 06_report.json by plain persona votes.

One persona = one vote per ticker (no weighting, list position ignored).
Tickers are ranked by votes; the consensus is the top n. Ties at the cutoff
are ALL included — without weighting there is no fair tie-breaker — so the
consensus can hold more than n tickers (flagged as tie_extended). A ticker
also needs at least min_votes (default 2): one persona alone is not a
consensus, and on the healthcare run n=6 would otherwise have pulled in a
16-way tie of 1-vote tickers. If too few tickers reach min_votes the
consensus is smaller than n (flagged as fewer_than_n).

Why not "recommended by >= 3 of 8 personas" (the rule of the manual
healthcare prompt): on that run only UNH reached 3 votes, so the consensus
would have been a single ticker; top-5 gives UNH + the four 2-vote tickers.

Deterministic and instant, so it can be recomputed with another n at will.

    07_consensus.json  {n, cutoff_votes, tie_extended, consensus, ranking[...]}
"""

from datetime import datetime
from typing import Any, Dict, List

from . import run_store
from .step1_universe import ARTIFACT as UNIVERSE_ARTIFACT
from .step6_report_json import ARTIFACT as REPORT_JSON_ARTIFACT

STEP = "consensus"
ARTIFACT = "07_consensus.json"
DEFAULT_N = 5
DEFAULT_MIN_VOTES = 2


def compute(report_json: Dict[str, Any], universe: List[str], n: int,
            min_votes: int = DEFAULT_MIN_VOTES) -> Dict[str, Any]:
    """Pure vote count + top-n with ties at the cutoff included, floor at min_votes."""
    if n < 1:
        raise ValueError("n must be at least 1")
    if min_votes < 1:
        raise ValueError("min_votes must be at least 1")
    personas = [p["persona"] for p in report_json["personas"]]
    voters: Dict[str, List[str]] = {t: [] for t in universe}
    for p in report_json["personas"]:
        for t in dict.fromkeys(p["tickers"]):  # a persona votes once per ticker
            voters.setdefault(t, []).append(p["persona"])

    # votes desc; alphabetical only to make the display order stable (not a tie-breaker)
    order = sorted(voters, key=lambda t: (-len(voters[t]), t))
    voted = [t for t in order if voters[t]]
    cutoff = len(voters[voted[n - 1]]) if len(voted) >= n else (len(voters[voted[-1]]) if voted else 0)
    cutoff = max(cutoff, min_votes)
    selected = [t for t in voted if len(voters[t]) >= cutoff]

    ranking, rank, prev = [], 0, None
    for i, t in enumerate(order):
        votes = len(voters[t])
        if votes != prev:
            rank, prev = i + 1, votes   # competition ranking: 1, 2, 2, 2, 2, 6, ...
        ranking.append({"ticker": t, "votes": votes, "rank": rank, "personas": voters[t],
                        "selected": t in selected})
    return {
        "method": "persona votes (1 persona = 1 vote), top n, ties at the cutoff included, min votes floor",
        "n": n,
        "min_votes": min_votes,
        "personas": personas,
        "persona_count": len(personas),
        "cutoff_votes": cutoff,
        "consensus": selected,
        "tie_extended": len(selected) > n,
        "fewer_than_n": len(selected) < n,
        "ranking": ranking,
    }


def build(run_id: str, n: int = DEFAULT_N, min_votes: int = DEFAULT_MIN_VOTES) -> Dict[str, Any]:
    manifest = run_store.load_run(run_id)
    if manifest["steps"]["report_json"]["status"] != run_store.STEP_COMPLETED:
        raise ValueError("Step 04 (report → JSON) is not completed yet")
    report_json = run_store.read_artifact(run_id, REPORT_JSON_ARTIFACT)
    universe = run_store.read_artifact(run_id, UNIVERSE_ARTIFACT)["ticker_universe"]
    result = {**compute(report_json, universe, int(n), int(min_votes)),
              "generated_at": datetime.now().isoformat(timespec='seconds')}
    run_store.write_artifact(run_id, ARTIFACT, result)

    note = (f" (tie at {result['cutoff_votes']} votes → {len(result['consensus'])})" if result["tie_extended"]
            else f" (only {len(result['consensus'])} reach {min_votes}+ votes)" if result["fewer_than_n"] else "")
    run_store.append_log(run_id, "consensus_built",
                         f"Consensus top {n}{note}: {', '.join(result['consensus']) or '—'}")
    return run_store.update_step(run_id, STEP, run_store.STEP_COMPLETED, artifact=ARTIFACT, summary={
        "n": n, "min_votes": min_votes, "consensus": result["consensus"], "cutoff_votes": result["cutoff_votes"],
        "tie_extended": result["tie_extended"],
    })


def get(run_id: str) -> Dict[str, Any]:
    run_store.load_run(run_id)
    return {"result": run_store.read_artifact(run_id, ARTIFACT), "default_n": DEFAULT_N,
            "default_min_votes": DEFAULT_MIN_VOTES}
