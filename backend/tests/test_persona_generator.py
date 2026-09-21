"""
Fase 10b — tes persona_generator.py (docs/design/fase10a_persona_generation.md).

Offline: get_news_data, screen_universe, LLM (create_chat_completion) dan
_get_llm_client di-patch; time.sleep di-patch; DB persona diisolasi lewat tmp_path.
"""

import json
from types import SimpleNamespace

import pytest

from app.services import persona_generator as pg


# ---------------------------------------------------------------------------
# Helper / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "PERSONA_DB_PATH", str(tmp_path / "persona_runs.db"))
    monkeypatch.setattr(pg.time, "sleep", lambda s: None)
    monkeypatch.setattr(pg, "_get_llm_client", lambda: object())


def make_universe(n, sectors=("Tech",)):
    return [
        {"ticker": f"T{i:03d}", "company_name": f"Co{i}", "gics_sector": sectors[i % len(sectors)],
         "market_cap": 1e12 - i, "market_cap_tier": "mega"}
        for i in range(n)
    ]


def art(article_id, published="2025-06-27T10:00:00+00:00", summary="s", publisher="Reuters"):
    return {"article_id": article_id, "headline": f"H{article_id}", "summary": summary,
            "publisher": publisher, "url": "http://x", "published_at": published,
            "related_ticker": "X"}


def patch_news(monkeypatch, per_ticker):
    """per_ticker: dict ticker -> list artikel | 'fail' | 'oor'; default 3 artikel unik."""
    counter = {"n": 0}

    def fake(ticker, as_of_date=None):
        spec = per_ticker.get(ticker) if isinstance(per_ticker, dict) else None
        if spec == "fail":
            return {"success": False, "error": "429", "articles": None}
        if spec == "oor":
            return {"success": True, "out_of_range": True, "articles": []}
        if spec is None:
            arts = []
            for k in range(4):
                counter["n"] += 1
                arts.append(art(counter["n"], published=f"2025-06-{10 + k:02d}T00:00:00+00:00"))
            spec = arts
        return {"success": True, "out_of_range": False, "articles": spec}

    monkeypatch.setattr(pg, "get_news_data", fake)


def persona_dict(i, label=None, name=None):
    return {
        "name": name or f"Persona {i}", "tagline": "t", "investment_philosophy": "p",
        "philosophy_label": label or f"label {i}", "biases": ["a", "b"],
        "personality": "x", "communication_style": "y", "edge_vs_others": "z",
    }


def valid_payload(n=8):
    return {"personas": [persona_dict(i) for i in range(n)]}


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
        content, finish = r if isinstance(r, tuple) else (r, "stop")
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=finish,
                                     message=SimpleNamespace(content=content))])


@pytest.fixture
def setup_generate(monkeypatch):
    def _setup(responses, universe=None, news=None):
        monkeypatch.setattr(pg, "screen_universe", lambda **kw: universe if universe is not None else make_universe(25, sectors=tuple('ABCDEF')))
        patch_news(monkeypatch, news or {})
        llm = FakeLLM(responses)
        monkeypatch.setattr(pg, "create_chat_completion", llm)
        monkeypatch.setattr(pg, "extract_chat_completion_text", lambda r: r.choices[0].message.content)
        return llm
    return _setup


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def test_sampling_sector_cap_5_of_20(monkeypatch):
    universe = make_universe(60, sectors=("Tech", "Health", "Fin"))
    patch_news(monkeypatch, {})
    selected = pg._select_tickers(universe)
    sector_of = {e["ticker"]: e["gics_sector"] for e in universe}
    counts = {}
    for t in selected:
        counts[sector_of[t]] = counts.get(sector_of[t], 0) + 1
    assert max(counts.values()) <= 5
    # 3 sektor x 5 = 15 < 20 -> hanya 15 yang bisa terpilih; cap benar-benar menahan
    assert len(selected) == 15


def test_sampling_20_tickers_with_enough_sectors_and_60_articles_max(monkeypatch):
    universe = make_universe(100, sectors=tuple(f"S{i}" for i in range(6)))
    patch_news(monkeypatch, {})
    selected = pg._select_tickers(universe)
    assert len(selected) == 20
    assert selected == [e["ticker"] for e in universe[:20]]  # urutan market cap tidak diubah
    lines, grounding = pg._sample_news_for_grounding(universe, None)
    assert grounding == "news" and len(lines) == 60


def test_sampling_universe_under_20_uses_all(monkeypatch):
    universe = make_universe(7, sectors=("A", "B", "C", "D", "E", "F", "G"))
    patch_news(monkeypatch, {})
    assert pg._select_tickers(universe) == [e["ticker"] for e in universe]
    lines, grounding = pg._sample_news_for_grounding(universe, None)
    assert grounding == "news" and len(lines) == 21


def test_sampling_dedup_article_id_across_tickers(monkeypatch):
    universe = make_universe(6, sectors=("A", "B", "C", "D", "E", "F"))
    shared = art(999)
    per = {e["ticker"]: [art(i * 10 + k) for k in range(3)] for i, e in enumerate(universe)}
    per["T000"] = [shared, art(1), art(2)]
    per["T001"] = [shared, art(3), art(4)]
    patch_news(monkeypatch, per)
    sample = pg._sample_articles(universe, None)
    ids = [a["article_id"] for a in sample]
    assert ids.count(999) == 1
    assert len(sample) == 6 * 3 - 1  # dibuang, TIDAK diganti artikel ke-4


def test_sampling_under_10_articles_grounding_none_and_no_news_block(monkeypatch):
    universe = make_universe(3, sectors=("A", "B", "C"))
    per = {e["ticker"]: [art(i * 10 + k) for k in range(3)] for i, e in enumerate(universe)}  # 9 artikel
    patch_news(monkeypatch, per)
    lines, grounding = pg._sample_news_for_grounding(universe, None)
    assert (lines, grounding) == ([], "none")
    _, user = pg._build_persona_prompt(lines, grounding)
    assert "MARKET NEWS SAMPLE" not in user
    assert "Use the news below" not in user


def test_prompt_with_news_contains_block_and_anti_collapse_rules(monkeypatch):
    system, user = pg._build_persona_prompt(["[2025-06-27] NVDA | Reuters | H"], "news")
    assert "MARKET NEWS SAMPLE (1 articles" in user and "NVDA" in user
    assert "at least 3 distinct values" in user
    flat = " ".join(user.split())
    assert "At least 2 must be unconventional" in flat
    assert "at least 1 must be strongly emotional or irrational" in flat
    assert "Do not use real people's names" in system


def test_sampling_out_of_range_all_tickers_grounding_none(monkeypatch):
    universe = make_universe(10, sectors=tuple("ABCDEFGHIJ"))
    patch_news(monkeypatch, {e["ticker"]: "oor" for e in universe})
    assert pg._sample_news_for_grounding(universe, "2020-01-01") == ([], "none")


def test_sampling_failed_ticker_skipped_not_replaced(monkeypatch):
    universe = make_universe(25, sectors=tuple("ABCDEFGH"))
    per = {"T000": "fail"}
    patch_news(monkeypatch, per)
    calls = []
    orig = pg.get_news_data
    monkeypatch.setattr(pg, "get_news_data", lambda t, as_of_date=None: (calls.append(t), orig(t, as_of_date))[1])
    sample = pg._sample_articles(universe, None)
    assert len(calls) == 20  # tidak ada ticker ke-21 sebagai pengganti
    assert "T000" not in {a["ticker"] for a in sample}
    assert len(sample) == 19 * 3


def test_line_format_and_none_summary(monkeypatch):
    a = {**art(1, summary=None), "ticker": "AAPL"}
    assert pg._format_article_line(a) == "[2025-06-27] AAPL | Reuters | H1"
    long = {**art(2, summary="x" * 500), "ticker": "AAPL"}
    assert pg._format_article_line(long).endswith(" | " + "x" * 200)


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

def test_generate_success_first_attempt(setup_generate):
    llm = setup_generate([json.dumps(valid_payload())])
    res = pg.generate_personas("2025-06-30")
    assert res["success"] and res["attempt"] == 1
    assert res["temperature_used"] == 1.0 and llm.calls[0]["temperature"] == 1.0
    assert llm.calls[0]["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in llm.calls[0]
    assert res["grounding"] == "news" and len(res["article_ids"]) == 60
    assert len(res["personas"]) == 8 and res["persisted"] is True


def test_retry_when_7_items_same_prompt_lower_temperature(setup_generate):
    llm = setup_generate([json.dumps(valid_payload(7)), json.dumps(valid_payload(8))])
    res = pg.generate_personas("2025-06-30")
    assert res["success"] and res["attempt"] == 2
    assert [c["temperature"] for c in llm.calls] == [1.0, 0.9]
    assert llm.calls[0]["messages"] == llm.calls[1]["messages"]  # regenerasi penuh, prompt sama


def test_retry_on_duplicate_names_after_casefold(setup_generate):
    bad = valid_payload()
    bad["personas"][1]["name"] = "Marcus Reid"
    bad["personas"][2]["name"] = "  marcus REID "
    llm = setup_generate([json.dumps(bad), json.dumps(valid_payload())])
    res = pg.generate_personas("2025-06-30")
    assert res["success"] and res["attempt"] == 2 and len(llm.calls) == 2


def test_retry_on_missing_field_and_bad_biases(setup_generate):
    p1 = valid_payload(); del p1["personas"][0]["tagline"]
    p2 = valid_payload(); p2["personas"][0]["biases"] = ["only one"]
    llm = setup_generate([json.dumps(p1), json.dumps(p2), json.dumps(valid_payload())])
    res = pg.generate_personas("2025-06-30")
    assert res["success"] and res["attempt"] == 3
    assert [c["temperature"] for c in llm.calls] == [1.0, 0.9, 0.8]


def test_truncated_json_is_repaired_then_fails_validation_and_retries(setup_generate):
    full = json.dumps(valid_payload())
    truncated = full[: len(full) // 2]  # terpotong di tengah -> < 8 item / item parsial
    llm = setup_generate([(truncated, "length"), json.dumps(valid_payload())])
    res = pg.generate_personas("2025-06-30")
    assert res["success"] and res["attempt"] == 2


def test_repair_json_closes_truncation_and_strips_fence():
    assert pg._repair_json('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert pg._repair_json('{"a": [{"b": "cut here') == {"a": [{"b": "cut here"}]}
    assert pg._repair_json("not json") is None


def test_api_exception_sleeps_and_retries(setup_generate, monkeypatch):
    sleeps = []
    monkeypatch.setattr(pg.time, "sleep", lambda s: sleeps.append(s))
    setup_generate([RuntimeError("boom"), json.dumps(valid_payload())])
    res = pg.generate_personas("2025-06-30")
    assert res["success"] and res["attempt"] == 2 and sleeps == [1]


def test_validation_failure_does_not_sleep(setup_generate, monkeypatch):
    sleeps = []
    monkeypatch.setattr(pg.time, "sleep", lambda s: sleeps.append(s))
    setup_generate([json.dumps(valid_payload(7)), json.dumps(valid_payload())])
    pg.generate_personas("2025-06-30")
    assert sleeps == []


def test_soft_check_duplicate_label_no_retry(setup_generate, caplog):
    payload = valid_payload()
    payload["personas"][0]["philosophy_label"] = "Value Investor"
    payload["personas"][1]["philosophy_label"] = " value  investor "
    llm = setup_generate([json.dumps(payload)])
    res = pg.generate_personas("2025-06-30")
    assert res["success"] and res["attempt"] == 1 and len(llm.calls) == 1
    assert any("philosophy_label duplikat" in w for w in res["warnings"])


def test_soft_check_archetype_name_warns_without_retry(setup_generate):
    payload = valid_payload()
    payload["personas"][0]["name"] = "Value Investor"
    llm = setup_generate([json.dumps(payload)])
    res = pg.generate_personas("2025-06-30")
    assert res["success"] and len(llm.calls) == 1
    assert any("archetype Fase 3" in w for w in res["warnings"])


def test_total_failure_no_fallback_no_partial(setup_generate):
    llm = setup_generate([json.dumps(valid_payload(7))] * 3)
    res = pg.generate_personas("2025-06-30")
    assert res["success"] is False and res["error"]
    assert "personas" not in res
    assert len(llm.calls) == 3
    assert pg._load_latest_run("2025-06-30", pg._screening_params(None, None)) is None  # tidak ada yang tersimpan


def test_no_stance_fields_anywhere(setup_generate):
    payload = valid_payload()
    payload["personas"][0].update({"stance": "BUY", "score": 0.9, "verdict": "x", "confidence": 1})
    setup_generate([json.dumps(payload)])
    res = pg.generate_personas("2025-06-30")
    forbidden = {"stance", "score", "verdict", "confidence"}
    for p in res["personas"]:
        assert not forbidden & set(p)
    assert not forbidden & set(pg.InvestorPersona.__dataclass_fields__)
    assert not forbidden & set(pg._load_latest_run("2025-06-30", pg._screening_params(None, None))["personas"][0])


def test_grounding_none_prompt_used_when_news_thin(setup_generate):
    universe = make_universe(3, sectors=("A", "B", "C"))
    llm = setup_generate([json.dumps(valid_payload())], universe=universe,
                         news={e["ticker"]: [art(i)] for i, e in enumerate(universe)})
    res = pg.generate_personas("2025-06-30")
    assert res["grounding"] == "none" and res["article_ids"] == []
    assert "MARKET NEWS SAMPLE" not in llm.calls[0]["messages"][1]["content"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_get_or_generate_reuses_stored_run(setup_generate):
    llm = setup_generate([json.dumps(valid_payload())])
    first = pg.get_or_generate_personas("2025-06-30")
    assert first["success"] and first["from_cache"] is False
    second = pg.get_or_generate_personas("2025-06-30")  # responses habis: panggilan LLM kedua akan IndexError
    assert len(llm.calls) == 1
    assert second["from_cache"] is True
    assert second["run_id"] == first["run_id"]
    assert second["personas"] == first["personas"]
    assert second["article_ids"] == first["article_ids"]
    assert second["temperature_used"] == first["temperature_used"]
    assert second["model"] == first["model"] and second["grounding"] == first["grounding"]


def test_force_regenerate_calls_llm_again_and_keeps_history(setup_generate):
    llm = setup_generate([json.dumps(valid_payload()), json.dumps(valid_payload())])
    first = pg.get_or_generate_personas("2025-06-30")
    forced = pg.get_or_generate_personas("2025-06-30", force_regenerate=True)
    assert len(llm.calls) == 2
    assert forced["run_id"] != first["run_id"] and forced["from_cache"] is False
    assert pg.get_or_generate_personas("2025-06-30")["run_id"] == forced["run_id"]  # terbaru dipakai
    import sqlite3
    conn = sqlite3.connect(pg.PERSONA_DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM persona_runs").fetchone()[0] == 2  # riwayat utuh
    conn.close()


def test_different_as_of_dates_are_separate(setup_generate):
    llm = setup_generate([json.dumps(valid_payload()), json.dumps(valid_payload())])
    a = pg.get_or_generate_personas("2025-06-30")
    b = pg.get_or_generate_personas("2025-05-30")
    assert len(llm.calls) == 2 and a["run_id"] != b["run_id"]


def test_failed_run_is_not_cached(setup_generate):
    llm = setup_generate([json.dumps(valid_payload(7))] * 3 + [json.dumps(valid_payload())])
    assert pg.get_or_generate_personas("2025-06-30")["success"] is False
    assert pg.get_or_generate_personas("2025-06-30")["success"] is True
    assert len(llm.calls) == 4


def test_universe_is_part_of_persistence_key(setup_generate, monkeypatch):
    llm = setup_generate([json.dumps(valid_payload()), json.dumps(valid_payload())])
    seen = []
    monkeypatch.setattr(pg, "screen_universe",
                        lambda **kw: (seen.append(kw), make_universe(25, sectors=tuple("ABCDEF")))[1])
    a = pg.get_or_generate_personas("2025-06-30", sectors=["Information Technology"])
    b = pg.get_or_generate_personas("2025-06-30", sectors=["Health Care"])
    assert len(llm.calls) == 2  # bukan 1 panggilan + cache hit yang salah
    assert a["run_id"] != b["run_id"] and a["universe_key"] != b["universe_key"]
    assert [kw["sectors"] for kw in seen] == [["Information Technology"], ["Health Care"]]
    import sqlite3
    conn = sqlite3.connect(pg.PERSONA_DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM persona_runs").fetchone()[0] == 2
    conn.close()
    # Masing-masing universe tetap bisa dipakai ulang untuk dirinya sendiri
    assert pg.get_or_generate_personas("2025-06-30", sectors=["Health Care"])["run_id"] == b["run_id"]
    assert len(llm.calls) == 2


def test_market_cap_tiers_also_part_of_key_and_order_insensitive(setup_generate):
    llm = setup_generate([json.dumps(valid_payload()), json.dumps(valid_payload())])
    a = pg.get_or_generate_personas("2025-06-30", market_cap_tiers=["mega", "large"])
    same = pg.get_or_generate_personas("2025-06-30", market_cap_tiers=["large", "mega"])
    other = pg.get_or_generate_personas("2025-06-30", market_cap_tiers=["mega"])
    assert same["run_id"] == a["run_id"] and same["from_cache"] is True
    assert other["run_id"] != a["run_id"]
    assert len(llm.calls) == 2
    # None (tanpa filter) berbeda dari filter apa pun
    assert pg._universe_key(pg._screening_params(None, None)) != a["universe_key"]


def test_old_schema_rows_are_migrated_and_never_reused(tmp_path, monkeypatch):
    import sqlite3
    conn = sqlite3.connect(pg.PERSONA_DB_PATH)
    conn.execute("""CREATE TABLE persona_runs (run_id TEXT PRIMARY KEY, as_of_key TEXT NOT NULL,
        generated_at TEXT NOT NULL, model TEXT NOT NULL, temperature_used REAL NOT NULL, attempt INTEGER NOT NULL,
        grounding TEXT NOT NULL, article_ids_json TEXT NOT NULL, personas_json TEXT NOT NULL, warnings_json TEXT NOT NULL)""")
    conn.execute("INSERT INTO persona_runs VALUES ('old','2025-06-30','t','m',1.0,1,'news','[]','[]','[]')")
    conn.commit(); conn.close()
    assert pg._load_latest_run("2025-06-30", pg._screening_params(None, None)) is None


def test_screen_universe_failure_stops_without_calling_llm(setup_generate, monkeypatch):
    llm = setup_generate([json.dumps(valid_payload())])
    def boom(**kw):
        raise RuntimeError("constituents unavailable")
    monkeypatch.setattr(pg, "screen_universe", boom)
    res = pg.generate_personas("2025-06-30")
    assert res["success"] is False and "screen_universe gagal" in res["error"]
    assert "personas" not in res
    assert llm.calls == []  # LLM tidak dipanggil sama sekali
    assert pg.get_or_generate_personas("2025-06-30")["success"] is False
    assert llm.calls == []


def test_empty_universe_without_error_continues_with_grounding_none(setup_generate):
    llm = setup_generate([json.dumps(valid_payload())], universe=[])
    res = pg.generate_personas("2025-06-30")
    assert res["success"] is True and res["grounding"] == "none"
    assert len(llm.calls) == 1
