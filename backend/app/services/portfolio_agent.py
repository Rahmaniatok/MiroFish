"""
Phase 6a — PortfolioAgent: natural-language Q&A over an already-computed portfolio.

Phase 5e's `build_portfolio()` produces the final artefact: screened + ranked
candidates, skfolio mean-variance weights, per-holding consensus/compliance
metadata, and (Phase 6a addition) the compliance-excluded / skipped breakdown.
This module answers plain-English questions about *that dict* — "why is JPM not
in the portfolio?", "why does MSFT have 20% weight?", "what's the Sharpe ratio?"
— WITHOUT re-running any Phase 5 computation. It reads the dict and explains it.

Design — "derive before LLM" (same discipline as Phase 3a / 3d / 5a):

  1. `extract_facts(question, portfolio)` pulls every relevant number out of the
     portfolio dict DETERMINISTICALLY: which bucket the ticker is in
     (holding / held-zero-weight / dropped-from-optimization / compliance-excluded
     / skipped / absent), its weight vs. the per-name cap and vs. its peers, its
     consensus score & rank, the VERBATIM compliance reason, and the
     portfolio-level metrics.
  2. Those facts — and ONLY those facts — are handed to an LLM to phrase. The
     system prompt forbids introducing any number not present in the facts blob.
  A ticker that appears nowhere in the portfolio dict short-circuits to a
  deterministic "not found" answer with NO LLM call.

Why MiroFish's original interview / ReportAgent path is not reused (Phase 6a
investigation): `oasis.social_agent.SocialAgent.perform_interview` reads
`self.memory.get_context()` off a live camel `ChatAgent` inside a running OASIS
env; our personas are plain dataclasses with no such memory, and there is no env.
`ReportAgent.chat` is a ReAct loop bound to Zep graph-search tools over a
simulation graph we no longer build. Both are unusable here — same conclusion as
Phase 4a re: Zep. Only the "derive facts, then let the LLM phrase" pattern
carries over.

Placement: `app/services/`, alongside `portfolio_optimizer` / `consensus_screener`
— this orchestrates on top of the Phase 5 output, it is not data-layer logic.

Phase 6b adds `get_debate()`: an explicit, separate, expensive escape hatch to
Phase 4's multi-round debate room for ONE ticker, on demand. Design decision
(confirmed before implementing): debate is presentation-only narrative, never
required for the allocation decision itself, so it must never run implicitly.
Concretely that means two guarantees, both enforced here rather than just
documented:
  1. `answer()` NEVER calls `run_debate()`. A question that reads as "show me
     the debate" is recognized by a cheap, deterministic keyword check
     (`_wants_debate`) and routed to a plain string telling the caller to call
     `get_debate()` themselves — no LLM call decides this, and no debate runs
     on that path.
  2. `get_debate(ticker, portfolio, ...)` is a distinct public method a caller
     must invoke on purpose. It reuses Phase 6a's ticker classification
     (`_subject_facts`) so "ticker not in this portfolio" behaves identically
     to `answer()`, and reuses the portfolio's own `as_of_date` so the debate
     can never drift to a different point in time than the portfolio it is
     explaining.
"""

import json
import re
from statistics import fmean
from typing import Any, Dict, List, Optional

from ..utils.logger import get_logger
from .debate_room import DEFAULT_DEBATE_ROUNDS, DebateTranscript, run_debate
from .seed_builder import build_seed_from_ticker

logger = get_logger("mirofish.portfolio_agent")

# Cheap, deterministic signal that a question wants the FULL multi-round agent
# debate rather than a quick fact lookup. Substring match on the lower-cased
# question — no LLM call decides this, so a wrong route never burns a debate.
_DEBATE_INTENT_KEYWORDS = (
    "debate", "discuss", "discussion", "deliberat", "argue", "argument",
    "back and forth", "round by round", "round-by-round", "full transcript",
    "what did the agents", "what did the investors", "what did the personas",
)


def _wants_debate(question: str) -> bool:
    q = (question or "").lower()
    return any(kw in q for kw in _DEBATE_INTENT_KEYWORDS)

# A weight at or below this is a true zero (the optimizer leaves dust; Phase 5e
# already filters `weights` to |w| > 1e-5, but `holdings[].weight` can be 0.0).
_ZERO_WEIGHT_EPS = 1e-9

# All-caps tokens that look like tickers but are finance / English noise. Used
# only when the question contains an unknown all-caps token (candidate "not
# found" ticker) — known tickers are matched directly against the portfolio.
_TICKER_STOPWORDS = frozenset({
    "A", "I", "AN", "AND", "OR", "THE", "US", "USD", "AI", "LLM", "ETF", "IPO",
    "CEO", "CFO", "GDP", "PE", "PB", "ROE", "ROA", "EPS", "YOY", "YTD", "NAV",
    "OK", "VS", "FAQ", "AAOIFI", "ESG", "P", "E", "B",
})

# What actually sets the weights — the single most important thing the agent must
# not conflate (Phase 5e "Option A": consensus score is NOT an optimizer input).
_WEIGHT_METHODOLOGY = (
    "Each holding's weight is set by skfolio's mean-variance (max-Sharpe) "
    "optimizer running on real historical returns, subject to long-only, "
    "fully-invested, and a per-name cap (max_weight). The consensus_score is NOT "
    "an input to that optimization — it only decided which tickers became "
    "candidates and their ranking for the top-K cut. A high consensus score does "
    "not imply a high weight; a weight sitting exactly at max_weight usually "
    "means the optimizer wanted even more of that name and was capped."
)

_SYSTEM_PROMPT = """You are PortfolioAgent. You explain ONE already-computed \
investment portfolio to a user. It was produced by this fixed pipeline, which \
you must NOT re-run or second-guess:
  1. Consensus screen — 5 investor archetypes score each candidate ticker \
(LLM-free); this yields consensus_score and a rank.
  2. Sharia-compliance HARD FILTER — a non-compliant ticker is removed entirely \
here, regardless of how high its consensus score is.
  3. skfolio mean-variance optimization (max-Sharpe, Ledoit-Wolf covariance) on \
real historical returns — this, and only this, sets each holding's weight.

Rules:
- Answer using ONLY the FACTS JSON supplied with the question. Every number, \
ticker, weight, score, rank and reason you state MUST come from FACTS. Never \
estimate, extrapolate, or use outside/general knowledge about these companies \
or markets.
- If FACTS does not contain what is needed, say so plainly instead of guessing.
- consensus_score does NOT determine weight. If the question assumes it does, \
correct that using facts.weight_methodology. Whenever you discuss a weight, make \
the distinction explicit.
- A holding with ~0% weight was NOT rejected for a low consensus_score or rank — \
consensus_score never reaches the optimizer, so it cannot be the reason. If \
facts.subject carries a `zero_weight_caveat`, follow it exactly: attribute the \
zero weight to the optimizer's risk/return judgment and say plainly that the \
specific return/volatility numbers behind that judgment are not in FACTS — do \
not invent them, and do not substitute consensus_score or rank as the cause.
- When you give a compliance reason, quote facts.subject.compliance_reason (or \
the matching entry in facts.compliance_excluded) VERBATIM, in quotation marks. \
Do not paraphrase it into something vaguer.
- Look at facts.subject first — it names the bucket the ticker is in (holding, \
held_zero_weight, dropped_from_optimization, excluded_non_compliant, skipped).
- Be direct and concise: 2–6 sentences of plain prose. No headings. No bullet \
lists unless you are comparing several holdings.
- All return / volatility / Sharpe figures are annualised."""


class PortfolioAgentError(RuntimeError):
    """Raised when the LLM phrasing step fails after retries."""


# --------------------------------------------------------------------------- #
# Deterministic fact extraction (no LLM)
# --------------------------------------------------------------------------- #
def _pct(x: Optional[float], *, signed: bool = False) -> Optional[str]:
    if x is None:
        return None
    return f"{x * 100:+.2f}%" if signed else f"{x * 100:.2f}%"


def _known_tickers(portfolio: Dict[str, Any]) -> List[str]:
    """Every ticker that appears anywhere in the portfolio dict."""
    out: set = set()
    for h in portfolio.get("holdings") or []:
        if h.get("ticker"):
            out.add(str(h["ticker"]).upper())
    for t in portfolio.get("weights") or {}:
        out.add(str(t).upper())
    screening = portfolio.get("screening") or {}
    for e in screening.get("excluded_non_compliant") or []:
        if e.get("ticker"):
            out.add(str(e["ticker"]).upper())
    for e in screening.get("skipped") or []:
        if e.get("ticker"):
            out.add(str(e["ticker"]).upper())
    for d in portfolio.get("dropped_from_optimization") or []:
        if d.get("ticker"):
            out.add(str(d["ticker"]).upper())
    return sorted(out)


def _find_ticker(question: str, known: List[str]) -> Dict[str, Any]:
    """Identify the ticker a question is about.

    A known ticker (present in the portfolio dict) wins — earliest mention
    breaks ties, so the result is deterministic. Otherwise an all-caps token
    that looks like a symbol is returned as an *unknown* ticker (→ "not found").
    """
    up = question.upper()
    hits = [t for t in known if re.search(rf"\b{re.escape(t)}\b", up)]
    if hits:
        hits.sort(key=up.find)
        return {"ticker": hits[0], "known": True}
    for m in re.finditer(r"\b[A-Z]{2,5}\b", question):
        tok = m.group(0)
        if tok not in _TICKER_STOPWORDS:
            return {"ticker": tok, "known": False}
    return {"ticker": None, "known": False}


def _holding_subject(ticker: str, h: Dict[str, Any], portfolio: Dict[str, Any]) -> Dict[str, Any]:
    weights = {str(k).upper(): float(v) for k, v in (portfolio.get("weights") or {}).items()}
    max_weight = portfolio.get("max_weight")
    w = float(h.get("weight") or 0.0)
    in_opt = bool(h.get("in_optimization"))

    if not in_opt:
        kind = "dropped_from_optimization"
    elif w <= _ZERO_WEIGHT_EPS:
        kind = "held_zero_weight"
    else:
        kind = "holding"

    ranked_by_weight = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    position = next((i + 1 for i, (t, _) in enumerate(ranked_by_weight) if t == ticker), None)

    zero_weight_caveat = None
    if kind == "held_zero_weight":
        zero_weight_caveat = (
            f"{ticker} passed compliance and WAS fed into the optimizer, which then "
            "assigned it ~0% weight. That is a mean-variance efficiency judgment "
            "based on its historical return/volatility/correlation profile — data "
            "not present in these facts. It is NOT because of a low consensus_score "
            "or consensus_rank; consensus_score never reaches the optimizer. State "
            "plainly that the optimizer judged it an inefficient risk/return "
            "trade-off relative to the other names and that the underlying "
            "return/volatility figures are not available here — do not attribute "
            "the zero weight to its consensus score or rank."
        )

    return {
        "kind": kind,
        "zero_weight_caveat": zero_weight_caveat,
        "ticker": ticker,
        "weight": round(w, 6),
        "weight_pct": _pct(w),
        "consensus_score": h.get("consensus_score"),
        "consensus_rank": h.get("consensus_rank"),
        "n_ranked": portfolio.get("n_ranked"),
        "passes_compliance": h.get("passes_compliance"),
        "compliance_verdict": h.get("compliance_verdict"),
        "in_optimization": in_opt,
        "dropped_reason": h.get("dropped_reason"),
        "max_weight_cap": max_weight,
        "at_max_weight_cap": (
            max_weight is not None and w > _ZERO_WEIGHT_EPS
            and abs(w - float(max_weight)) < 1e-4
        ),
        "weight_position": position,          # 1 = largest position in the book
        "n_positions": len(weights),
        "largest_position": (
            {"ticker": ranked_by_weight[0][0], "weight": round(ranked_by_weight[0][1], 6)}
            if ranked_by_weight else None
        ),
        "smallest_nonzero_position": (
            {"ticker": ranked_by_weight[-1][0], "weight": round(ranked_by_weight[-1][1], 6)}
            if ranked_by_weight else None
        ),
        "mean_position_weight": round(fmean(weights.values()), 6) if weights else None,
    }


def _subject_facts(ticker_info: Dict[str, Any], portfolio: Dict[str, Any]) -> Dict[str, Any]:
    ticker = ticker_info["ticker"]
    if ticker is None:
        return {"kind": "portfolio_level"}

    holdings = {str(h.get("ticker", "")).upper(): h for h in (portfolio.get("holdings") or [])}
    if ticker in holdings:
        return _holding_subject(ticker, holdings[ticker], portfolio)

    screening = portfolio.get("screening") or {}
    for e in screening.get("excluded_non_compliant") or []:
        if str(e.get("ticker", "")).upper() == ticker:
            return {
                "kind": "excluded_non_compliant",
                "ticker": ticker,
                "consensus_score": e.get("consensus_score"),
                "per_archetype_scores": e.get("per_archetype_scores"),
                "per_archetype_stances": e.get("per_archetype_stances"),
                "compliance_verdict": e.get("compliance_verdict"),
                "compliance_reason": e.get("compliance_reason"),
                "note": (
                    "Removed by the Sharia-compliance HARD FILTER before the "
                    "optimization step. The consensus score is recorded only to "
                    "show the exclusion is a hard filter, not a low ranking — a "
                    "non-compliant name is dropped no matter how high it scores."
                ),
            }
    for e in screening.get("skipped") or []:
        if str(e.get("ticker", "")).upper() == ticker:
            return {
                "kind": "skipped",
                "ticker": ticker,
                "reason": e.get("reason"),
                "note": (
                    "Could not be scored at all (e.g. no market data / unresolvable "
                    "symbol); it never reached the compliance screen or the optimizer."
                ),
            }

    return {"kind": "not_found", "ticker": ticker}


# --------------------------------------------------------------------------- #
# PortfolioAgent
# --------------------------------------------------------------------------- #
class PortfolioAgent:
    """Answers natural-language questions about a Phase 5e portfolio dict.

    Two clearly separate API-level paths:
      - `answer()` — CHEAP. Read-only: never calls `screen_and_rank`,
        `optimize_portfolio`, `build_portfolio`, or `run_debate`. Extracts
        facts from the dict deterministically and uses an LLM only to phrase
        the answer (or a fixed string for "not found" / debate-routing).
      - `get_debate()` — EXPENSIVE. A distinct, explicit call that runs a fresh
        Phase 4 multi-round debate for one ticker. Never invoked by `answer()`.
    """

    def __init__(
        self,
        llm_client: Optional[Any] = None,
        *,
        model_name: Optional[str] = None,
    ):
        """
        Args:
            llm_client: anything with `.chat(messages, temperature=, max_tokens=)`
                returning a string (e.g. `app.utils.llm_client.LLMClient`). If
                omitted, an `LLMClient` is built lazily on the first `answer()`
                that needs it — a "not found" answer needs no LLM at all.
            model_name: optional model override passed to the lazily-built client.
        """
        self._llm = llm_client
        self._model_name = model_name

    # -- public API -------------------------------------------------------- #
    def extract_facts(self, question: str, portfolio: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic fact bundle for `question` against `portfolio`.

        This is the "derive" half of the derive-before-LLM pattern. It never
        calls an LLM and never mutates `portfolio`. Exposed so tests (and
        callers who want the raw grounding) can inspect exactly what the LLM is
        given.
        """
        known = _known_tickers(portfolio)
        ticker_info = _find_ticker(question, known)
        subject = _subject_facts(ticker_info, portfolio)

        pf = portfolio.get("portfolio") or {}
        screening = portfolio.get("screening") or {}

        return {
            "question": question,
            "as_of_date": portfolio.get("as_of_date"),
            "run_params": {
                "top_k": portfolio.get("top_k"),
                "max_weight": portfolio.get("max_weight"),
                "n_candidates": portfolio.get("n_candidates"),
                "n_ranked": portfolio.get("n_ranked"),
                "lookback": pf.get("lookback"),
                "objective": pf.get("objective"),
                "n_assets_optimized": pf.get("n_assets_optimized"),
            },
            "portfolio_metrics": {
                "expected_return": pf.get("expected_return"),
                "expected_return_pct": _pct(pf.get("expected_return"), signed=True),
                "volatility": pf.get("volatility"),
                "volatility_pct": _pct(pf.get("volatility")),
                "sharpe_ratio": pf.get("sharpe_ratio"),
                "risk_free_rate": pf.get("risk_free_rate"),
                "risk_free_rate_pct": _pct(pf.get("risk_free_rate")),
                "annualised": True,
            },
            "final_weights": {
                str(t).upper(): round(float(w), 6)
                for t, w in (portfolio.get("weights") or {}).items()
            },
            "holdings": [
                {
                    "ticker": h.get("ticker"),
                    "weight": round(float(h.get("weight") or 0.0), 6),
                    "weight_pct": _pct(float(h.get("weight") or 0.0)),
                    "consensus_score": h.get("consensus_score"),
                    "consensus_rank": h.get("consensus_rank"),
                    "compliance_verdict": h.get("compliance_verdict"),
                    "in_optimization": h.get("in_optimization"),
                    "dropped_reason": h.get("dropped_reason"),
                }
                for h in (portfolio.get("holdings") or [])
            ],
            "compliance_excluded": [
                {
                    "ticker": e.get("ticker"),
                    "compliance_verdict": e.get("compliance_verdict"),
                    "compliance_reason": e.get("compliance_reason"),
                    "consensus_score": e.get("consensus_score"),
                }
                for e in screening.get("excluded_non_compliant") or []
            ],
            "skipped": [dict(s) for s in screening.get("skipped") or []],
            "dropped_from_optimization": [
                dict(d) for d in portfolio.get("dropped_from_optimization") or []
            ],
            "weight_methodology": _WEIGHT_METHODOLOGY,
            "known_tickers": known,
            "subject": subject,
        }

    def answer(self, question: str, portfolio: Dict[str, Any]) -> str:
        """Answer `question` grounded in `portfolio` (a Phase 5e `build_portfolio`
        dict).

        This is the CHEAP path and never runs a debate: a question that reads as
        a request for the full multi-round agent debate is routed to a plain
        string pointing at `get_debate()` (see `_wants_debate` — a keyword
        check, not an LLM call). A question about a ticker that appears nowhere
        in the dict returns a deterministic "not found" message with no LLM
        call. Everything else extracts facts deterministically, then asks the
        LLM to phrase them.
        """
        facts = self.extract_facts(question, portfolio)
        subject = facts["subject"]

        if _wants_debate(question):
            return self._debate_routing_message(subject, facts)

        if subject.get("kind") == "not_found":
            return self._not_found_answer(subject["ticker"], facts)

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"QUESTION:\n{question}\n\n"
                    "FACTS (the only ground truth you may cite; do not add numbers "
                    "that are not here):\n"
                    f"{json.dumps(facts, indent=2, default=str, ensure_ascii=False)}"
                ),
            },
        ]
        return self._phrase_with_llm(messages)

    def get_debate(
        self,
        ticker: str,
        portfolio: Dict[str, Any],
        rounds: int = DEFAULT_DEBATE_ROUNDS,
        *,
        use_llm: bool = True,
    ) -> DebateTranscript:
        """Run a FRESH Phase 4 multi-round debate for `ticker` on demand.

        This is the EXPENSIVE, explicit path (a multi-round LLM call per
        persona) — it is a distinct public method a caller must invoke on
        purpose, and `answer()` never calls it. Use it only after the caller
        has actually decided they want the debate (e.g. `answer()` routed them
        here via `_wants_debate`, or they asked for it directly).

        Args:
            ticker: any ticker present anywhere in `portfolio` — a holding,
                compliance-excluded, dropped-from-optimization, or skipped.
                Reuses Phase 6a's `_subject_facts` classification, so a ticker
                absent from the portfolio fails exactly like `answer()` would
                report it: clearly, and without guessing.
            portfolio: the Phase 5e `build_portfolio()` dict. Its `as_of_date`
                is reused for the seed build so the debate is guaranteed to be
                about the same point in time the portfolio was computed for —
                the caller cannot pass a different date by mistake because
                there is nowhere to pass one.
            rounds: debate rounds (default `debate_room.DEFAULT_DEBATE_ROUNDS`).
            use_llm: forwarded to `run_debate` (False = deterministic offline
                templates; used by tests, real callers want the default True).

        Returns:
            The full `DebateTranscript` (5 investors + Gamma, `rounds` rounds).

        Raises:
            PortfolioAgentError: `ticker` does not appear anywhere in
                `portfolio` (not a holding, not compliance-excluded, not
                skipped) — no seed is built and no debate is run.
        """
        ticker_norm = (ticker or "").strip().upper()
        subject = _subject_facts({"ticker": ticker_norm}, portfolio)
        if subject["kind"] == "not_found":
            known = _known_tickers(portfolio)
            raise PortfolioAgentError(
                f"cannot debate {ticker_norm!r}: it does not appear anywhere in "
                f"this portfolio result (not a holding, not compliance-excluded, "
                f"not skipped). Tickers available: "
                f"{', '.join(known) if known else 'none'}."
            )

        as_of_date = portfolio.get("as_of_date")
        seed = build_seed_from_ticker(ticker_norm, as_of_date)
        return run_debate(seed, rounds=rounds, use_llm=use_llm, model_name=self._model_name)

    # -- internals ------------------------------------------------------- #
    @staticmethod
    def _debate_routing_message(subject: Dict[str, Any], facts: Dict[str, Any]) -> str:
        """Deterministic (no LLM) routing reply for a debate-shaped question."""
        known = facts["known_tickers"]
        known_str = ", ".join(known) if known else "none"
        kind = subject.get("kind")
        ticker = subject.get("ticker")

        if kind in (None, "portfolio_level"):
            return (
                "That reads like a request for the full multi-round agent debate, "
                "not a quick fact lookup, but I can't tell which ticker you mean. "
                f"Call PortfolioAgent.get_debate(ticker, portfolio) with one of the "
                f"tickers in this portfolio: {known_str}."
            )
        if kind == "not_found":
            return (
                f"{ticker} does not appear anywhere in this portfolio result, so "
                f"there is no debate to run for it. Tickers available: {known_str}."
            )
        return (
            f"That's a request for the full 5-investor + Gamma debate transcript "
            f"on {ticker}, not a quick fact lookup. Call "
            f"PortfolioAgent.get_debate({ticker!r}, portfolio) to run it — that "
            f"triggers a fresh multi-round LLM debate and is intentionally kept "
            f"separate from this cheap Q&A path."
        )

    @staticmethod
    def _not_found_answer(ticker: str, facts: Dict[str, Any]) -> str:
        known = facts["known_tickers"]
        return (
            f"{ticker} does not appear anywhere in this portfolio result — it is "
            f"not one of the {len(facts['holdings'])} screened-in holdings, not in "
            f"the compliance-excluded list, and not in the skipped list. This agent "
            f"only explains tickers that were part of this portfolio computation "
            f"({', '.join(known) if known else 'none'})."
        )

    def _client(self) -> Any:
        if self._llm is None:
            from ..utils.llm_client import LLMClient

            self._llm = LLMClient(model=self._model_name)
        return self._llm

    def _phrase_with_llm(self, messages: List[Dict[str, str]]) -> str:
        client = self._client()
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                text = client.chat(messages, temperature=0.2, max_tokens=600)
                if text and text.strip():
                    return text.strip()
                last_error = ValueError("empty LLM response")
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised wrapped
                last_error = exc
                logger.warning(
                    "PortfolioAgent LLM phrasing attempt %d/3 failed: %s", attempt + 1, exc
                )
        raise PortfolioAgentError(
            f"could not generate an answer after 3 attempts: {last_error}"
        )
