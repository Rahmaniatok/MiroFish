"""
Phase 3a — tes generator persona INVESTOR.

`OasisProfileGenerator.generate_profiles_from_entities()` dulu menghasilkan
satu persona netizen per entitas sosial. Sekarang ia menghasilkan SATU persona
per arketipe investor (INVESTOR_ARCHETYPES) dari SELURUH seed graph finansial
hasil `build_seed_from_ticker()`.

Offline: `get_stock_context` di-patch dengan fixture dict (angka AAPL asli @
2024-06-01, sama seperti test Phase 2). `use_llm=False` -> jalur template
deterministik, tetap wajib mengutip angka nyata. Tidak ada jaringan.

Yang diverifikasi:
  - signature lama tetap: dipanggil dengan `entities=seed.entities`
  - tepat 5 persona, satu per arketipe, bentuk objek `OasisAgentProfile` utuh
  - stance (bullish/neutral/bearish) DITURUNKAN dari angka AAPL nyata, konsisten
    antar-run (bukan acak)
  - tiap bio + persona mengutip minimal satu angka entity nyata dari seed
  - viewpoint beragam (tidak semua stance sama)

Blok __main__ menjalankan versi LLM sungguhan dan mencetak semua persona
(untuk verifikasi manual — lihat instruksi task).
"""

import copy
import re

import pytest

from app.services import seed_builder
from app.services.oasis_profile_generator import (
    INVESTOR_ARCHETYPES,
    OasisAgentProfile,
    OasisProfileGenerator,
    _CONV_FLOOR,
    _clamp,
    _derive_investor_stance,
    _index_seed_entities,
    _mean,
)
from app.services.seed_builder import build_seed_from_ticker

# Bentuk persis output get_stock_context() — AAPL @ 2024-06-01 (angka nyata).
STOCK_CONTEXT_AAPL = {
    "ticker": "AAPL",
    "company_name": "Apple Inc.",
    "as_of_date": "2024-06-01",
    "success": True,
    "price": {
        "ticker": "AAPL",
        "success": True,
        "error": None,
        "period": "1y",
        "as_of_date": "2024-06-01",
        "latest_close": 190.4347,
        "latest_date": "2024-05-31",
        "stats": {"52w_high": 195.72, "52w_low": 163.22},
        "technical_indicators": {
            "price_vs_sma50": 8.61,
            "price_vs_sma200": 6.33,
            "rsi_14": 67.29,
            "macd_signal": {"macd": 4.2141, "signal": 4.3303, "histogram": -0.1162},
            "bollinger_position": 0.8127,
            "volume_vs_avg": 1.2413,
            "change_1d_pct": 0.5,
        },
    },
    "fundamental": {
        "ticker": "AAPL",
        "success": True,
        "error": None,
        "as_of_date": "2024-06-01",
        "is_point_in_time": False,
        "warning": "nilai terkini, bukan snapshot historis",
        "company_name": "Apple Inc.",
        "pe_ratio": 36.65178,
        "pb_ratio": 43.474186,
        "market_cap": 4669700046848,
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "revenue_growth_yoy": 0.164,
        "profit_margin": 0.27618998,
        "roe": 1.4875101,
        "debt_to_equity": 0.78445,
        "eps_growth": 0.287,
        "dividend_yield": 0.0032815575,
        "skipped_fields": {},
    },
    # Phase 1g shape — AAPL is compliant (Phase 3d adds Gamma as a 6th persona;
    # these investor tests assert the OTHER 5 are unaffected by its presence).
    "sharia_compliance": {
        "ticker": "AAPL",
        "success": True,
        "error": None,
        "standard": "AAOIFI Shari'ah Standard No. 21",
        "is_point_in_time": False,
        "debt_to_market_cap": 0.018327,
        "debt_to_market_cap_threshold": 0.33,
        "passes_debt_screen": True,
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "sector_exclusion_flag": False,
        "excluded_category": None,
        "exclusion_reason": None,
        "overall_compliant": True,
        "unavailable_checks": {"impure_income_ratio": "not computable from yfinance"},
    },
}

_ARCHETYPE_NAMES = [a["name"] for a in INVESTOR_ARCHETYPES]

# Stance yang HARUS keluar untuk angka AAPL di atas (diturunkan, bukan acak):
#   Value     -> bearish  (P/E 36.7, P/B 43.5 — tidak ada margin of safety)
#   Growth    -> bullish  (revenue +16.4% YoY, EPS +28.7% YoY)
#   Technical -> neutral  (harga > SMA50/200 tapi MACD bearish & Bollinger %B 0.81)
#   Quality   -> bullish  (ROE 149%, margin 27.6%, D/E 0.78)
#   Macro     -> bullish  (sektor Technology)
_EXPECTED_STANCE = {
    "Value Investor": "bearish",
    "Growth Investor": "bullish",
    "Technical Trader": "neutral",
    "Quality/Profitability Investor": "bullish",
    "Macro/Sector Investor": "bullish",
}

# Substring angka nyata (dari fixture) yang wajib muncul di bio/persona
# tiap arketipe — bukti data-grounding.
_MUST_CITE = {
    "Value Investor": ["36.7", "43.5"],          # P/E, P/B
    "Growth Investor": ["16.4%", "28.7%"],       # revenue growth, EPS growth
    "Technical Trader": ["67", "0.81"],          # RSI(14), Bollinger %B
    "Quality/Profitability Investor": ["149%", "27.6%", "0.78"],  # ROE, margin, D/E
    "Macro/Sector Investor": ["Technology"],     # Sector entity
}


@pytest.fixture
def aapl_seed(monkeypatch):
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(STOCK_CONTEXT_AAPL),
    )
    return build_seed_from_ticker("AAPL", as_of_date="2024-06-01")


@pytest.fixture
def personas(aapl_seed):
    generator = object.__new__(OasisProfileGenerator)
    generator.client = None            # use_llm=False -> tidak dipakai
    generator.model_name = "test-model"
    generator.zep_client = None
    generator.graph_id = None
    return generator.generate_profiles_from_entities(
        entities=aapl_seed.entities,
        use_llm=False,
    )


def _stance_of(profile: OasisAgentProfile) -> str:
    return profile.interested_topics[0].split(":", 1)[1].strip()


def _investors(personas):
    return [p for p in personas if p.source_entity_type == "InvestorArchetype"]


def test_one_persona_per_archetype_with_intact_object_shape(personas):
    # Phase 3d: 5 investor archetypes + Gamma the compliance monitor.
    assert len(personas) == len(INVESTOR_ARCHETYPES) + 1 == 6
    investors = _investors(personas)
    assert [p.profession for p in investors] == _ARCHETYPE_NAMES
    assert personas[-1].source_entity_type == "ShariaComplianceVerdict"  # Gamma last

    for p in investors:
        assert isinstance(p, OasisAgentProfile)
        # bentuk objek tidak berubah — field lama masih ada & terisi
        assert p.user_name and p.name and p.bio and p.persona
        assert p.source_entity_type == "InvestorArchetype"
        assert p.source_entity_uuid.startswith("AAPL::investor::")
        assert p.interested_topics[0].startswith("stance: ")
        # serialisasi downstream tetap jalan
        assert "persona" in p.to_reddit_format()
        assert "persona" in p.to_twitter_format()
        assert p.to_dict()["source_entity_type"] == "InvestorArchetype"


def test_stances_are_derived_from_real_aapl_numbers(personas):
    got = {p.profession: _stance_of(p) for p in _investors(personas)}
    assert got == _EXPECTED_STANCE


def test_stance_derivation_is_deterministic(aapl_seed, capsys):
    gen = object.__new__(OasisProfileGenerator)
    gen.client = None
    gen.model_name = "test-model"
    gen.zep_client = None
    gen.graph_id = None

    # Two independent full generation passes over the same seed.
    run1 = gen.generate_profiles_from_entities(entities=aapl_seed.entities, use_llm=False)
    run2 = gen.generate_profiles_from_entities(entities=aapl_seed.entities, use_llm=False)

    stance1 = {p.profession: _stance_of(p) for p in _investors(run1)}
    stance2 = {p.profession: _stance_of(p) for p in _investors(run2)}

    # And the low-level derivation called straight on the indexed seed graph.
    idx = _index_seed_entities(aapl_seed.entities)
    stance_direct = {
        a["name"]: _derive_investor_stance(a["key"], idx)["stance"]
        for a in INVESTOR_ARCHETYPES
    }

    with capsys.disabled():
        print("\n\n  archetype                        run 1     run 2     _derive_investor_stance()")
        print("  " + "-" * 82)
        for name in _ARCHETYPE_NAMES:
            print(f"  {name:<32} {stance1[name]:<9} {stance2[name]:<9} {stance_direct[name]}")
        print()

    # EXACT equality, archetype by archetype — not "similar".
    for name in _ARCHETYPE_NAMES:
        assert stance1[name] == stance2[name] == stance_direct[name] == _EXPECTED_STANCE[name]


def test_every_bio_and_persona_cites_a_real_entity_value(personas):
    for p in _investors(personas):
        text = f"{p.bio}\n{p.persona}"
        assert re.search(r"\d", p.bio), f"{p.profession} bio has no number: {p.bio!r}"
        for needle in _MUST_CITE[p.profession]:
            assert needle in text, f"{p.profession} does not cite {needle!r}:\n{text}"


def test_viewpoints_are_diverse(personas):
    stances = {_stance_of(p) for p in _investors(personas)}
    assert len(stances) >= 2, f"all personas share one stance: {stances}"


def test_gamma_present_but_does_not_disturb_the_investors(personas):
    """Phase 3d: the 6th persona is Gamma, clearly separated from the 5 stances."""
    gamma = [p for p in personas if p.source_entity_type == "ShariaComplianceVerdict"]
    assert len(gamma) == 1
    g = gamma[0]
    assert g.name == "Gamma · AAPL"
    assert g.interested_topics[0] == "verdict: compliant"   # AAPL fixture is compliant
    assert not g.interested_topics[0].startswith("stance: ")
    assert "0.0183" in f"{g.bio}\n{g.persona}"              # real debt/market-cap ratio
    # the 5 investors are exactly the Phase 3a set, untouched
    assert {p.profession for p in _investors(personas)} == set(_ARCHETYPE_NAMES)


# ---------------------------------------------------------------------------
# Phase 5a — continuous conviction score alongside the categorical stance
# ---------------------------------------------------------------------------

# Expected (stance, score) for the AAPL fixture. The stance column is byte-for
# -byte the Phase 3a `_EXPECTED_STANCE`; the score column is the Phase 5a
# addition, sanity-checked in the design review:
#   Value     bearish  -0.83  (P/E 36.7 & P/B 43.5 both far past the bear line)
#   Growth    bullish  +0.78  (rev +16.4%, EPS +28.7% - EPS term caps at +1.0)
#   Technical neutral  -0.17  (trend up, but RSI 67 + %B 0.81 lean overbought)
#   Quality   bullish  +0.84  (ROE/margin cap high; D/E 0.78 shaves ~0.10 off)
#   Macro     bullish  +0.60  (favoured sector +0.5, mega-cap proxy +0.1)
_EXPECTED_SCORE = {
    "Value Investor": -0.833,
    "Growth Investor": +0.780,
    "Quality/Profitability Investor": +0.841,
    "Macro/Sector Investor": +0.600,
    "Technical Trader": -0.174,
}


def test_stance_score_pairs_for_aapl_are_directionally_and_relatively_sane(aapl_seed, capsys):
    """Prints all 5 archetypes' (stance, score) side by side for a visual check,
    then asserts the exact scores plus the invariants Phase 5b relies on."""
    idx = _index_seed_entities(aapl_seed.entities)
    rows = {
        a["name"]: _derive_investor_stance(a["key"], idx)
        for a in INVESTOR_ARCHETYPES
    }

    with capsys.disabled():
        print("\n\n  AAPL @ 2024-06-01 — (stance, score) per investor archetype")
        print("  " + "-" * 62)
        print(f"  {'archetype':<32} {'stance':<9} {'score':>7}   sign ok?")
        print("  " + "-" * 62)
        for name in _ARCHETYPE_NAMES:
            info = rows[name]
            ok = (
                (info["stance"] == "bullish" and info["score"] > 0)
                or (info["stance"] == "bearish" and info["score"] < 0)
                or (info["stance"] == "neutral")
            )
            print(f"  {name:<32} {info['stance']:<9} {info['score']:>+7.3f}   {'yes' if ok else 'NO'}")
        print()

    # (a) categorical label is unchanged from Phase 3a, archetype by archetype
    assert {n: rows[n]["stance"] for n in _ARCHETYPE_NAMES} == _EXPECTED_STANCE

    # (b) every score is a float inside [-1.0, +1.0]
    for name in _ARCHETYPE_NAMES:
        s = rows[name]["score"]
        assert isinstance(s, float) and -1.0 <= s <= 1.0, f"{name}: score {s!r} out of range"

    # (c) stance and score never disagree in direction
    for name in _ARCHETYPE_NAMES:
        stance, score = rows[name]["stance"], rows[name]["score"]
        if stance == "bullish":
            assert score > 0, f"{name}: bullish but score {score:+.3f}"
        elif stance == "bearish":
            assert score < 0, f"{name}: bearish but score {score:+.3f}"

    # (d) exact scores (deterministic - no randomness, no LLM)
    for name in _ARCHETYPE_NAMES:
        assert rows[name]["score"] == pytest.approx(_EXPECTED_SCORE[name], abs=1e-3), (
            f"{name}: score {rows[name]['score']:+.4f} != {_EXPECTED_SCORE[name]:+.4f}"
        )

    # (e) additive change only - the Phase 3a/4 fields are all still present
    for name in _ARCHETYPE_NAMES:
        assert set(rows[name]) >= {"stance", "rationale", "evidence", "score"}

    # (f) relative conviction: Value's bearish call and Quality's bullish call
    # are the two highest-magnitude reads (metrics far past threshold on both);
    # among the bullish calls, the metric-driven ones outrank Macro's
    # deliberately-coarse fixed score; and the neutral call sits nearest zero.
    by_magnitude = sorted(_ARCHETYPE_NAMES, key=lambda n: abs(rows[n]["score"]), reverse=True)
    assert set(by_magnitude[:2]) == {"Value Investor", "Quality/Profitability Investor"}
    assert rows["Quality/Profitability Investor"]["score"] > rows["Macro/Sector Investor"]["score"]
    assert rows["Growth Investor"]["score"] > rows["Macro/Sector Investor"]["score"]
    assert by_magnitude[-1] == "Technical Trader"  # the lone neutral, closest to 0


def test_stance_score_is_deterministic(aapl_seed):
    """Same seed graph => byte-identical score on every call."""
    idx = _index_seed_entities(aapl_seed.entities)
    for a in INVESTOR_ARCHETYPES:
        runs = {_derive_investor_stance(a["key"], idx)["score"] for _ in range(5)}
        assert len(runs) == 1, f"{a['key']}: score not deterministic: {runs}"


def test_sign_guard_fires_on_a_ladder_quantization_edge_case(capsys):
    """Proof the ±_CONV_FLOOR guard actually does something.

    Synthetic technical case: price is +2% vs both moving averages, so the
    integer ladder scores +1+1 = +2 -> BULLISH. But RSI is 69 (just under the
    70 overbought line the ladder rounds to 0), and the continuous RSI term is
    -(69-50)/20 = -0.95. The raw continuous mean is therefore NEGATIVE while the
    label is bullish - exactly the mismatch `_sign_guarded_score` exists to
    catch. Without the guard the score would contradict the stance.
    """
    idx = {
        "ticker": "SYNTH",
        "valuation": {}, "fundamental": {},
        "technical": {
            "price_vs_sma50": {"value": 2.0},
            "price_vs_sma200": {"value": 2.0},
            "rsi_14": {"value": 69.0},
            "bollinger_position": {"value": 0.5},
        },
    }
    info = _derive_investor_stance("technical", idx)

    # what the continuous mean would have been, with NO guard:
    raw_terms = [
        _clamp(2.0 / 10.0),
        _clamp(2.0 / 10.0),
        -_clamp((69.0 - 50.0) / 20.0),
        -_clamp((0.5 - 0.5) / 0.3),
    ]
    raw_mean = _mean(raw_terms)

    with capsys.disabled():
        print(f"\n\n  sign-guard edge case: ladder -> {info['stance']!r}")
        print(f"    raw continuous mean (unguarded) = {raw_mean:+.4f}  (would contradict the label)")
        print(f"    guarded score                   = {info['score']:+.4f}\n")

    assert info["stance"] == "bullish"       # ladder: +1 +1 = +2
    assert raw_mean < 0                       # continuous formula alone disagrees
    assert info["score"] == pytest.approx(_CONV_FLOOR)   # guard pulled it to the floor
    assert info["score"] > 0                  # ...and now it agrees with the label


def test_sign_guard_is_a_noop_when_formula_already_agrees(aapl_seed):
    """The guard must not distort scores that are already on the right side -
    it only ever clamps toward the label, never away from a correct sign."""
    idx = _index_seed_entities(aapl_seed.entities)
    # AAPL's 5 archetypes: none of them hit the guard (all |raw| > _CONV_FLOOR
    # and already sign-consistent), so the printed scores equal the raw means.
    for a in INVESTOR_ARCHETYPES:
        info = _derive_investor_stance(a["key"], idx)
        assert abs(info["score"]) > _CONV_FLOOR, (
            f"{a['key']}: AAPL score {info['score']:+.3f} sits on the guard floor "
            f"- expected the raw formula to stand on its own here"
        )


if __name__ == "__main__":
    # Verifikasi manual dengan LLM sungguhan:
    #   cd backend && python -m pytest tests/test_investor_persona_generator.py -s -q
    # atau langsung:
    #   cd backend && python tests/test_investor_persona_generator.py
    import app.services.seed_builder as _sb

    _orig = _sb.get_stock_context
    _sb.get_stock_context = lambda ticker, as_of_date=None: copy.deepcopy(STOCK_CONTEXT_AAPL)
    try:
        seed = build_seed_from_ticker("AAPL", as_of_date="2024-06-01")
    finally:
        _sb.get_stock_context = _orig

    generator = OasisProfileGenerator()
    result = generator.generate_profiles_from_entities(
        entities=seed.entities,
        use_llm=True,
        parallel_count=5,
    )

    print("\n\n" + "=" * 78)
    print("INVESTOR PERSONAS — AAPL @ 2024-06-01")
    print("=" * 78)
    for p in result:
        stance = _stance_of(p)
        print(f"\n[{p.profession}]  stance = {stance.upper()}")
        print("-" * 78)
        print(f"BIO:     {p.bio}")
        print(f"PERSONA: {p.persona}")
        print(f"TOPICS:  {', '.join(p.interested_topics)}")
