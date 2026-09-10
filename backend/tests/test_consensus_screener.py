"""
Phase 5b — tes `compute_consensus` / `screen_and_rank`.

Menegakkan tiga hal:
  1. consensus_score = rata-rata sederhana 5 skor arketipe investor (Phase 5a),
     diturunkan langsung dari seed graph — TANPA LLM, TANPA debate room.
  2. verdict Gamma (Phase 3d) dipakai sebagai HARD FILTER: ticker non-compliant
     (mis. JPM, GS = conventional finance) HILANG total dari hasil ranking,
     bukan sekadar diurutkan ke bawah.
  3. NOL panggilan LLM di sepanjang jalur — dibuktikan dengan mem-patch
     `create_chat_completion` + `openai.OpenAI` supaya meledak kalau dipanggil.

Offline: `seed_builder.get_stock_context` di-patch dengan fixture dict bentuk
persis `get_stock_context()` (AAPL & JPM angka nyata @ 2024-06-01 di-reuse dari
test Phase 3d; sisanya sintetis tapi realistis). Tidak ada jaringan.
"""

import copy
import time

import pytest

from app.services import consensus_screener
from app.services import seed_builder
from app.services.consensus_screener import (
    _INVESTOR_KEYS,
    compute_consensus,
    screen_and_rank,
)
from tests.test_sharia_compliance_persona import STOCK_CONTEXT_AAPL, STOCK_CONTEXT_JPM


# --------------------------------------------------------------------------- #
# Fixture contexts — exact shape of get_stock_context()
# --------------------------------------------------------------------------- #
def _ctx(
    ticker,
    company_name,
    sector,
    industry,
    *,
    pe,
    pb,
    market_cap,
    rev_growth,
    eps_growth,
    profit_margin,
    roe,
    debt_to_equity,
    sma50,
    sma200,
    rsi,
    macd_hist,
    boll,
    sharia,
):
    """Build one synthetic get_stock_context() dict.

    `sharia` is "compliant" or "non_compliant"; the debt/activity fields are
    filled to match so `_derive_sharia_verdict` returns that verdict.
    """
    if sharia == "compliant":
        sharia_block = {
            "ticker": ticker, "success": True, "error": None,
            "standard": "AAOIFI Shari'ah Standard No. 21", "is_point_in_time": False,
            "debt_to_market_cap": 0.05, "debt_to_market_cap_threshold": 0.33,
            "passes_debt_screen": True,
            "sector": sector, "industry": industry,
            "sector_exclusion_flag": False, "excluded_category": None, "exclusion_reason": None,
            "overall_compliant": True,
            "unavailable_checks": {"impure_income_ratio": "not computable from yfinance"},
        }
    else:
        sharia_block = {
            "ticker": ticker, "success": True, "error": None,
            "standard": "AAOIFI Shari'ah Standard No. 21", "is_point_in_time": False,
            "debt_to_market_cap": 1.30, "debt_to_market_cap_threshold": 0.33,
            "passes_debt_screen": False,
            "sector": sector, "industry": industry,
            "sector_exclusion_flag": True,
            "excluded_category": "conventional_finance",
            "exclusion_reason": (
                f"conventional (interest-based) financials — banking / insurance / lending "
                f"(yfinance industry: {industry!r})"
            ),
            "overall_compliant": False,
            "unavailable_checks": {"impure_income_ratio": "not computable from yfinance"},
        }
    return {
        "ticker": ticker,
        "company_name": company_name,
        "as_of_date": "2024-06-01",
        "success": True,
        "price": {
            "ticker": ticker, "success": True, "error": None,
            "period": "1y", "as_of_date": "2024-06-01",
            "latest_close": 100.0, "latest_date": "2024-05-31",
            "stats": {"52w_high": 120.0, "52w_low": 80.0},
            "technical_indicators": {
                "price_vs_sma50": sma50, "price_vs_sma200": sma200, "rsi_14": rsi,
                "macd_signal": {"macd": 1.0, "signal": 1.0 - macd_hist, "histogram": macd_hist},
                "bollinger_position": boll, "volume_vs_avg": 1.1, "change_1d_pct": 0.3,
            },
        },
        "fundamental": {
            "ticker": ticker, "success": True, "error": None, "as_of_date": "2024-06-01",
            "is_point_in_time": False, "warning": "nilai terkini",
            "company_name": company_name,
            "pe_ratio": pe, "pb_ratio": pb, "market_cap": market_cap,
            "sector": sector, "industry": industry,
            "revenue_growth_yoy": rev_growth, "profit_margin": profit_margin,
            "roe": roe, "debt_to_equity": debt_to_equity,
            "eps_growth": eps_growth, "dividend_yield": 0.01, "skipped_fields": {},
        },
        "sharia_compliance": sharia_block,
    }


_CONTEXTS = {
    "AAPL": copy.deepcopy(STOCK_CONTEXT_AAPL),   # real @ 2024-06-01 — compliant
    "JPM": copy.deepcopy(STOCK_CONTEXT_JPM),     # real @ 2024-06-01 — NON-compliant
    # strong quality + growth mega-cap tech -> should rank high
    "MSFT": _ctx("MSFT", "Microsoft Corp.", "Technology", "Software - Infrastructure",
                 pe=38.0, pb=13.0, market_cap=3.3e12, rev_growth=0.17, eps_growth=0.20,
                 profit_margin=0.36, roe=0.39, debt_to_equity=0.35,
                 sma50=6.0, sma200=15.0, rsi=63.0, macd_hist=0.4, boll=0.7,
                 sharia="compliant"),
    # blistering growth, very rich valuation, overbought tape
    "NVDA": _ctx("NVDA", "NVIDIA Corp.", "Technology", "Semiconductors",
                 pe=68.0, pb=60.0, market_cap=3.0e12, rev_growth=1.2, eps_growth=1.5,
                 profit_margin=0.55, roe=0.90, debt_to_equity=0.20,
                 sma50=12.0, sma200=45.0, rsi=78.0, macd_hist=0.9, boll=0.95,
                 sharia="compliant"),
    # conventional finance -> NON-compliant (2nd excluded name)
    "GS": _ctx("GS", "Goldman Sachs Group Inc.", "Financial Services", "Capital Markets",
               pe=13.0, pb=1.4, market_cap=1.5e11, rev_growth=0.10, eps_growth=0.12,
               profit_margin=0.24, roe=0.11, debt_to_equity=None,
               sma50=3.0, sma200=10.0, rsi=55.0, macd_hist=0.1, boll=0.55,
               sharia="non_compliant"),
    # defensive, fair-ish valuation, sluggish growth
    "KO": _ctx("KO", "Coca-Cola Co.", "Consumer Defensive", "Beverages - Non-Alcoholic",
               pe=24.0, pb=10.0, market_cap=2.6e11, rev_growth=0.03, eps_growth=0.05,
               profit_margin=0.23, roe=0.40, debt_to_equity=1.6,
               sma50=2.0, sma200=4.0, rsi=52.0, macd_hist=0.05, boll=0.5,
               sharia="compliant"),
    # cheap value, but shrinking topline and a broken tape
    "XOM": _ctx("XOM", "Exxon Mobil Corp.", "Energy", "Oil & Gas Integrated",
                pe=13.5, pb=2.0, market_cap=4.6e11, rev_growth=-0.04, eps_growth=-0.20,
                profit_margin=0.10, roe=0.16, debt_to_equity=0.20,
                sma50=-3.0, sma200=-1.0, rsi=42.0, macd_hist=-0.3, boll=0.25,
                sharia="compliant"),
    # middling on every axis
    "WMT": _ctx("WMT", "Walmart Inc.", "Consumer Defensive", "Discount Stores",
                pe=28.0, pb=7.5, market_cap=5.4e11, rev_growth=0.06, eps_growth=0.09,
                profit_margin=0.025, roe=0.20, debt_to_equity=0.75,
                sma50=4.0, sma200=9.0, rsi=61.0, macd_hist=0.2, boll=0.65,
                sharia="compliant"),
    # earnings going backwards -> should land near the bottom of the ranking
    "PFE": _ctx("PFE", "Pfizer Inc.", "Healthcare", "Drug Manufacturers - General",
                pe=75.0, pb=1.6, market_cap=1.6e11, rev_growth=-0.42, eps_growth=-0.75,
                profit_margin=0.06, roe=0.05, debt_to_equity=0.75,
                sma50=-2.0, sma200=-8.0, rsi=38.0, macd_hist=-0.15, boll=0.2,
                sharia="compliant"),
}

_TEST_TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "GS", "KO", "XOM", "WMT", "PFE"]
_NON_COMPLIANT = {"JPM", "GS"}


@pytest.fixture
def patched(monkeypatch):
    """Offline seed data + a trip-wire on every LLM entry point."""
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(_CONTEXTS[ticker.strip().upper()]),
    )

    calls = {"llm": 0}

    def _boom(*args, **kwargs):
        calls["llm"] += 1
        raise AssertionError(
            "an LLM call was attempted inside the Phase 5b consensus path — "
            "this path must be 100% deterministic / LLM-free"
        )

    # Patch every route to the model: the chat-completion helper the persona
    # path uses, and the OpenAI client constructor itself.
    import app.services.oasis_profile_generator as opg
    monkeypatch.setattr(opg, "create_chat_completion", _boom, raising=True)
    monkeypatch.setattr(opg, "OpenAI", _boom, raising=True)
    return calls


# --------------------------------------------------------------------------- #
# compute_consensus
# --------------------------------------------------------------------------- #
def test_compute_consensus_shape_and_mean(patched):
    r = compute_consensus("AAPL", as_of_date="2024-06-01")

    assert r["ticker"] == "AAPL"
    assert r["as_of_date"] == "2024-06-01"
    assert set(r) >= {
        "ticker", "as_of_date", "consensus_score", "per_archetype_scores",
        "per_archetype_stances", "passes_compliance", "compliance_verdict",
        "compliance_reason",
    }

    scores = r["per_archetype_scores"]
    assert set(scores) == set(_INVESTOR_KEYS)
    assert all(-1.0 <= v <= 1.0 for v in scores.values())

    # consensus_score is EXACTLY the unweighted mean of the 5 archetype scores
    assert r["consensus_score"] == pytest.approx(sum(scores.values()) / 5.0)
    assert -1.0 <= r["consensus_score"] <= 1.0

    # AAPL is compliant -> passes, reason is null
    assert r["passes_compliance"] is True
    assert r["compliance_verdict"] == "compliant"
    assert r["compliance_reason"] is None


def test_compute_consensus_marks_non_compliant(patched):
    r = compute_consensus("JPM", as_of_date="2024-06-01")
    assert r["passes_compliance"] is False
    assert r["compliance_verdict"] == "non_compliant"
    assert r["compliance_reason"]                       # non-null rationale
    assert "conventional" in r["compliance_reason"].lower()
    # ...but the consensus score is still computed (for transparency)
    assert isinstance(r["consensus_score"], float)


def test_compute_consensus_is_deterministic(patched):
    a = compute_consensus("MSFT", as_of_date="2024-06-01")
    b = compute_consensus("MSFT", as_of_date="2024-06-01")
    assert a == b


# --------------------------------------------------------------------------- #
# screen_and_rank — the headline test
# --------------------------------------------------------------------------- #
def test_screen_and_rank_hard_filters_and_ranks(patched, capsys):
    # What the non-compliant names WOULD have scored, if the filter didn't exist:
    would_have = {t: compute_consensus(t, "2024-06-01") for t in _NON_COMPLIANT}

    ranked = screen_and_rank(_TEST_TICKERS, as_of_date="2024-06-01", top_k=25)

    with capsys.disabled():
        print("\n\n" + "=" * 96)
        print("Phase 5b — screen_and_rank(9 tickers) @ 2024-06-01   (NO LLM)")
        print("=" * 96)
        hdr = (f"  {'#':>2}  {'TICKER':<6} {'CONSENSUS':>10}  "
               + "  ".join(f"{k[:4].upper():>7}" for k in _INVESTOR_KEYS)
               + f"   {'COMPLIANT':>9}")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for i, r in enumerate(ranked, start=1):
            cells = "  ".join(f"{r['per_archetype_scores'][k]:+7.3f}" for k in _INVESTOR_KEYS)
            print(f"  {i:>2}  {r['ticker']:<6} {r['consensus_score']:>+10.3f}  {cells}   "
                  f"{str(r['passes_compliance']):>9}")

        print("\n  Excluded by the compliance HARD FILTER (absent from the table above):")
        for t in sorted(_NON_COMPLIANT):
            w = would_have[t]
            print(f"    {t:<6} verdict={w['compliance_verdict']:<14} "
                  f"consensus_score={w['consensus_score']:+.3f}  <-- would have ranked here, but excluded")
            print(f"           reason: {w['compliance_reason']}")
        print()

    ranked_tickers = [r["ticker"] for r in ranked]

    # (1) every non-compliant ticker is ABSENT entirely — not just last
    for t in _NON_COMPLIANT:
        assert t not in ranked_tickers, f"{t} is non-compliant but appears in the ranked output"

    # (2) proof this is a filter, not a sort: JPM's would-be score is not
    # rock-bottom, yet it is still gone.
    jpm_score = would_have["JPM"]["consensus_score"]
    assert any(r["consensus_score"] < jpm_score for r in ranked), (
        "JPM outscores at least one ranked name, so its absence proves the hard "
        "filter (not the sort) removed it"
    )

    # (3) everything that IS in the output passed compliance
    assert all(r["passes_compliance"] for r in ranked)

    # (4) sorted by consensus_score descending
    got = [r["consensus_score"] for r in ranked]
    assert got == sorted(got, reverse=True)

    # (5) exactly the 7 compliant names, none dropped
    assert set(ranked_tickers) == set(_TEST_TICKERS) - _NON_COMPLIANT
    assert len(ranked) == 7

    # (6) directional sanity: the strong compounders beat the ones with
    # shrinking earnings.
    rank_of = {r["ticker"]: i for i, r in enumerate(ranked)}
    assert rank_of["MSFT"] < rank_of["PFE"]
    assert rank_of["MSFT"] < rank_of["XOM"]


def test_screen_and_rank_respects_top_k(patched):
    ranked = screen_and_rank(_TEST_TICKERS, as_of_date="2024-06-01", top_k=3)
    assert len(ranked) == 3
    got = [r["consensus_score"] for r in ranked]
    assert got == sorted(got, reverse=True)
    assert not ({"JPM", "GS"} & {r["ticker"] for r in ranked})


def test_screen_and_rank_skips_bad_tickers_without_crashing(patched, monkeypatch, capsys):
    # BROKEN resolves to a context where price AND fundamental both fail ->
    # build_seed_from_ticker raises SeedBuildError.
    ctxs = dict(_CONTEXTS)
    ctxs["BROKEN"] = {
        "ticker": "BROKEN", "success": False,
        "price": {"success": False, "error": "no price data"},
        "fundamental": {"success": False, "error": "no fundamental data"},
    }
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(ctxs[ticker.strip().upper()]),
    )

    out = consensus_screener._run_consensus_screen(
        ["AAPL", "BROKEN", "MSFT", "JPM"], as_of_date="2024-06-01", top_k=25,
    )
    scores = [r["consensus_score"] for r in out["ranked"]]
    assert scores == sorted(scores, reverse=True)
    assert {r["ticker"] for r in out["ranked"]} == {"AAPL", "MSFT"}      # JPM filtered, BROKEN skipped
    assert [s["ticker"] for s in out["skipped"]] == ["BROKEN"]
    assert "seed build failed" in out["skipped"][0]["reason"]
    assert {r["ticker"] for r in out["excluded_non_compliant"]} == {"JPM"}


# --------------------------------------------------------------------------- #
# zero-LLM proof
# --------------------------------------------------------------------------- #
def test_no_llm_calls_anywhere_in_the_path(patched):
    """The `patched` fixture already turns every LLM entry point into a landmine
    that raises AssertionError. If any of these calls completes, the path is
    provably LLM-free. Timing is a secondary sanity check.
    """
    t0 = time.monotonic()
    compute_consensus("AAPL", "2024-06-01")
    compute_consensus("JPM", "2024-06-01")
    ranked = screen_and_rank(_TEST_TICKERS, as_of_date="2024-06-01", top_k=25)
    elapsed = time.monotonic() - t0

    assert patched["llm"] == 0, f"{patched['llm']} LLM call(s) were attempted"
    assert len(ranked) == 7
    # 9 tickers scored end-to-end in well under a second (no network, no model).
    assert elapsed < 5.0, f"consensus screen took {elapsed:.1f}s — unexpectedly slow for an LLM-free path"


if __name__ == "__main__":
    #   cd backend && python -m pytest tests/test_consensus_screener.py -s -q
    import sys

    seed_builder.get_stock_context = lambda ticker, as_of_date=None: copy.deepcopy(
        _CONTEXTS[ticker.strip().upper()]
    )
    out = consensus_screener._run_consensus_screen(_TEST_TICKERS, "2024-06-01", 25)
    for i, r in enumerate(out["ranked"], start=1):
        print(f"{i:>2}. {r['ticker']:<6} {r['consensus_score']:+.3f}  "
              + "  ".join(f"{k}={r['per_archetype_scores'][k]:+.2f}" for k in _INVESTOR_KEYS))
    for r in out["excluded_non_compliant"]:
        print(f"  EXCLUDED {r['ticker']}: {r['compliance_verdict']} "
              f"(score would have been {r['consensus_score']:+.3f})")
    sys.exit(0)
