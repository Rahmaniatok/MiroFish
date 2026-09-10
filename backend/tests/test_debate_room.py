"""
Phase 4a — tes ruang debat multi-ronde untuk 6 persona tetap.

MiroFish lama menjalankan populasi besar agen sosial di DUA platform OASIS
paralel (Twitter + Reddit), memori antar-ronde = feed platform OASIS + memori
percakapan tiap agen `camel`. `ZepGraphMemoryUpdater` cuma penulis SATU ARAH
pasca-simulasi (tidak pernah balik ke prompt agen).

Mesin investasi mengganti populasi itu dengan TEPAT 6 persona deterministik
(Phase 3a + 3d). Phase 4a menyimpan HANYA struktur ronde-nya:

  * SATU ruang debat bersama (dual-platform / retail-crowd dibuang).
  * Jumlah ronde bisa dikonfigurasi (`DEFAULT_DEBATE_ROUNDS`, default 3).
  * Ronde N: tiap persona menghasilkan TEPAT satu pernyataan, bereaksi ke data
    seed DAN ke semua pernyataan ronde 1..N-1 (transkrip eksplisit — analog
    langsung dari feed OASIS, tanpa recommender).

INVARIAN GAMMA (jaminan struktural):
  `_derive_sharia_verdict(idx)` dipanggil SEKALI, sebelum loop ronde. Tiap
  ronde, `DebateStatement.disposition` untuk Gamma diambil dari dict beku itu —
  BUKAN di-parse dari output LLM. Jadi verdict Gamma tidak bisa bergeser
  antar-ronde apa pun yang diargumentasikan persona lain.

Offline: `get_stock_context` di-patch fixture (angka AAPL & JPM asli @
2024-06-01, sama seperti tes Phase 2/3). `use_llm=False` -> template
deterministik yang tetap mengutip angka nyata dan tetap mereferensikan ronde
sebelumnya.

Blok __main__ menjalankan debat 3-ronde LLM sungguhan untuk AAPL dan mencetak
transkrip (verifikasi manual — lihat instruksi task).
"""

import copy
import re

import pytest

from app.services import seed_builder
from app.services.debate_room import (
    DEFAULT_DEBATE_ROUNDS,
    DebateRoom,
    DebateTranscript,
    run_debate,
)
from app.services.oasis_profile_generator import (
    GAMMA_ARCHETYPE,
    INVESTOR_ARCHETYPES,
    _derive_investor_stance,
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

# Stance Phase 3a untuk angka AAPL (diturunkan, bukan acak).
_EXPECTED_STANCE = {
    "value": "bearish",
    "growth": "bullish",
    "technical": "neutral",
    "quality": "bullish",
    "macro": "bullish",
}


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(_CONTEXTS[ticker.upper()]),
    )


def _seed(ticker):
    return build_seed_from_ticker(ticker, as_of_date="2024-06-01")


def _debate(ticker, rounds=DEFAULT_DEBATE_ROUNDS):
    return run_debate(_seed(ticker), rounds=rounds, use_llm=False)


# --------------------------------------------------------------------------- #
def test_default_rounds_is_small():
    assert DEFAULT_DEBATE_ROUNDS == 3


def test_shape_six_statements_per_round_in_phase3_order(patched):
    t = _debate("AAPL", rounds=3)
    assert isinstance(t, DebateTranscript)
    assert t.rounds == 3
    assert len(t.statements) == 3
    assert len(t.personas) == 6

    for r, round_stmts in enumerate(t.statements, start=1):
        assert len(round_stmts) == 6, f"round {r}: expected 6 statements"
        # positions 0..5, Phase 3 order: 5 investors then Gamma
        assert [s.position for s in round_stmts] == [0, 1, 2, 3, 4, 5]
        assert [s.persona_key for s in round_stmts] == [
            "value", "growth", "technical", "quality", "macro", GAMMA_ARCHETYPE["key"]
        ]
        assert [s.role for s in round_stmts[:5]] == ["investor"] * 5
        assert round_stmts[5].role == "compliance"
        for s in round_stmts:
            assert s.round_num == r
            assert s.text.strip()


def test_rounds_are_configurable(patched):
    assert _debate("AAPL", rounds=1).rounds == 1
    assert len(_debate("AAPL", rounds=1).statements) == 1
    five = _debate("AAPL", rounds=5)
    assert five.rounds == 5 and len(five.statements) == 5
    with pytest.raises(Exception):
        _debate("AAPL", rounds=0)


def test_investor_stances_match_phase3_and_stay_constant(patched):
    t = _debate("AAPL", rounds=3)
    for key, expected in _EXPECTED_STANCE.items():
        dispositions = [s.disposition for s in t.for_persona(key)]
        assert dispositions == [expected, expected, expected], (
            f"{key}: dispositions drifted across rounds: {dispositions}"
        )


def test_investor_statements_evolve_and_reference_prior_rounds(patched):
    t = _debate("AAPL", rounds=3)

    # Round 1: opening statements, no prior reference.
    for s in t.round_statements(1)[:5]:
        assert "opening the debate" in s.text.lower()

    # Rounds 2-3: each investor statement references an earlier round explicitly
    # and names a persona that already spoke.
    prior_names = {p.name for p in t.personas}
    for r in (2, 3):
        for s in t.round_statements(r)[:5]:
            assert re.search(rf"round {r}", s.text, re.IGNORECASE), s.text
            assert "round 1" in s.text.lower() or "earlier round" in s.text.lower()
            assert any(name in s.text for name in prior_names), s.text
            # statement must differ from that persona's round-1 statement
            r1 = next(x for x in t.round_statements(1) if x.persona_key == s.persona_key)
            assert s.text != r1.text


def test_gamma_verdict_is_identical_every_round_aapl(patched):
    t = _debate("AAPL", rounds=3)
    gamma = t.gamma_statements()
    assert len(gamma) == 3

    # (b) verdict recorded on Gamma is byte-constant across all 3 rounds
    assert t.gamma_verdicts() == ["compliant", "compliant", "compliant"]
    # deterministic path: the prose itself is identical too (pure restatement)
    assert gamma[0].text == gamma[1].text == gamma[2].text
    # and it cites the real screen figures
    for s in gamma:
        assert "0.0183" in s.text          # real debt/market-cap ratio
        assert "compliant" in s.text.lower()


def test_gamma_verdict_is_a_structural_loop_invariant(patched):
    """The recorded verdict must equal the ONE pre-loop derivation, every round,
    for both a compliant and a non-compliant name."""
    for ticker, expected in (("AAPL", "compliant"), ("JPM", "non_compliant")):
        seed = _seed(ticker)
        direct = _derive_sharia_verdict(_index_seed_entities(seed.entities))["verdict"]
        assert direct == expected

        t = run_debate(seed, rounds=4, use_llm=False)
        assert t.gamma_verdict["verdict"] == direct
        assert t.gamma_verdicts() == [direct] * 4
        # Gamma's disposition is never the parsed-from-text kind: it equals the
        # frozen derivation regardless of round.
        for s in t.gamma_statements():
            assert s.disposition == direct


def test_jpm_gamma_non_compliant_cites_verbatim_reason_each_round(patched):
    t = run_debate(_seed("JPM"), rounds=3, use_llm=False)
    for s in t.gamma_statements():
        assert s.disposition == "non_compliant"
        assert "1.4247" in s.text                       # real failed ratio
        assert "conventional_finance" in s.text          # verbatim excluded_category


def test_every_statement_is_data_grounded(patched):
    t = _debate("AAPL", rounds=3)
    for s in t.flat():
        assert re.search(r"\d", s.text), f"{s.persona_name} r{s.round_num}: no figure in statement"
    # a couple of spot checks against the real AAPL numbers
    value_r1 = next(s for s in t.round_statements(1) if s.persona_key == "value")
    assert "36.7" in value_r1.text and "43.5" in value_r1.text   # P/E, P/B


def _statements_dump(t):
    # persona prose is stable; only Phase-3 persona.user_name carries a random
    # suffix, so compare the statements (+ frozen verdict), not the personas.
    return (
        [[s.to_dict() for s in rnd] for rnd in t.statements],
        t.gamma_verdict,
    )


def test_debate_is_deterministic_without_llm(patched):
    assert _statements_dump(_debate("AAPL", rounds=3)) == _statements_dump(
        _debate("AAPL", rounds=3)
    )


def test_run_debate_and_debateroom_agree(patched):
    seed = _seed("AAPL")
    via_fn = run_debate(seed, rounds=2, use_llm=False)
    via_cls = DebateRoom(seed, use_llm=False).run(rounds=2)
    assert _statements_dump(via_fn) == _statements_dump(via_cls)


if __name__ == "__main__":
    #   cd backend && python tests/test_debate_room.py
    import app.services.seed_builder as _sb
    from app.services.debate_room import run_debate as _run

    _orig = _sb.get_stock_context
    _sb.get_stock_context = lambda ticker, as_of_date=None: copy.deepcopy(
        _CONTEXTS[ticker.upper()]
    )
    try:
        seed = build_seed_from_ticker("AAPL", as_of_date="2024-06-01")
    finally:
        _sb.get_stock_context = _orig

    rounds = 3
    transcript = _run(seed, rounds=rounds, use_llm=True)

    print("\n\n" + "=" * 90)
    print(f"DEBATE ROOM — {transcript.ticker} @ {transcript.as_of_date} — "
          f"{rounds} rounds — 5 investors + Gamma  (LLM)")
    print("=" * 90)
    print(f"Gamma verdict (derived ONCE, before round 1): "
          f"{transcript.gamma_verdict['verdict'].upper()} — {transcript.gamma_verdict['rationale']}")

    for r in range(1, rounds + 1):
        print("\n" + "#" * 90)
        print(f"# ROUND {r}")
        print("#" * 90)
        for s in transcript.round_statements(r):
            tag = f"{s.persona_name}  [{s.role}: {s.disposition}]"
            print(f"\n  {tag}\n  {'-' * len(tag)}")
            print(f"  {s.text}")

    print("\n\n" + "=" * 90)
    print("VERIFY:")
    print(f"  (a) 5 investors — statements evolve / reference prior rounds:")
    for key in ("value", "growth", "technical", "quality", "macro"):
        stances = [s.disposition for s in transcript.for_persona(key)]
        print(f"      {key:<10} stance each round: {stances}  (constant: {len(set(stances)) == 1})")
    print(f"  (b) Gamma verdict each round: {transcript.gamma_verdicts()}  "
          f"(identical: {len(set(transcript.gamma_verdicts())) == 1})")
    print("=" * 90)
