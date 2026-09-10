"""
Phase 3d — tes persona "Gamma", monitor kepatuhan Syariah.

Gamma adalah persona ke-6 yang dihasilkan `generate_profiles_from_entities()`
bersama 5 arketipe investor Phase 3a — SATU seed graph masuk, 6 persona keluar.
Beda struktural: verdict Gamma = compliant / non_compliant / indeterminate
(BUKAN bullish/neutral/bearish), diturunkan deterministik dari entity
`ShariaScreen` di seed graph (Phase 1g -> Phase 2a extractor), SEBELUM LLM.

Ditandai supaya Phase 5 bisa menegakkannya sebagai HARD FILTER:
  - source_entity_type == "ShariaComplianceVerdict"   (investor: "InvestorArchetype")
  - interested_topics[0] == "verdict: <...>"           (investor: "stance: <...>")
  - source_entity_uuid == "<TICKER>::sharia::verdict"

Offline: `get_stock_context` di-patch dengan fixture (angka AAPL & JPM asli @
2024-06-01, kontras Phase 1g). `use_llm=False` -> template deterministik.

  AAPL -> compliant     (debt/market-cap 0.0183 < 0.33; tidak ada larangan aktivitas)
  JPM  -> non_compliant (conventional_finance / banking; debt/market-cap 1.4247)

Blok __main__ menjalankan versi LLM sungguhan dan mencetak Gamma untuk kedua
ticker (verifikasi manual — lihat instruksi task).
"""

import copy
import re

import pytest

from app.services import seed_builder
from app.services.oasis_profile_generator import (
    INVESTOR_ARCHETYPES,
    OasisAgentProfile,
    OasisProfileGenerator,
    _derive_sharia_verdict,
    _index_seed_entities,
)
from app.services.seed_builder import build_seed_from_ticker

_ARCHETYPE_NAMES = [a["name"] for a in INVESTOR_ARCHETYPES]

# --- fixtures: bentuk persis get_stock_context(), angka nyata @ 2024-06-01 ---
STOCK_CONTEXT_AAPL = {
    "ticker": "AAPL",
    "company_name": "Apple Inc.",
    "as_of_date": "2024-06-01",
    "success": True,
    "price": {
        "ticker": "AAPL", "success": True, "error": None,
        "period": "1y", "as_of_date": "2024-06-01",
        "latest_close": 190.4347, "latest_date": "2024-05-31",
        "stats": {"52w_high": 195.72, "52w_low": 163.22},
        "technical_indicators": {
            "price_vs_sma50": 8.61, "price_vs_sma200": 6.33, "rsi_14": 67.29,
            "macd_signal": {"macd": 4.2141, "signal": 4.3303, "histogram": -0.1162},
            "bollinger_position": 0.8127, "volume_vs_avg": 1.2413, "change_1d_pct": 0.5,
        },
    },
    "fundamental": {
        "ticker": "AAPL", "success": True, "error": None, "as_of_date": "2024-06-01",
        "is_point_in_time": False, "warning": "nilai terkini",
        "company_name": "Apple Inc.",
        "pe_ratio": 36.65178, "pb_ratio": 43.474186, "market_cap": 4669700046848,
        "sector": "Technology", "industry": "Consumer Electronics",
        "revenue_growth_yoy": 0.164, "profit_margin": 0.27618998,
        "roe": 1.4875101, "debt_to_equity": 0.78445,
        "eps_growth": 0.287, "dividend_yield": 0.0032815575, "skipped_fields": {},
    },
    "sharia_compliance": {
        "ticker": "AAPL", "success": True, "error": None,
        "standard": "AAOIFI Shari'ah Standard No. 21", "is_point_in_time": False,
        "total_debt": 85570002944, "market_cap": 4669700046848,
        "debt_to_market_cap": 0.018327, "debt_to_market_cap_threshold": 0.33,
        "passes_debt_screen": True,
        "sector": "Technology", "industry": "Consumer Electronics",
        "sector_exclusion_flag": False, "excluded_category": None, "exclusion_reason": None,
        "overall_compliant": True,
        "unavailable_checks": {"impure_income_ratio": "not computable from yfinance"},
    },
}

STOCK_CONTEXT_JPM = {
    "ticker": "JPM",
    "company_name": "JPMorgan Chase & Co.",
    "as_of_date": "2024-06-01",
    "success": True,
    "price": {
        "ticker": "JPM", "success": True, "error": None,
        "period": "1y", "as_of_date": "2024-06-01",
        "latest_close": 198.48, "latest_date": "2024-05-31",
        "stats": {"52w_high": 205.88, "52w_low": 135.19},
        "technical_indicators": {
            "price_vs_sma50": 4.0, "price_vs_sma200": 20.8, "rsi_14": 60.11,
            "macd_signal": {"macd": 1.9277, "signal": 2.004, "histogram": -0.0763},
            "bollinger_position": 0.7913, "volume_vs_avg": 1.6569, "change_1d_pct": 1.66,
        },
    },
    "fundamental": {
        "ticker": "JPM", "success": True, "error": None, "as_of_date": "2024-06-01",
        "is_point_in_time": False, "warning": "nilai terkini",
        "company_name": "JPMorgan Chase & Co.",
        "pe_ratio": 15.152072, "pb_ratio": 2.666852, "market_cap": 942885240832,
        "sector": "Financial Services", "industry": "Banks - Diversified",
        "revenue_growth_yoy": 0.304, "profit_margin": 0.34921002,
        "roe": 0.17789, "debt_to_equity": None,
        "eps_growth": 0.469, "dividend_yield": 0.016972646, "skipped_fields": {},
    },
    "sharia_compliance": {
        "ticker": "JPM", "success": True, "error": None,
        "standard": "AAOIFI Shari'ah Standard No. 21", "is_point_in_time": False,
        "total_debt": 1343280054784, "market_cap": 942885240832,
        "debt_to_market_cap": 1.424677, "debt_to_market_cap_threshold": 0.33,
        "passes_debt_screen": False,
        "sector": "Financial Services", "industry": "Banks - Diversified",
        "sector_exclusion_flag": True,
        "excluded_category": "conventional_finance",
        "exclusion_reason": (
            "conventional (interest-based) financials — banking / insurance / lending "
            "(yfinance industry: 'Banks - Diversified')"
        ),
        "overall_compliant": False,
        "unavailable_checks": {"impure_income_ratio": "not computable from yfinance"},
    },
}

_CONTEXTS = {"AAPL": STOCK_CONTEXT_AAPL, "JPM": STOCK_CONTEXT_JPM}


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(_CONTEXTS[ticker.upper()]),
    )


def _generator():
    gen = object.__new__(OasisProfileGenerator)
    gen.client = None            # use_llm=False -> tidak dipakai
    gen.model_name = "test-model"
    gen.zep_client = None
    gen.graph_id = None
    return gen


def _personas(ticker):
    seed = build_seed_from_ticker(ticker, as_of_date="2024-06-01")
    return _generator().generate_profiles_from_entities(entities=seed.entities, use_llm=False)


def _gamma(personas):
    hits = [p for p in personas if p.source_entity_type == "ShariaComplianceVerdict"]
    assert len(hits) == 1, f"expected exactly one Gamma verdict, got {len(hits)}"
    return hits[0]


def _investors(personas):
    return [p for p in personas if p.source_entity_type == "InvestorArchetype"]


def _verdict_of(profile):
    return profile.interested_topics[0].split(":", 1)[1].strip()


# --------------------------------------------------------------------------- #
def test_six_personas_five_investors_plus_one_gamma(patched):
    for ticker in ("AAPL", "JPM"):
        personas = _personas(ticker)
        assert len(personas) == 6, f"{ticker}: expected 6 personas, got {len(personas)}"
        assert len(_investors(personas)) == 5
        gamma = _gamma(personas)
        # Phase-5 discriminators
        assert gamma.profession == "Sharia Compliance Monitor"
        assert gamma.source_entity_uuid == f"{ticker}::sharia::verdict"
        assert gamma.interested_topics[0].startswith("verdict: ")
        assert not gamma.interested_topics[0].startswith("stance: ")
        assert gamma.name == f"Gamma · {ticker}"
        # object shape unchanged
        assert isinstance(gamma, OasisAgentProfile)
        assert "persona" in gamma.to_reddit_format()
        assert gamma.to_dict()["source_entity_type"] == "ShariaComplianceVerdict"


def test_aapl_gamma_is_compliant_and_cites_the_real_ratio(patched):
    gamma = _gamma(_personas("AAPL"))
    assert _verdict_of(gamma) == "compliant"
    text = f"{gamma.bio}\n{gamma.persona}"
    assert "0.0183" in text, f"AAPL Gamma must cite debt/market-cap 0.0183:\n{text}"
    assert "0.33" in text                     # the AAOIFI cap it clears


def test_jpm_gamma_is_non_compliant_with_the_verbatim_reason(patched):
    gamma = _gamma(_personas("JPM"))
    assert _verdict_of(gamma) == "non_compliant"
    text = f"{gamma.bio}\n{gamma.persona}"
    # the actual excluded_category, not a vaguer paraphrase
    assert "conventional_finance" in gamma.bio
    # the verbatim exclusion_reason (contains "banking") lands in the persona
    assert "banking" in gamma.persona
    # the real failed ratio is cited
    assert "1.4247" in text, f"JPM Gamma must cite debt/market-cap 1.4247:\n{text}"


def test_gamma_verdict_is_deterministic_and_matches_direct_derivation(patched, capsys):
    rows = []
    for ticker in ("AAPL", "JPM"):
        seed = build_seed_from_ticker(ticker, as_of_date="2024-06-01")
        run1 = _generator().generate_profiles_from_entities(entities=seed.entities, use_llm=False)
        run2 = _generator().generate_profiles_from_entities(entities=seed.entities, use_llm=False)
        idx = _index_seed_entities(seed.entities)
        direct = _derive_sharia_verdict(idx)["verdict"]
        v1, v2 = _verdict_of(_gamma(run1)), _verdict_of(_gamma(run2))
        rows.append((ticker, v1, v2, direct))
        assert v1 == v2 == direct

    with capsys.disabled():
        print("\n\n  ticker   run 1          run 2          _derive_sharia_verdict()")
        print("  " + "-" * 60)
        for t, a, b, c in rows:
            print(f"  {t:<7}  {a:<13}  {b:<13}  {c}")
        print()

    assert dict((t, c) for t, _, _, c in rows) == {"AAPL": "compliant", "JPM": "non_compliant"}


def test_the_five_investors_are_unaffected_by_gamma(patched):
    for ticker in ("AAPL", "JPM"):
        seed = build_seed_from_ticker(ticker, as_of_date="2024-06-01")
        run1 = _generator().generate_profiles_from_entities(entities=seed.entities, use_llm=False)
        run2 = _generator().generate_profiles_from_entities(entities=seed.entities, use_llm=False)

        inv1, inv2 = _investors(run1), _investors(run2)
        # still exactly the Phase 3a set, in order
        assert [p.profession for p in inv1] == _ARCHETYPE_NAMES
        # still a bullish/neutral/bearish stance, still deterministic
        s1 = [p.interested_topics[0] for p in inv1]
        s2 = [p.interested_topics[0] for p in inv2]
        assert s1 == s2
        assert all(x.startswith("stance: ") for x in s1)
        assert all(x.split(":", 1)[1].strip() in {"bullish", "neutral", "bearish"} for x in s1)
        # still data-grounded (a number in every investor bio)
        for p in inv1:
            assert re.search(r"\d", p.bio), f"{ticker}/{p.profession}: bio has no number"


if __name__ == "__main__":
    #   cd backend && python tests/test_sharia_compliance_persona.py
    import app.services.seed_builder as _sb

    _orig = _sb.get_stock_context
    _sb.get_stock_context = lambda ticker, as_of_date=None: copy.deepcopy(_CONTEXTS[ticker.upper()])
    try:
        seeds = {t: build_seed_from_ticker(t, as_of_date="2024-06-01") for t in ("AAPL", "JPM")}
    finally:
        _sb.get_stock_context = _orig

    generator = OasisProfileGenerator()
    print("\n\n" + "=" * 80)
    print("GAMMA — Sharia compliance monitor — AAPL vs JPM @ 2024-06-01 (LLM)")
    print("=" * 80)
    for ticker, seed in seeds.items():
        result = generator.generate_profiles_from_entities(
            entities=seed.entities, use_llm=True, parallel_count=6,
        )
        gamma = [p for p in result if p.source_entity_type == "ShariaComplianceVerdict"][0]
        investors = [p for p in result if p.source_entity_type == "InvestorArchetype"]
        verdict = gamma.interested_topics[0].split(":", 1)[1].strip()
        print(f"\n\n#### {ticker}  —  {len(result)} personas total "
              f"({len(investors)} investors + Gamma)")
        print(f"[Gamma · {ticker}]  VERDICT = {verdict.upper()}")
        print("-" * 80)
        print(f"BIO:     {gamma.bio}")
        print(f"PERSONA: {gamma.persona}")
        print(f"TOPICS:  {', '.join(gamma.interested_topics)}")
        print(f"\n  investor stances (unaffected): "
              + ", ".join(f"{p.profession.split('/')[0].split()[0]}={p.interested_topics[0].split(':',1)[1].strip()}"
                          for p in investors))
