"""
Fase 11a — tes persona_oasis_adapter.py (docs/design/fase11a_oasis_adapter.md).

Offline: LLM (create_chat_completion), _get_llm_client dan time.sleep di-patch; DB persona diisolasi
lewat tmp_path; artefak ditulis ke tmp_path (sim_root).
"""

import csv
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from app.services import persona_generator as pg
from app.services import persona_oasis_adapter as ad
from app.services.persona_generator import InvestorPersona


# ---------------------------------------------------------------------------
# Helper / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "PERSONA_DB_PATH", str(tmp_path / "persona_runs.db"))
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    monkeypatch.setattr(ad, "_get_llm_client", lambda: object())
    monkeypatch.setattr(ad, "extract_chat_completion_text", lambda r: r.choices[0].message.content)


class FakeLLM:
    """Urutan respons: str (content) atau Exception."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, client, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=r))])


@pytest.fixture
def llm(monkeypatch):
    def _setup(responses):
        fake = FakeLLM(responses)
        monkeypatch.setattr(ad, "create_chat_completion", fake)
        return fake
    return _setup


NAMES = ["Marisol Vance", "Dex Okonkwo-Hale", "Dr. Ilse Brandvold", "Tante Rina",
         "Kenji Arai", "Priya Nair", "Omar Haddad", "Lena Fischer"]


def persona_dict(i, **overrides):
    d = {
        "name": NAMES[i], "tagline": f"tagline {i}", "investment_philosophy": f"philosophy {i}",
        "philosophy_label": f"label {i}", "biases": ["bias a", "bias b"], "personality": f"personality {i}",
        "communication_style": f"style {i}", "edge_vs_others": f"edge {i}", "short_bio": f"bio {i}",
    }
    d.update(overrides)
    return d


def personas8():
    return [persona_dict(i) for i in range(8)]


MBTIS = ["INTJ", "ESTP", "ISTJ", "ESFJ", "ENTP", "INFP", "ENFJ", "ISTP"]
COUNTRIES = ["United States", "United Kingdom", "Norway", "Indonesia", "Japan", "India", "Egypt", "Germany"]


def profile_item(i, **overrides):
    d = {"index": i, "name": NAMES[i], "age": 30 + i, "gender": "male" if i % 2 else "female",
         "mbti": MBTIS[i], "country": COUNTRIES[i],
         "posting_intensity": 1 + i % 5, "reactivity": 1 + (i + 1) % 5,
         "optimism": 1 + (i + 2) % 5, "influence_level": 1 + (i + 3) % 5}
    d.update(overrides)
    return d


def valid_enrichment(item_overrides_at=None):
    """item_overrides_at: {index: {field: value}}"""
    item_overrides_at = item_overrides_at or {}
    items = [profile_item(i, **item_overrides_at.get(i, {})) for i in range(8)]
    return json.dumps({"profiles": items})


def sample_articles():
    return [
        {"article_id": 11, "ticker": "NVDA", "headline": "Chip rally extends", "publisher": "Reuters",
         "published_at": "2025-06-27T10:00:00+00:00"},
        {"article_id": 12, "ticker": "JPM", "headline": "Banks pass stress test", "publisher": "Bloomberg",
         "published_at": "2025-06-26T10:00:00+00:00"},
        {"article_id": 13, "ticker": "XOM", "headline": "Oil slips", "publisher": "WSJ",
         "published_at": "2025-06-25T10:00:00+00:00"},
        {"article_id": 14, "ticker": "PFE", "headline": "Pharma cuts guidance", "publisher": "CNBC",
         "published_at": "2025-06-24T10:00:00+00:00"},
        {"article_id": 15, "ticker": "AAPL", "headline": "Fifth headline", "publisher": "FT",
         "published_at": "2025-06-23T10:00:00+00:00"},
    ]


def personas_result(**overrides):
    r = {
        "success": True, "run_id": "run-abc", "as_of_date": "2025-06-30",
        "screening": {"sectors": None, "market_cap_tiers": None},
        "generated_at": "2025-07-01T00:00:00+00:00", "grounding": "news", "article_ids": [11, 12, 13, 14, 15],
        "sample_articles": sample_articles(), "personas": personas8(), "from_cache": False,
    }
    r.update(overrides)
    return r


def enrichment_rows():
    conn = sqlite3.connect(pg.PERSONA_DB_PATH)
    try:
        return conn.execute("SELECT run_id FROM persona_oasis_enrichment").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


DEX = InvestorPersona(
    name="Dex Okonkwo-Hale",
    tagline="Rides whatever is going up and bails the second it cracks.",
    investment_philosophy=("Price is the only truth that pays. Dex buys strength, adds on breakouts and cuts "
                           "losers within days; fundamentals are a story people tell after the move. "
                           "Horizon is days to a few weeks."),
    philosophy_label="momentum chaser",
    biases=["recency bias", "FOMO on breakouts", "overconfidence after a winning streak"],
    personality=("Restless, competitive and quick to celebrate. Treats every trade like a scoreboard and gets "
                 "visibly rattled when a position stalls."),
    communication_style=("Short, loud posts with ALL-CAPS bursts and emojis like 🚀🔥. Announces entries and "
                         "exits in real time and rarely explains beyond the chart."),
    edge_vs_others="The only persona who acts purely on price momentum and is comfortable reversing within a day.",
    short_bio="Ex-options desk runner turned full-time retail trader based in London.",
)

DEX_EXPECTED = (
    "Dex Okonkwo-Hale is an independent investor who shares views on US stocks and market news on social media. "
    "Tagline: Rides whatever is going up and bails the second it cracks. "
    "Investment philosophy (momentum chaser): Price is the only truth that pays. Dex buys strength, adds on "
    "breakouts and cuts losers within days; fundamentals are a story people tell after the move. Horizon is days "
    "to a few weeks. "
    "Personality: Restless, competitive and quick to celebrate. Treats every trade like a scoreboard and gets "
    "visibly rattled when a position stalls. "
    "Communication style (how Dex Okonkwo-Hale writes posts): Short, loud posts with ALL-CAPS bursts and emojis "
    "like 🚀🔥. Announces entries and exits in real time and rarely explains beyond the chart. "
    "Known biases: recency bias; FOMO on breakouts; overconfidence after a winning streak. "
    "What sets Dex Okonkwo-Hale apart from the other investors: The only persona who acts purely on price "
    "momentum and is comfortable reversing within a day. "
    "Background: Ex-options desk runner turned full-time retail trader based in London."
)


def make_persona(name, **kw):
    base = dict(tagline="t", investment_philosophy="p", philosophy_label="l", biases=["a", "b"],
                personality="x", communication_style="y", edge_vs_others="z")
    base.update(kw)
    return InvestorPersona(name=name, **base)


# ---------------------------------------------------------------------------
# compose_persona_text
# ---------------------------------------------------------------------------

def test_compose_matches_design_document_example_exactly():
    assert ad.compose_persona_text(DEX) == DEX_EXPECTED


def test_compose_accepts_dict_from_get_or_generate_personas():
    from dataclasses import asdict
    assert ad.compose_persona_text(asdict(DEX)) == DEX_EXPECTED


def test_compose_without_short_bio_drops_background_entirely():
    text = ad.compose_persona_text(make_persona("Ann", short_bio=None))
    assert "Background" not in text
    assert text.endswith("What sets Ann apart from the other investors: z.")
    assert "  " not in text and "None" not in text


def test_compose_normalizes_whitespace_and_adds_terminal_punctuation():
    p = make_persona("Ann", personality="line one\n\n  line   two\ttab", edge_vs_others="Keeps its bang!")
    text = ad.compose_persona_text(p)
    assert "\n" not in text and "\t" not in text and "  " not in text
    assert "Personality: line one line two tab." in text        # '.' ditambahkan
    assert "What sets Ann apart from the other investors: Keeps its bang! " in text or text.endswith("Keeps its bang!")
    assert "bang!." not in text                                 # sudah berakhir tanda baca -> tidak ditambah


def test_compose_is_deterministic():
    assert ad.compose_persona_text(DEX) == ad.compose_persona_text(DEX)


# ---------------------------------------------------------------------------
# derive_usernames
# ---------------------------------------------------------------------------

def test_usernames_match_design_examples():
    names = ["Marisol Vance", "Dex Okonkwo-Hale", "Dr. Ilse Brandvold", "Tante Rina"]
    assert ad.derive_usernames([make_persona(n) for n in names]) == [
        "marisol_vance", "dex_okonkwo_hale", "dr_ilse_brandvold", "tante_rina"]


def test_username_collision_suffixes_only_the_later_one():
    got = ad.derive_usernames([make_persona("Marisol Vance"), make_persona("Ann"), make_persona("Marisol-Vance")])
    assert got == ["marisol_vance", "ann", "marisol_vance_2"]   # index 2 = persona kedua yang bertabrakan


def test_username_ascii_fold_length_and_fallback():
    got = ad.derive_usernames([make_persona("Zoë Ångström"), make_persona("李雷"), make_persona("A" * 50 + " B")])
    assert got[0] == "zoe_angstrom"
    assert got[1] == "investor"
    assert got[2] == "a" * 30 and len(got[2]) == 30


def test_username_repeated_collisions_stay_unique():
    got = ad.derive_usernames([make_persona("x y"), make_persona("x_y"), make_persona("x-y")])
    assert got == ["x_y", "x_y_1", "x_y_2"]


def test_username_suffix_itself_colliding_gets_extra_counter():
    # index 2 ("x!") -> "x" terpakai -> "x_2" terpakai (persona 1) -> "x_2_2"
    got = ad.derive_usernames([make_persona("x"), make_persona("x_2"), make_persona("x!")])
    assert got == ["x", "x_2", "x_2_2"]
    assert len(set(got)) == 3


# ---------------------------------------------------------------------------
# short_description
# ---------------------------------------------------------------------------

def test_short_description_prefers_short_bio_then_tagline():
    assert ad.short_description(make_persona("A", short_bio="the bio")) == "the bio"
    assert ad.short_description(make_persona("A", short_bio=None, tagline="the tagline")) == "the tagline"


def test_short_description_truncates_at_word_boundary_with_ellipsis():
    words = ["word%03d" % i for i in range(60)]
    long = " ".join(words)
    out = ad.short_description(make_persona("A", short_bio=long))
    assert out.endswith("…") and len(out) <= 150
    body = out[:-1]
    assert body.split(" ")[-1] in words          # berakhir di kata utuh, bukan potongan
    assert long.startswith(body)


def test_short_description_exactly_150_is_untouched():
    text = "x" * 150
    assert ad.short_description(make_persona("A", short_bio=text)) == text


def test_short_description_single_huge_word_is_hard_cut():
    out = ad.short_description(make_persona("A", short_bio="y" * 400))
    assert out == "y" * 147 + "…"


# ---------------------------------------------------------------------------
# get_or_generate_enrichment
# ---------------------------------------------------------------------------

def test_enrichment_success_is_persisted(llm):
    fake = llm([valid_enrichment()])
    result = ad.get_or_generate_enrichment("run-abc", personas8())
    assert result["success"] and result["from_cache"] is False and result["attempt"] == 1
    assert result["temperature_used"] == 1.0
    assert len(result["profiles"]) == 8 and result["profiles"][3]["name"] == "Tante Rina"
    assert enrichment_rows() == [("run-abc",)]
    assert len(fake.calls) == 1
    assert fake.calls[0]["response_format"] == {"type": "json_object"}
    system_prompt, user_prompt = fake.calls[0]["messages"][0]["content"], fake.calls[0]["messages"][1]["content"]
    assert "FICTIONAL" in system_prompt
    assert "[0] name: Marisol Vance" in user_prompt and "[7] name: Lena Fischer" in user_prompt
    assert "age: integer 21-85" in user_prompt and "influence_level 1-5" in user_prompt
    assert "at least 3 different countries" in user_prompt


def test_enrichment_cache_hit_does_not_call_llm_again(llm):
    fake = llm([valid_enrichment()])
    first = ad.get_or_generate_enrichment("run-abc", personas8())
    second = ad.get_or_generate_enrichment("run-abc", personas8())   # respons habis: panggilan kedua = IndexError
    assert len(fake.calls) == 1
    assert second["from_cache"] is True and second["profiles"] == first["profiles"]
    assert second["generated_at"] == first["generated_at"]


def test_enrichment_force_regenerate_appends_history(llm):
    fake = llm([valid_enrichment(), valid_enrichment({0: {"age": 55}})])
    ad.get_or_generate_enrichment("run-abc", personas8())
    forced = ad.get_or_generate_enrichment("run-abc", personas8(), force_regenerate=True)
    assert len(fake.calls) == 2 and forced["profiles"][0]["age"] == 55
    assert len(enrichment_rows()) == 2                      # append-only
    assert ad.get_or_generate_enrichment("run-abc", personas8())["profiles"][0]["age"] == 55   # terbaru dipakai


def test_enrichment_different_run_ids_are_separate(llm):
    fake = llm([valid_enrichment(), valid_enrichment()])
    ad.get_or_generate_enrichment("run-1", personas8())
    ad.get_or_generate_enrichment("run-2", personas8())
    assert len(fake.calls) == 2


def _retry_case(llm, bad_response):
    fake = llm([bad_response, valid_enrichment()])
    result = ad.get_or_generate_enrichment("run-abc", personas8())
    assert result["success"] and result["attempt"] == 2
    assert [c["temperature"] for c in fake.calls] == [1.0, 0.9]      # _temperature_for_attempt (Fase 10)
    assert len(enrichment_rows()) == 1
    return result


def test_enrichment_retries_when_index_out_of_order(llm):
    items = [profile_item(i) for i in range(8)]
    items[0]["index"], items[1]["index"] = 1, 0
    _retry_case(llm, json.dumps({"profiles": items}))


def test_enrichment_retries_when_name_does_not_match_original_order(llm):
    items = [profile_item(i) for i in range(8)]
    items[2]["name"], items[3]["name"] = items[3]["name"], items[2]["name"]   # index benar, nama tertukar
    _retry_case(llm, json.dumps({"profiles": items}))


def test_enrichment_name_match_is_case_and_space_insensitive(llm):
    llm([valid_enrichment({0: {"name": "  MARISOL vance "}})])
    result = ad.get_or_generate_enrichment("run-abc", personas8())
    assert result["success"] and result["attempt"] == 1
    assert result["profiles"][0]["name"] == "Marisol Vance"      # nama asli persona, bukan echo LLM


@pytest.mark.parametrize("age", [20, 86, "34", 34.5, True, None])
def test_enrichment_retries_on_bad_age(llm, age):
    _retry_case(llm, valid_enrichment({4: {"age": age}}))


@pytest.mark.parametrize("mbti", ["XXXX", "INT", "INTJX", "", None, 7])
def test_enrichment_retries_on_invalid_mbti(llm, mbti):
    _retry_case(llm, valid_enrichment({5: {"mbti": mbti}}))


@pytest.mark.parametrize("field,value", [
    ("gender", "robot"), ("country", "  "), ("country", None),
    ("posting_intensity", 0), ("reactivity", 6), ("optimism", "3"), ("influence_level", None),
])
def test_enrichment_retries_on_other_bad_fields(llm, field, value):
    _retry_case(llm, valid_enrichment({6: {field: value}}))


def test_enrichment_retries_on_wrong_length_missing_key_and_bad_json(llm):
    fake = llm([json.dumps({"profiles": [profile_item(i) for i in range(7)]}),
                json.dumps({"nope": []}),
                "not json at all {{{",
                valid_enrichment()])
    result = ad.get_or_generate_enrichment("run-abc", personas8(), max_attempts=4)
    assert result["success"] and result["attempt"] == 4
    assert len(fake.calls) == 4


def test_enrichment_normalizes_values(llm):
    llm([valid_enrichment({0: {"gender": " FEMALE ", "mbti": " intj ", "country": "USA", "age": 41.0}})])
    p0 = ad.get_or_generate_enrichment("run-abc", personas8())["profiles"][0]
    assert (p0["gender"], p0["mbti"], p0["country"], p0["age"]) == ("female", "INTJ", "United States", 41)


def test_enrichment_failure_after_3_attempts_writes_nothing(llm):
    bad = valid_enrichment({0: {"age": 5}})
    fake = llm([bad, bad, bad])
    result = ad.get_or_generate_enrichment("run-abc", personas8())
    assert result["success"] is False and "3 attempt" in result["error"] and "age=5" in result["error"]
    assert len(fake.calls) == 3
    assert [c["temperature"] for c in fake.calls] == [1.0, 0.9, 0.8]
    assert enrichment_rows() == []                                   # tidak ada baris tertulis


def test_enrichment_llm_exceptions_sleep_then_fail_without_fallback(llm, monkeypatch):
    sleeps = []
    monkeypatch.setattr(ad.time, "sleep", lambda s: sleeps.append(s))
    llm([RuntimeError("boom")] * 3)
    result = ad.get_or_generate_enrichment("run-abc", personas8())
    assert result["success"] is False and "boom" in result["error"]
    assert sleeps == [1, 2]                                          # tidak tidur setelah attempt terakhir
    assert enrichment_rows() == []


def test_enrichment_soft_checks_warn_but_do_not_retry(llm):
    same = {i: {"mbti": "INTJ", "country": "United States", "gender": "male", "optimism": 3} for i in range(8)}
    fake = llm([valid_enrichment(same)])
    result = ad.get_or_generate_enrichment("run-abc", personas8())
    assert result["success"] and len(fake.calls) == 1
    text = " | ".join(result["warnings"])
    assert "MBTI" in text and "negara" in text and "gender" in text and "optimism" in text


# ---------------------------------------------------------------------------
# map_activity
# ---------------------------------------------------------------------------

def test_map_activity_known_ratings_match_design_tables():
    profiles = [dict(profile_item(0), posting_intensity=5, reactivity=5, optimism=4, influence_level=4)]
    cfg = ad.map_activity(profiles * 1, [persona_dict(0)], "run-abc")[0]
    assert cfg["posts_per_hour"] == 1.0 and cfg["comments_per_hour"] == 2.0
    assert (cfg["response_delay_min"], cfg["response_delay_max"]) == (1, 10)
    assert cfg["sentiment_bias"] == 0.25 and cfg["influence_weight"] == 1.8
    assert cfg["stance"] == "neutral" and cfg["entity_type"] == "Investor"
    assert cfg["entity_name"] == "Marisol Vance" and cfg["agent_id"] == 0
    assert cfg["activity_level"] == 1.0 and cfg["active_hours"] == list(range(24))


def test_map_activity_low_end_and_all_table_rows():
    low = dict(profile_item(0), posting_intensity=1, reactivity=1, optimism=1, influence_level=1)
    cfg = ad.map_activity([low], [persona_dict(0)], "r")[0]
    assert (cfg["posts_per_hour"], cfg["comments_per_hour"]) == (0.2, 0.4)
    assert (cfg["response_delay_min"], cfg["response_delay_max"]) == (60, 240)
    assert cfg["sentiment_bias"] == -0.5 and cfg["influence_weight"] == 0.6
    delays = {r: ad._RESPONSE_DELAY[r] for r in range(1, 6)}
    assert delays == {5: (1, 10), 4: (3, 20), 3: (5, 45), 2: (15, 90), 1: (60, 240)}
    assert ad._INFLUENCE_WEIGHT == {1: 0.6, 2: 0.9, 3: 1.2, 4: 1.8, 5: 2.5}
    for level in range(1, 6):
        row = dict(profile_item(0), posting_intensity=level, optimism=level, reactivity=level, influence_level=level)
        c = ad.map_activity([row], [persona_dict(0)], "r")[0]
        assert c["posts_per_hour"] == round(0.2 * level, 1) and c["comments_per_hour"] == round(0.4 * level, 1)
        assert c["sentiment_bias"] == round((level - 3) * 0.25, 2)


def test_map_activity_activity_level_and_hours_do_not_depend_on_ratings():
    profiles = [profile_item(i) for i in range(8)]
    cfgs = ad.map_activity(profiles, personas8(), "run-abc")
    assert [c["agent_id"] for c in cfgs] == list(range(8))
    assert all(c["activity_level"] == 1.0 and c["active_hours"] == list(range(24)) for c in cfgs)
    assert len({c["entity_uuid"] for c in cfgs}) == 8
    assert cfgs[3]["entity_uuid"] == ad.map_activity(profiles, personas8(), "run-abc")[3]["entity_uuid"]
    assert cfgs[3]["entity_uuid"] != ad.map_activity(profiles, personas8(), "other-run")[3]["entity_uuid"]


# ---------------------------------------------------------------------------
# build_time_config
# ---------------------------------------------------------------------------

def test_time_config_yields_exactly_10_rounds_with_runner_formulas():
    tc = ad.build_time_config(8)
    assert (tc["total_simulation_hours"], tc["minutes_per_round"]) == (5, 30)
    assert (tc["agents_per_hour_min"], tc["agents_per_hour_max"]) == (8, 8)
    assert (tc["total_simulation_hours"] * 60) // tc["minutes_per_round"] == 10       # run_parallel_simulation.py
    assert int(tc["total_simulation_hours"] * 60 / tc["minutes_per_round"]) == 10     # simulation_runner.py
    assert tc["total_simulation_hours"] * 2 == 10                                     # action_logger.py total_rounds


def test_time_config_neutralizes_hour_multipliers():
    tc = ad.build_time_config(8)
    for key in ("peak_hours", "off_peak_hours", "morning_hours", "work_hours"):
        assert tc[key] == []                     # harus ada (bukan dihapus) supaya default MiroFish tidak dipakai
    for key in ("peak_activity_multiplier", "off_peak_activity_multiplier",
                "morning_activity_multiplier", "work_activity_multiplier"):
        assert tc[key] == 1.0


@pytest.mark.parametrize("n", [0, 1, 7, 9])
def test_time_config_rejects_non_8_agents(n):
    with pytest.raises(ValueError, match="8"):
        ad.build_time_config(n)


# ---------------------------------------------------------------------------
# build_event_config
# ---------------------------------------------------------------------------

def agent_cfgs(weights):
    return [{"agent_id": i, "influence_weight": w} for i, w in enumerate(weights)]


def test_event_config_four_articles_four_distinct_posters_ranked_by_influence():
    cfgs = agent_cfgs([1.2, 0.6, 2.5, 1.8, 0.9, 1.8, 0.6, 1.2])
    event, warnings = ad.build_event_config(personas8(), cfgs, sample_articles())
    posts = event["initial_posts"]
    assert warnings == [] and len(posts) == 4                       # 5 artikel -> maks 4
    assert len({p["poster_agent_id"] for p in posts}) == 4          # tiap post dari agent berbeda
    # ranking (-influence, agent_id): 2 (2.5), 3 (1.8), 5 (1.8), 0 (1.2)
    assert [p["poster_agent_id"] for p in posts] == [2, 3, 5, 0]
    assert [p["poster_type"] for p in posts] == ["Investor"] * 4
    assert posts[0]["content"] == "Chip rally extends $NVDA — via Reuters (2025-06-27)"
    assert posts[3]["content"].startswith("Pharma cuts guidance $PFE")
    assert all("Fifth headline" not in p["content"] for p in posts)
    assert event["scheduled_events"] == [] and event["hot_topics"] == [] and event["narrative_direction"] == ""


def test_event_config_one_article_per_ticker_first_by_published_desc():
    arts = [
        {"article_id": 1, "ticker": "AAA", "headline": "old", "publisher": "P", "published_at": "2025-06-01T00:00:00+00:00"},
        {"article_id": 2, "ticker": "AAA", "headline": "new", "publisher": "P", "published_at": "2025-06-20T00:00:00+00:00"},
        {"article_id": 3, "ticker": "BBB", "headline": "b", "publisher": "P", "published_at": "2025-06-10T00:00:00+00:00"},
    ]
    event, _ = ad.build_event_config(personas8(), agent_cfgs([1.0] * 8), arts)
    contents = [p["content"] for p in event["initial_posts"]]
    assert len(contents) == 2
    assert contents[0].startswith("new $AAA") and contents[1].startswith("b $BBB")
    assert not any(c.startswith("old") for c in contents)


def test_event_config_tie_on_published_at_breaks_by_article_id_asc():
    same = "2025-06-10T00:00:00+00:00"
    arts = [
        {"article_id": 9, "ticker": "T1", "headline": "nine", "publisher": "P", "published_at": same},
        {"article_id": 3, "ticker": "T2", "headline": "three", "publisher": "P", "published_at": same},
    ]
    event, _ = ad.build_event_config(personas8(), agent_cfgs([1.0] * 8), arts)
    assert [p["content"].split(" ")[0] for p in event["initial_posts"]] == ["three", "nine"]


def test_event_config_empty_publisher_drops_via_clause_only():
    arts = [{"article_id": 1, "ticker": "AAA", "headline": "Head", "publisher": "", "published_at": "2025-06-10T00:00:00+00:00"}]
    event, _ = ad.build_event_config(personas8(), agent_cfgs([1.0] * 8), arts)
    assert event["initial_posts"][0]["content"] == "Head $AAA (2025-06-10)"


@pytest.mark.parametrize("empty", [[], None])
def test_event_config_without_articles_uses_one_static_seed_and_warns(empty):
    cfgs = agent_cfgs([1.0, 2.5, 1.2, 0.6, 0.6, 0.6, 0.6, 0.6])
    event, warnings = ad.build_event_config(personas8(), cfgs, empty)
    assert warnings == ["no_news_seed"]
    assert event["initial_posts"] == [{
        "content": "Market check-in: what are you watching in US equities right now, and what's your read?",
        "poster_type": "Investor", "poster_agent_id": 1}]


# ---------------------------------------------------------------------------
# build_oasis_artifacts
# ---------------------------------------------------------------------------

def test_artifacts_persona_failure_returns_error_without_io(tmp_path, llm):
    fake = llm([])
    root = tmp_path / "sims"
    out = ad.build_oasis_artifacts({"success": False, "error": "screen_universe gagal"},
                                   simulation_id="sim_x", sim_root=str(root))
    assert out["success"] is False and "screen_universe gagal" in out["error"]
    assert not root.exists() and fake.calls == []


def test_artifacts_enrichment_failure_returns_error_without_io(tmp_path, llm):
    bad = valid_enrichment({0: {"mbti": "??"}})
    llm([bad, bad, bad])
    root = tmp_path / "sims"
    out = ad.build_oasis_artifacts(personas_result(), simulation_id="sim_x", sim_root=str(root))
    assert out["success"] is False and "enrichment gagal" in out["error"]
    assert not root.exists()
    assert enrichment_rows() == []


def test_artifacts_wrong_persona_count_returns_error_without_io(tmp_path, llm):
    llm([valid_enrichment()])
    root = tmp_path / "sims"
    out = ad.build_oasis_artifacts(personas_result(personas=personas8()[:7]), simulation_id="sim_x", sim_root=str(root))
    assert out["success"] is False and not root.exists()


def test_artifacts_invalid_simulation_id_is_rejected(tmp_path, llm):
    out = ad.build_oasis_artifacts(personas_result(), simulation_id="../evil", sim_root=str(tmp_path))
    assert out["success"] is False and "simulation_id" in out["error"]


def build_ok(tmp_path, llm, **kw):
    llm([valid_enrichment()])
    out = ad.build_oasis_artifacts(personas_result(**kw), simulation_id="sim_test", sim_root=str(tmp_path / "sims"))
    assert out["success"], out
    return out


def test_artifacts_success_writes_four_files_in_order_with_state_last(tmp_path, llm, monkeypatch):
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(ad.os, "replace", lambda src, dst: (replaced.append(os.path.basename(dst)), real_replace(src, dst))[1])
    out = build_ok(tmp_path, llm)
    assert replaced == ["twitter_profiles.csv", "reddit_profiles.json", "simulation_config.json", "state.json"]
    sim_dir = tmp_path / "sims" / "sim_test"
    assert out["sim_dir"] == str(sim_dir) and out["simulation_id"] == "sim_test"
    assert sorted(os.listdir(sim_dir)) == sorted(replaced)           # tidak ada file .tmp tersisa
    assert out["persona_run_id"] == "run-abc" and out["as_of_date"] == "2025-06-30"


def test_artifacts_state_json_matches_design(tmp_path, llm):
    build_ok(tmp_path, llm)
    state = json.loads((tmp_path / "sims" / "sim_test" / "state.json").read_text(encoding="utf-8"))
    assert state["config_generated"] is True and state["profiles_generated"] is True
    assert state["status"] == "ready" and state["simulation_id"] == "sim_test"
    assert state["project_id"] == "" and state["graph_id"] == ""
    assert state["enable_twitter"] is True and state["enable_reddit"] is True
    assert state["entities_count"] == 8 and state["profiles_count"] == 8 and state["entity_types"] == ["Investor"]
    assert state["current_round"] == 0 and state["twitter_status"] == "not_started" and state["reddit_status"] == "not_started"
    assert state["error"] is None
    from app.services.simulation_manager import SimulationState
    assert set(state) == set(SimulationState(simulation_id="s", project_id="", graph_id="").to_dict())   # bentuk to_dict


def test_artifacts_config_content_matches_design(tmp_path, llm):
    build_ok(tmp_path, llm)
    cfg = json.loads((tmp_path / "sims" / "sim_test" / "simulation_config.json").read_text(encoding="utf-8"))
    assert cfg["social_graph"] == {"follow_network": "complete"}
    assert cfg["persona_run"] == {"run_id": "run-abc", "as_of_date": "2025-06-30",
                                  "screening": {"sectors": None, "market_cap_tiers": None},
                                  "generated_at": "2025-07-01T00:00:00+00:00", "grounding": "news",
                                  "warnings": []}
    assert cfg["time_config"] == ad.build_time_config(8)
    assert cfg["project_id"] == "" and cfg["graph_id"] == ""
    assert "DEAD CONFIG" in cfg["generation_reasoning"] and "DO NOT affect the simulation" in cfg["generation_reasoning"]
    for platform_key in ("twitter_config", "reddit_config"):
        assert set(cfg[platform_key]) == {"platform", "recency_weight", "popularity_weight", "relevance_weight",
                                          "viral_threshold", "echo_chamber_strength"}
    assert len(cfg["event_config"]["initial_posts"]) == 4
    assert "api_key" not in json.dumps(cfg).lower()
    for a in cfg["agent_configs"]:
        assert set(a) == {"agent_id", "entity_uuid", "entity_name", "entity_type", "activity_level", "posts_per_hour",
                          "comments_per_hour", "active_hours", "response_delay_min", "response_delay_max",
                          "sentiment_bias", "stance", "influence_weight"}


def test_artifacts_agent_ids_consistent_across_csv_json_and_config(tmp_path, llm):
    build_ok(tmp_path, llm)
    sim_dir = tmp_path / "sims" / "sim_test"
    with (sim_dir / "twitter_profiles.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    reddit = json.loads((sim_dir / "reddit_profiles.json").read_text(encoding="utf-8"))
    cfg = json.loads((sim_dir / "simulation_config.json").read_text(encoding="utf-8"))

    assert [int(r["user_id"]) for r in rows] == list(range(8))
    assert [r["user_id"] for r in reddit] == list(range(8))
    assert [a["agent_id"] for a in cfg["agent_configs"]] == list(range(8))
    assert [r["name"] for r in rows] == [r["name"] for r in reddit] == [a["entity_name"] for a in cfg["agent_configs"]] == NAMES
    assert [r["username"] for r in rows] == [r["username"] for r in reddit]
    assert len({r["username"] for r in rows}) == 8
    for post in cfg["event_config"]["initial_posts"]:
        assert 0 <= post["poster_agent_id"] < 8


def test_artifacts_files_match_oasis_contract(tmp_path, llm):
    """Kolom/kunci yang benar-benar dibaca generate_twitter_agent_graph / generate_reddit_agent_graph."""
    build_ok(tmp_path, llm)
    sim_dir = tmp_path / "sims" / "sim_test"
    with (sim_dir / "twitter_profiles.csv").open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert reader.fieldnames == ["user_id", "name", "username", "user_char", "description"]
    assert all("\n" not in r["user_char"] and r["user_char"] and r["description"] for r in rows)
    assert rows[1]["user_char"] == ad.compose_persona_text(persona_dict(1))
    reddit = json.loads((sim_dir / "reddit_profiles.json").read_text(encoding="utf-8"))
    for r in reddit:
        assert {"persona", "mbti", "gender", "age", "country", "username", "bio"} <= set(r)
        assert isinstance(r["age"], int) and r["gender"] in ("male", "female", "other")
        assert r["persona"] == rows[r["user_id"]]["user_char"]
        assert len(r["bio"]) <= 150
    assert reddit[0]["mbti"] == "INTJ" and reddit[3]["country"] == "Indonesia"


def test_artifacts_no_news_seed_warning_flows_to_result(tmp_path, llm):
    out = build_ok(tmp_path, llm, sample_articles=[], grounding="none", article_ids=[])
    assert "event: no_news_seed" in out["warnings"]
    cfg = json.loads((tmp_path / "sims" / "sim_test" / "simulation_config.json").read_text(encoding="utf-8"))
    assert len(cfg["event_config"]["initial_posts"]) == 1


def test_artifacts_old_run_without_sample_articles_key_uses_fallback(tmp_path, llm):
    llm([valid_enrichment()])
    pr = personas_result()
    del pr["sample_articles"]                      # run lama: key tidak ada sama sekali
    out = ad.build_oasis_artifacts(pr, simulation_id="sim_old", sim_root=str(tmp_path))
    assert out["success"] and "event: no_news_seed" in out["warnings"]


def test_artifacts_default_simulation_id_pattern(tmp_path, llm):
    llm([valid_enrichment()])
    out = ad.build_oasis_artifacts(personas_result(), sim_root=str(tmp_path))
    assert out["success"] and out["simulation_id"].startswith("sim_") and len(out["simulation_id"]) == 16
    assert os.path.isdir(tmp_path / out["simulation_id"])


def test_artifacts_refuses_to_overwrite_existing_simulation(tmp_path, llm):
    build_ok(tmp_path, llm)
    llm([])   # tidak boleh sampai memanggil LLM baru (cache) maupun menimpa
    again = ad.build_oasis_artifacts(personas_result(), simulation_id="sim_test", sim_root=str(tmp_path / "sims"))
    assert again["success"] is False and "tidak menimpa" in again["error"]


def test_artifacts_write_failure_rolls_back_and_never_writes_state(tmp_path, llm, monkeypatch):
    llm([valid_enrichment()])
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:                         # gagal saat simulation_config.json
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(ad.os, "replace", flaky)
    out = ad.build_oasis_artifacts(personas_result(), simulation_id="sim_fail", sim_root=str(tmp_path / "sims"))
    assert out["success"] is False and "disk full" in out["error"]
    assert not (tmp_path / "sims" / "sim_fail").exists()          # rollback: tidak ada state.json, dir dibersihkan


def test_artifacts_accepts_get_or_generate_personas_dict_shape(tmp_path, llm):
    """personas berupa dict hasil asdict() (bentuk sebenarnya dari Fase 10), bukan InvestorPersona."""
    assert isinstance(personas_result()["personas"][0], dict)
    build_ok(tmp_path, llm)


# ---------------------------------------------------------------------------
# Warning: semua sumber, prefix, urutan, dan tertulis di FILE simulation_config.json
# ---------------------------------------------------------------------------

def uniform_enrichment():
    """Memicu soft-check enrichment (MBTI/negara/gender/rating seragam)."""
    same = {i: {"mbti": "INTJ", "country": "United States", "gender": "male", "optimism": 3} for i in range(8)}
    return valid_enrichment(same)


def written_config(tmp_path, sim_id="sim_test"):
    """Baca FILE yang benar-benar ditulis, bukan dict return."""
    return json.loads((tmp_path / "sims" / sim_id / "simulation_config.json").read_text(encoding="utf-8"))


def build_with(tmp_path, llm, enrichment_payload=None, sim_id="sim_test", **personas_overrides):
    if enrichment_payload is not None:
        llm([enrichment_payload])
    out = ad.build_oasis_artifacts(personas_result(**personas_overrides), simulation_id=sim_id, sim_root=str(tmp_path / "sims"))
    assert out["success"], out
    return out


def test_warnings_persona_source_gets_persona_prefix(tmp_path, llm):
    out = build_with(tmp_path, llm, valid_enrichment(),
                     warnings=["label filosofi duplikat: 'x'", "nama mirip archetype Fase 3: 'value'"])
    assert out["warnings"] == ["persona: label filosofi duplikat: 'x'", "persona: nama mirip archetype Fase 3: 'value'"]


def test_warnings_enrichment_source_gets_enrichment_prefix(tmp_path, llm):
    out = build_with(tmp_path, llm, uniform_enrichment())
    assert out["warnings"], "soft-check enrichment seharusnya menghasilkan warning"
    assert all(w.startswith("enrichment: ") for w in out["warnings"])
    text = " | ".join(out["warnings"])
    assert "MBTI" in text and "negara" in text and "gender" in text


def test_warnings_event_source_gets_event_prefix(tmp_path, llm):
    out = build_with(tmp_path, llm, valid_enrichment(), sample_articles=[], grounding="none")
    assert out["warnings"] == ["event: no_news_seed"]


def test_warnings_enrichment_not_persisted_is_an_explicit_warning_not_only_a_log(tmp_path, llm, monkeypatch):
    def boom(result):
        raise OSError("db locked")
    monkeypatch.setattr(ad, "_save_enrichment", boom)
    out = build_with(tmp_path, llm, valid_enrichment())
    assert out["warnings"] == ["enrichment: hasil enrichment gagal disimpan ke persistence (persisted=False)"]
    assert written_config(tmp_path)["persona_run"]["warnings"] == out["warnings"]     # juga di file, bukan hanya log


def test_warnings_from_all_sources_are_ordered_persona_enrichment_event(tmp_path, llm, monkeypatch):
    def boom(result):
        raise OSError("db locked")
    monkeypatch.setattr(ad, "_save_enrichment", boom)
    out = build_with(tmp_path, llm, uniform_enrichment(),
                     warnings=["p1", "p2"], sample_articles=[], grounding="none")
    warnings = out["warnings"]
    sources = [w.split(":", 1)[0] for w in warnings]
    assert sources[:2] == ["persona", "persona"] and sources[-1] == "event"
    assert set(sources[2:-1]) == {"enrichment"}
    # urutan blok persis persona -> enrichment -> event (tidak alfabetis, tidak tercampur)
    import itertools
    assert [k for k, _ in itertools.groupby(sources)] == ["persona", "enrichment", "event"]
    assert warnings[:2] == ["persona: p1", "persona: p2"]
    assert warnings[-2] == "enrichment: hasil enrichment gagal disimpan ke persistence (persisted=False)"   # setelah soft-check enrichment
    assert warnings[-1] == "event: no_news_seed"
    assert len(warnings) == 2 + len(ad._soft_checks([profile_item(i, mbti="INTJ", country="United States", gender="male", optimism=3) for i in range(8)])) + 1 + 1


def test_warnings_written_to_simulation_config_file_equal_returned_list(tmp_path, llm):
    out = build_with(tmp_path, llm, uniform_enrichment(), warnings=["p1"], sample_articles=[], grounding="none")
    persona_run = written_config(tmp_path)["persona_run"]                  # dibaca dari FILE hasil tulis
    assert persona_run["warnings"] == out["warnings"] and len(out["warnings"]) >= 3
    assert persona_run["warnings"][0] == "persona: p1" and persona_run["warnings"][-1] == "event: no_news_seed"
    # key lain di persona_run tetap utuh
    assert {"run_id", "as_of_date", "screening", "generated_at", "grounding"} <= set(persona_run)
    assert "warnings" not in written_config(tmp_path)                       # bukan field top-level terpisah


def test_no_warnings_writes_empty_list_not_missing_key(tmp_path, llm):
    out = build_with(tmp_path, llm, valid_enrichment())
    assert out["warnings"] == []
    persona_run = written_config(tmp_path)["persona_run"]
    assert "warnings" in persona_run and persona_run["warnings"] == []


def test_cached_enrichment_warnings_still_reach_result_and_file(tmp_path, llm):
    first = build_with(tmp_path, llm, uniform_enrichment(), sim_id="sim_a")
    assert first["warnings"] and all(w.startswith("enrichment: ") for w in first["warnings"])
    fake = llm([])                                                           # cache hit: LLM tidak boleh dipanggil
    second = build_with(tmp_path, llm, None, sim_id="sim_b")
    assert fake.calls == []
    assert second["warnings"] == first["warnings"]                           # tidak hilang, tidak ada 'persisted' palsu
    assert written_config(tmp_path, "sim_b")["persona_run"]["warnings"] == first["warnings"]


def test_personas_result_warnings_none_is_treated_as_empty(tmp_path, llm):
    out = build_with(tmp_path, llm, valid_enrichment(), warnings=None)
    assert out["warnings"] == []
