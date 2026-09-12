"""
Phase 6a — tests for `PortfolioAgent` (Q&A over a computed portfolio dict).

Uses the SAME 9-ticker example as Phase 5b / 5e
(AAPL/MSFT/NVDA/JPM/GS/KO/XOM/WMT/PFE), where JPM & GS are filtered out as
non-compliant (conventional finance) and PFE is dominated -> ~0 weight.

Offline (pytest): `seed_builder.get_stock_context` + `portfolio_optimizer.get_price_data`
are patched with the Phase 5b / 5e fixtures, exactly as `test_portfolio_optimizer`
does, and `build_portfolio()` is run once to produce a real portfolio dict. The
deterministic fact-extraction path and the no-LLM "not found" path are asserted
directly. `PortfolioAgent.answer()`'s LLM phrasing step is exercised with a
capture stub.

Live LLM transcript: `cd backend && python tests/test_portfolio_agent.py`
(needs LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_NAME, same as the other phases'
manual prints). Builds the portfolio from the offline fixtures, then asks the
real model each question and prints the Q&A transcript.
"""

import copy

import pytest

from app.services import portfolio_optimizer, seed_builder
from app.services.portfolio_agent import (
    PortfolioAgent,
    PortfolioAgentError,
    _find_ticker,
    _known_tickers,
)
from app.services.portfolio_optimizer import build_portfolio

from tests.test_consensus_screener import _CONTEXTS
from tests.test_portfolio_optimizer import _CANDIDATES, _PRICE_OHLCV

_AS_OF = "2024-06-01"
_MAX_WEIGHT = 0.20

# `_derive_sharia_verdict` builds this from JPM's real `exclusion_reason` fixture:
# overall_compliant=False + sector_exclusion_flag=True + a reason string ->
# rationale = f"Excluded on business activity: {reason}".
_JPM_COMPLIANCE_REASON_VERBATIM = (
    "Excluded on business activity: conventional (interest-based) financials — "
    "banking / insurance / lending (yfinance industry: 'Banks - Diversified')"
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _fake_get_price_data(ticker, as_of_date=None):
    t = (ticker or "").strip().upper()
    if t not in _PRICE_OHLCV:
        return {"ticker": t, "success": False, "error": "no synthetic series", "ohlcv": None}
    return {"ticker": t, "success": True, "error": None, "ohlcv": copy.deepcopy(_PRICE_OHLCV[t])}


@pytest.fixture
def portfolio(monkeypatch):
    """A real `build_portfolio()` dict from the offline Phase 5b/5e fixtures."""
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(_CONTEXTS[ticker.strip().upper()]),
    )
    monkeypatch.setattr(portfolio_optimizer, "get_price_data", _fake_get_price_data)
    monkeypatch.setattr(
        portfolio_optimizer, "fetch_price_data",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetch_price_data hit unexpectedly")),
    )
    return build_portfolio(_CANDIDATES, as_of_date=_AS_OF, top_k=25, max_weight=_MAX_WEIGHT)


class _CaptureLLM:
    """Stub LLM: records the messages it was handed, returns a canned reply."""

    def __init__(self, reply="STUB ANSWER"):
        self.reply = reply
        self.calls = []

    def chat(self, messages, temperature=0.7, max_tokens=None, response_format=None):
        self.calls.append(messages)
        return self.reply


# --------------------------------------------------------------------------- #
# build_portfolio now carries the screening breakdown (Phase 6a addition)
# --------------------------------------------------------------------------- #
def test_build_portfolio_exposes_screening_block(portfolio):
    scr = portfolio["screening"]
    excluded = {e["ticker"]: e for e in scr["excluded_non_compliant"]}
    assert set(excluded) == {"JPM", "GS"}
    assert excluded["JPM"]["compliance_verdict"] == "non_compliant"
    assert excluded["JPM"]["compliance_reason"] == _JPM_COMPLIANCE_REASON_VERBATIM
    assert isinstance(excluded["JPM"]["consensus_score"], float)
    assert excluded["JPM"]["passes_compliance"] is False
    # JPM / GS appear NOWHERE in the optimized book
    assert "JPM" not in portfolio["weights"] and "GS" not in portfolio["weights"]
    assert not any(h["ticker"] in {"JPM", "GS"} for h in portfolio["holdings"])


# --------------------------------------------------------------------------- #
# ticker resolution
# --------------------------------------------------------------------------- #
def test_known_and_unknown_ticker_resolution(portfolio):
    known = _known_tickers(portfolio)
    assert {"AAPL", "MSFT", "JPM", "GS", "PFE"} <= set(known)

    assert _find_ticker("why is JPM not in the portfolio", known) == {"ticker": "JPM", "known": True}
    assert _find_ticker("why does MSFT have 20% weight", known) == {"ticker": "MSFT", "known": True}
    assert _find_ticker("what's the portfolio's Sharpe ratio", known) == {"ticker": None, "known": False}
    assert _find_ticker("why is BOGUS in the portfolio", known) == {"ticker": "BOGUS", "known": False}


# --------------------------------------------------------------------------- #
# category 1 — why is <ticker> in / not in the portfolio
# --------------------------------------------------------------------------- #
def test_facts_why_holding_is_in(portfolio):
    agent = PortfolioAgent()
    facts = agent.extract_facts("Why is AAPL in the portfolio?", portfolio)
    s = facts["subject"]
    assert s["kind"] in {"holding", "held_zero_weight"}
    assert s["ticker"] == "AAPL"
    assert isinstance(s["consensus_score"], float)
    assert s["consensus_rank"] >= 1
    assert s["compliance_verdict"] == "compliant"
    assert s["in_optimization"] is True


def test_facts_why_ticker_not_in_portfolio(portfolio):
    agent = PortfolioAgent()
    facts = agent.extract_facts("Why is JPM not in the portfolio?", portfolio)
    s = facts["subject"]
    assert s["kind"] == "excluded_non_compliant"
    assert s["compliance_verdict"] == "non_compliant"
    assert isinstance(s["consensus_score"], float)          # actual number, for context
    # it is NOT in holdings/weights
    assert "JPM" not in facts["final_weights"]


# --------------------------------------------------------------------------- #
# category 2 — why does <ticker> have X% weight  (weight != consensus score)
# --------------------------------------------------------------------------- #
def test_facts_weight_question_carries_methodology_and_peers(portfolio):
    agent = PortfolioAgent()
    facts = agent.extract_facts("Why does MSFT have a 20% weight?", portfolio)
    s = facts["subject"]
    assert s["kind"] == "holding"
    assert 0 < s["weight"] <= _MAX_WEIGHT + 1e-6
    assert s["weight_position"] is not None and s["n_positions"] >= 1
    assert s["largest_position"] is not None
    assert s["mean_position_weight"] is not None
    # the distinction that must never be conflated:
    assert "NOT an input" in facts["weight_methodology"]
    assert "consensus_score" in facts["weight_methodology"]
    # consensus score is present but clearly separate from weight
    assert isinstance(s["consensus_score"], float)


def test_facts_capped_weight_is_flagged(portfolio):
    """At least one Phase 5e holding sits exactly on the max_weight cap."""
    agent = PortfolioAgent()
    capped = []
    for h in portfolio["holdings"]:
        if not h["in_optimization"]:
            continue
        s = agent.extract_facts(f"why does {h['ticker']} have that weight", portfolio)["subject"]
        if s["at_max_weight_cap"]:
            capped.append(h["ticker"])
            assert abs(s["weight"] - _MAX_WEIGHT) < 1e-4
    assert capped, "expected >=1 holding pinned at the 20% cap in the 5e example"


def test_facts_dominated_name_is_zero_weight(portfolio):
    agent = PortfolioAgent()
    s = agent.extract_facts("Why does PFE have such a low weight?", portfolio)["subject"]
    assert s["kind"] == "held_zero_weight"
    assert s["weight"] == 0.0
    assert s["in_optimization"] is True          # it WAS optimized, just zeroed
    # must steer the LLM away from blaming the consensus score for the zero weight
    assert "consensus_score" in s["zero_weight_caveat"]
    assert "NOT" in s["zero_weight_caveat"]


# --------------------------------------------------------------------------- #
# category 3 — why is <ticker> excluded for compliance  (VERBATIM reason)
# --------------------------------------------------------------------------- #
def test_facts_compliance_reason_is_verbatim(portfolio):
    agent = PortfolioAgent()
    facts = agent.extract_facts("Why is JPM excluded for compliance?", portfolio)
    s = facts["subject"]
    assert s["kind"] == "excluded_non_compliant"
    assert s["compliance_reason"] == _JPM_COMPLIANCE_REASON_VERBATIM
    # also surfaced in the portfolio-wide list, identically
    jpm_row = next(e for e in facts["compliance_excluded"] if e["ticker"] == "JPM")
    assert jpm_row["compliance_reason"] == _JPM_COMPLIANCE_REASON_VERBATIM


def test_facts_second_excluded_name(portfolio):
    agent = PortfolioAgent()
    s = agent.extract_facts("why is GS excluded", portfolio)["subject"]
    assert s["kind"] == "excluded_non_compliant"
    assert "conventional (interest-based) financials" in s["compliance_reason"]
    assert "Capital Markets" in s["compliance_reason"]


# --------------------------------------------------------------------------- #
# category 4 — general portfolio metrics (read straight from the dict)
# --------------------------------------------------------------------------- #
def test_facts_portfolio_metrics_match_dict(portfolio):
    agent = PortfolioAgent()
    facts = agent.extract_facts("What's the portfolio's expected return and Sharpe ratio?", portfolio)
    assert facts["subject"]["kind"] == "portfolio_level"
    m = facts["portfolio_metrics"]
    p = portfolio["portfolio"]
    assert m["expected_return"] == p["expected_return"]
    assert m["volatility"] == p["volatility"]
    assert m["sharpe_ratio"] == p["sharpe_ratio"]
    assert m["risk_free_rate"] == p["risk_free_rate"]


# --------------------------------------------------------------------------- #
# not-found handling — deterministic, no LLM
# --------------------------------------------------------------------------- #
def test_nonsense_ticker_is_graceful_and_llm_free(portfolio):
    # a landmine client: if answer() calls the LLM for a not-found ticker, fail loud
    class _Boom:
        def chat(self, *a, **k):
            raise AssertionError("LLM must not be called for a not-found ticker")

    agent = PortfolioAgent(llm_client=_Boom())
    out = agent.answer("Why is ZZZZ in the portfolio?", portfolio)
    assert "ZZZZ" in out
    assert "does not appear anywhere in this portfolio" in out
    assert "JPM" in out and "AAPL" in out          # lists the tickers it CAN explain


def test_agent_never_recomputes(portfolio, monkeypatch):
    """extract_facts / not-found answer must not touch any Phase 5 computation."""
    for name in ("screen_and_rank", "optimize_portfolio", "build_portfolio", "_run_consensus_screen"):
        monkeypatch.setattr(
            portfolio_optimizer, name,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"{name} must not run")),
            raising=False,
        )
    agent = PortfolioAgent(llm_client=_CaptureLLM())
    agent.extract_facts("why is MSFT weighted that way", portfolio)
    agent.answer("why is NOPE here", portfolio)     # not-found, no llm


# --------------------------------------------------------------------------- #
# answer() wiring: facts -> LLM -> text
# --------------------------------------------------------------------------- #
def test_answer_hands_facts_to_llm(portfolio):
    llm = _CaptureLLM(reply="MSFT is capped at the 20% per-name limit.")
    agent = PortfolioAgent(llm_client=llm)
    out = agent.answer("Why does MSFT have a 20% weight?", portfolio)
    assert out == "MSFT is capped at the 20% per-name limit."

    (msgs,) = llm.calls
    assert msgs[0]["role"] == "system" and "PortfolioAgent" in msgs[0]["content"]
    user = msgs[1]["content"]
    assert "Why does MSFT have a 20% weight?" in user
    assert '"weight_methodology"' in user           # facts JSON was serialized in
    assert '"subject"' in user


def test_answer_raises_wrapped_on_llm_failure(portfolio):
    class _AlwaysFails:
        def chat(self, *a, **k):
            raise RuntimeError("provider down")

    agent = PortfolioAgent(llm_client=_AlwaysFails())
    with pytest.raises(PortfolioAgentError):
        agent.answer("what is the Sharpe ratio", portfolio)


# =========================================================================== #
# Manual live-LLM transcript
# =========================================================================== #
if __name__ == "__main__":
    #   cd backend && python tests/test_portfolio_agent.py
    import json
    import os

    import app.services.portfolio_optimizer as _po
    import app.services.seed_builder as _sb

    _orig_ctx, _orig_price = _sb.get_stock_context, _po.get_price_data
    _sb.get_stock_context = lambda ticker, as_of_date=None: copy.deepcopy(_CONTEXTS[ticker.strip().upper()])
    _po.get_price_data = _fake_get_price_data
    try:
        pf = build_portfolio(_CANDIDATES, as_of_date=_AS_OF, top_k=25, max_weight=_MAX_WEIGHT)
    finally:
        _sb.get_stock_context, _po.get_price_data = _orig_ctx, _orig_price

    print("\n" + "=" * 94)
    print(f"PORTFOLIO under test — build_portfolio({_CANDIDATES}) @ {_AS_OF}")
    print("=" * 94)
    print(f"  {'TICKER':<7} {'WEIGHT':>9}  {'CONSENSUS':>10}  {'RANK':>4}  {'VERDICT':<14}  IN-OPT")
    print("  " + "-" * 64)
    for h in pf["holdings"]:
        print(f"  {h['ticker']:<7} {h['weight'] * 100:>8.2f}%  {h['consensus_score']:>+10.3f}  "
              f"{h['consensus_rank']:>4}  {h['compliance_verdict']:<14}  "
              f"{'yes' if h['in_optimization'] else 'no'}")
    for e in pf["screening"]["excluded_non_compliant"]:
        print(f"  {e['ticker']:<7} {'—':>9}  {e['consensus_score']:>+10.3f}  {'—':>4}  "
              f"{e['compliance_verdict']:<14}  EXCLUDED (hard filter)")
    p = pf["portfolio"]
    print(f"\n  expected return {p['expected_return']:+.2%} | volatility {p['volatility']:+.2%} | "
          f"Sharpe {p['sharpe_ratio']:.3f} (rf {p['risk_free_rate']:.2%}), annualised")

    QUESTIONS = [
        "Why is JPM not in the portfolio?",
        "Why is JPM excluded for compliance?",
        "Why does MSFT have a 20% weight?",
        "Why is PFE in the portfolio but at essentially zero weight?",
        "What are the portfolio's expected return, volatility and Sharpe ratio?",
        "Why is BOGUS not in the portfolio?",
    ]

    agent = PortfolioAgent()

    llm_ready = bool(os.environ.get("LLM_API_KEY"))
    if not llm_ready:
        print("\n[!] LLM_API_KEY not set — printing the deterministic FACT BUNDLE for each\n"
              "    question only (this is exactly what would be handed to the model).\n")

    for i, q in enumerate(QUESTIONS, start=1):
        print("\n" + "#" * 94)
        print(f"# Q{i}. {q}")
        print("#" * 94)
        facts = agent.extract_facts(q, pf)
        print("\n-- derived facts (subject) --")
        print(json.dumps(facts["subject"], indent=2, default=str, ensure_ascii=False))
        if facts["subject"].get("kind") == "not_found" or llm_ready:
            print("\n-- ANSWER --")
            try:
                print(agent.answer(q, pf))
            except Exception as exc:  # noqa: BLE001
                print(f"[answer failed: {exc}]")
