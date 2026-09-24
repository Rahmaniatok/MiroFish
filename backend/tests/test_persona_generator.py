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
    # Tahap 4: _grounding_tokenizer adalah singleton module-level (di-load
    # sekali per proses, sengaja -- lihat build_grounding_from_graph). Reset di
    # setiap tes supaya tes yang satu tidak diam-diam mewarisi tokenizer
    # (asli/palsu) dari tes lain lewat state module-level yang dibagi.
    monkeypatch.setattr(pg, "_grounding_tokenizer", None)


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
    """Tahap 4 refactor: generate_personas() tidak lagi memanggil screen_universe
    sendiri (dipindah ke caller/build_grounding_from_news, Opsi a --
    docs/design/tahap4_persona_from_graph_design.md §1/§4). Fixture ini
    menyimpan `universe` di objek FakeLLM supaya helper `call_generate`/
    `call_get_or_generate` di bawah bisa membangun GroundingResult sendiri,
    persis meniru cara caller sungguhan sekarang wajib memanggilnya."""
    def _setup(responses, universe=None, news=None):
        patch_news(monkeypatch, news or {})
        llm = FakeLLM(responses)
        llm.universe = universe if universe is not None else make_universe(25, sectors=tuple('ABCDEF'))
        monkeypatch.setattr(pg, "create_chat_completion", llm)
        monkeypatch.setattr(pg, "extract_chat_completion_text", lambda r: r.choices[0].message.content)
        return llm
    return _setup


def call_generate(llm, as_of_date, **kwargs):
    """Bangun grounding dari llm.universe lalu panggil generate_personas() --
    menggantikan pola lama `pg.generate_personas(as_of_date)` yang membangun
    grounding sendiri di dalam fungsi."""
    grounding = pg.build_grounding_from_news(llm.universe, as_of_date)
    return pg.generate_personas(grounding, as_of_date, **kwargs)


def call_get_or_generate(llm, as_of_date, **kwargs):
    """Setara call_generate, untuk get_or_generate_personas()."""
    grounding = pg.build_grounding_from_news(llm.universe, as_of_date)
    return pg.get_or_generate_personas(grounding, as_of_date, **kwargs)


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
    result = pg.build_grounding_from_news(universe, None)
    assert result.grounding == "news" and result.source == "news" and len(result.news_lines) == 60


def test_sampling_universe_under_20_uses_all(monkeypatch):
    universe = make_universe(7, sectors=("A", "B", "C", "D", "E", "F", "G"))
    patch_news(monkeypatch, {})
    assert pg._select_tickers(universe) == [e["ticker"] for e in universe]
    result = pg.build_grounding_from_news(universe, None)
    assert result.grounding == "news" and len(result.news_lines) == 21


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
    result = pg.build_grounding_from_news(universe, None)
    assert (result.news_lines, result.grounding) == ([], "none")
    assert result.source == "news"
    assert result.provenance == {"article_ids": [], "sample_articles": []}
    _, user = pg._build_persona_prompt(result.news_lines, result.grounding)
    assert "MARKET CONTEXT SAMPLE" not in user
    assert "Use the market context below" not in user


def test_prompt_with_news_contains_block_and_anti_collapse_rules(monkeypatch):
    system, user = pg._build_persona_prompt(["[2025-06-27] NVDA | Reuters | H"], "news")
    assert "MARKET CONTEXT SAMPLE (1 items)" in user and "NVDA" in user
    assert "at least 3 distinct values" in user
    flat = " ".join(user.split())
    assert "At least 2 must be unconventional" in flat
    assert "at least 1 must be strongly emotional or irrational" in flat
    assert "Do not use real people's names" in system


def test_sampling_out_of_range_all_tickers_grounding_none(monkeypatch):
    universe = make_universe(10, sectors=tuple("ABCDEFGHIJ"))
    patch_news(monkeypatch, {e["ticker"]: "oor" for e in universe})
    result = pg.build_grounding_from_news(universe, "2020-01-01")
    assert (result.news_lines, result.grounding) == ([], "none")


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
    res = call_generate(llm, "2025-06-30")
    assert res["success"] and res["attempt"] == 1
    assert res["temperature_used"] == 1.0 and llm.calls[0]["temperature"] == 1.0
    assert llm.calls[0]["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in llm.calls[0]
    assert res["grounding"] == "news" and len(res["article_ids"]) == 60
    assert len(res["personas"]) == 8 and res["persisted"] is True


def test_retry_when_7_items_same_prompt_lower_temperature(setup_generate):
    llm = setup_generate([json.dumps(valid_payload(7)), json.dumps(valid_payload(8))])
    res = call_generate(llm, "2025-06-30")
    assert res["success"] and res["attempt"] == 2
    assert [c["temperature"] for c in llm.calls] == [1.0, 0.9]
    assert llm.calls[0]["messages"] == llm.calls[1]["messages"]  # regenerasi penuh, prompt sama


def test_retry_on_duplicate_names_after_casefold(setup_generate):
    bad = valid_payload()
    bad["personas"][1]["name"] = "Marcus Reid"
    bad["personas"][2]["name"] = "  marcus REID "
    llm = setup_generate([json.dumps(bad), json.dumps(valid_payload())])
    res = call_generate(llm, "2025-06-30")
    assert res["success"] and res["attempt"] == 2 and len(llm.calls) == 2


def test_retry_on_missing_field_and_bad_biases(setup_generate):
    p1 = valid_payload(); del p1["personas"][0]["tagline"]
    p2 = valid_payload(); p2["personas"][0]["biases"] = ["only one"]
    llm = setup_generate([json.dumps(p1), json.dumps(p2), json.dumps(valid_payload())])
    res = call_generate(llm, "2025-06-30")
    assert res["success"] and res["attempt"] == 3
    assert [c["temperature"] for c in llm.calls] == [1.0, 0.9, 0.8]


def test_truncated_json_is_repaired_then_fails_validation_and_retries(setup_generate):
    full = json.dumps(valid_payload())
    truncated = full[: len(full) // 2]  # terpotong di tengah -> < 8 item / item parsial
    llm = setup_generate([(truncated, "length"), json.dumps(valid_payload())])
    res = call_generate(llm, "2025-06-30")
    assert res["success"] and res["attempt"] == 2


def test_repair_json_closes_truncation_and_strips_fence():
    assert pg._repair_json('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert pg._repair_json('{"a": [{"b": "cut here') == {"a": [{"b": "cut here"}]}
    assert pg._repair_json("not json") is None


def test_api_exception_sleeps_and_retries(setup_generate, monkeypatch):
    sleeps = []
    monkeypatch.setattr(pg.time, "sleep", lambda s: sleeps.append(s))
    llm = setup_generate([RuntimeError("boom"), json.dumps(valid_payload())])
    res = call_generate(llm, "2025-06-30")
    assert res["success"] and res["attempt"] == 2 and sleeps == [1]


def test_validation_failure_does_not_sleep(setup_generate, monkeypatch):
    sleeps = []
    monkeypatch.setattr(pg.time, "sleep", lambda s: sleeps.append(s))
    llm = setup_generate([json.dumps(valid_payload(7)), json.dumps(valid_payload())])
    call_generate(llm, "2025-06-30")
    assert sleeps == []


def test_soft_check_duplicate_label_no_retry(setup_generate, caplog):
    payload = valid_payload()
    payload["personas"][0]["philosophy_label"] = "Value Investor"
    payload["personas"][1]["philosophy_label"] = " value  investor "
    llm = setup_generate([json.dumps(payload)])
    res = call_generate(llm, "2025-06-30")
    assert res["success"] and res["attempt"] == 1 and len(llm.calls) == 1
    assert any("philosophy_label duplikat" in w for w in res["warnings"])


def test_soft_check_archetype_name_warns_without_retry(setup_generate):
    payload = valid_payload()
    payload["personas"][0]["name"] = "Value Investor"
    llm = setup_generate([json.dumps(payload)])
    res = call_generate(llm, "2025-06-30")
    assert res["success"] and len(llm.calls) == 1
    assert any("archetype Fase 3" in w for w in res["warnings"])


def test_total_failure_no_fallback_no_partial(setup_generate):
    llm = setup_generate([json.dumps(valid_payload(7))] * 3)
    res = call_generate(llm, "2025-06-30")
    assert res["success"] is False and res["error"]
    assert "personas" not in res
    assert len(llm.calls) == 3
    assert pg._load_latest_run("2025-06-30", pg._screening_params(None, None)) is None  # tidak ada yang tersimpan


def test_no_stance_fields_anywhere(setup_generate):
    payload = valid_payload()
    payload["personas"][0].update({"stance": "BUY", "score": 0.9, "verdict": "x", "confidence": 1})
    llm = setup_generate([json.dumps(payload)])
    res = call_generate(llm, "2025-06-30")
    forbidden = {"stance", "score", "verdict", "confidence"}
    for p in res["personas"]:
        assert not forbidden & set(p)
    assert not forbidden & set(pg.InvestorPersona.__dataclass_fields__)
    assert not forbidden & set(pg._load_latest_run("2025-06-30", pg._screening_params(None, None))["personas"][0])


def test_grounding_none_prompt_used_when_news_thin(setup_generate):
    universe = make_universe(3, sectors=("A", "B", "C"))
    llm = setup_generate([json.dumps(valid_payload())], universe=universe,
                         news={e["ticker"]: [art(i)] for i, e in enumerate(universe)})
    res = call_generate(llm, "2025-06-30")
    assert res["grounding"] == "none" and res["article_ids"] == []
    assert "MARKET CONTEXT SAMPLE" not in llm.calls[0]["messages"][1]["content"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_get_or_generate_reuses_stored_run(setup_generate):
    llm = setup_generate([json.dumps(valid_payload())])
    first = call_get_or_generate(llm, "2025-06-30")
    assert first["success"] and first["from_cache"] is False
    second = call_get_or_generate(llm, "2025-06-30")  # responses habis: panggilan LLM kedua akan IndexError
    assert len(llm.calls) == 1
    assert second["from_cache"] is True
    assert second["run_id"] == first["run_id"]
    assert second["personas"] == first["personas"]
    assert second["article_ids"] == first["article_ids"]
    assert second["temperature_used"] == first["temperature_used"]
    assert second["model"] == first["model"] and second["grounding"] == first["grounding"]


def test_force_regenerate_calls_llm_again_and_keeps_history(setup_generate):
    llm = setup_generate([json.dumps(valid_payload()), json.dumps(valid_payload())])
    first = call_get_or_generate(llm, "2025-06-30")
    forced = call_get_or_generate(llm, "2025-06-30", force_regenerate=True)
    assert len(llm.calls) == 2
    assert forced["run_id"] != first["run_id"] and forced["from_cache"] is False
    assert call_get_or_generate(llm, "2025-06-30")["run_id"] == forced["run_id"]  # terbaru dipakai
    import sqlite3
    conn = sqlite3.connect(pg.PERSONA_DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM persona_runs").fetchone()[0] == 2  # riwayat utuh
    conn.close()


def test_different_as_of_dates_are_separate(setup_generate):
    llm = setup_generate([json.dumps(valid_payload()), json.dumps(valid_payload())])
    a = call_get_or_generate(llm, "2025-06-30")
    b = call_get_or_generate(llm, "2025-05-30")
    assert len(llm.calls) == 2 and a["run_id"] != b["run_id"]


def test_failed_run_is_not_cached(setup_generate):
    llm = setup_generate([json.dumps(valid_payload(7))] * 3 + [json.dumps(valid_payload())])
    assert call_get_or_generate(llm, "2025-06-30")["success"] is False
    assert call_get_or_generate(llm, "2025-06-30")["success"] is True
    assert len(llm.calls) == 4


def test_universe_is_part_of_persistence_key(setup_generate):
    # Tahap 4 refactor: sectors/market_cap_tiers sekarang HANYA mempengaruhi
    # cache key (_screening_params), bukan lagi argumen ke screen_universe yang
    # dipanggil dari dalam modul ini (sudah pindah ke caller) -- jadi tes ini
    # tidak lagi memonkeypatch/menginspeksi screen_universe, cukup memverifikasi
    # bahwa 2 nilai sectors berbeda menghasilkan run_id/universe_key berbeda.
    llm = setup_generate([json.dumps(valid_payload()), json.dumps(valid_payload())])
    a = call_get_or_generate(llm, "2025-06-30", sectors=["Information Technology"])
    b = call_get_or_generate(llm, "2025-06-30", sectors=["Health Care"])
    assert len(llm.calls) == 2  # bukan 1 panggilan + cache hit yang salah
    assert a["run_id"] != b["run_id"] and a["universe_key"] != b["universe_key"]
    import sqlite3
    conn = sqlite3.connect(pg.PERSONA_DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM persona_runs").fetchone()[0] == 2
    conn.close()
    # Masing-masing universe tetap bisa dipakai ulang untuk dirinya sendiri
    assert call_get_or_generate(llm, "2025-06-30", sectors=["Health Care"])["run_id"] == b["run_id"]
    assert len(llm.calls) == 2


def test_market_cap_tiers_also_part_of_key_and_order_insensitive(setup_generate):
    llm = setup_generate([json.dumps(valid_payload()), json.dumps(valid_payload())])
    a = call_get_or_generate(llm, "2025-06-30", market_cap_tiers=["mega", "large"])
    same = call_get_or_generate(llm, "2025-06-30", market_cap_tiers=["large", "mega"])
    other = call_get_or_generate(llm, "2025-06-30", market_cap_tiers=["mega"])
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


# NOTE (Tahap 4 refactor): `test_screen_universe_failure_stops_without_calling_llm`
# dihapus di sini, BUKAN diam-diam -- perilaku yang diujinya (generate_personas
# menangkap kegagalan screen_universe sendiri) sudah TIDAK ADA lagi secara sengaja
# (docs/design/tahap4_persona_from_graph_design.md §1/§4, Opsi a): screen_universe
# sekarang dipanggil CALLER, sebelum build_grounding_from_news/generate_personas
# — persis seperti build_universe_graph (Tahap 3) yang juga tidak menangkap
# kegagalan screen_universe secara khusus. Cakupan test untuk "screen_universe
# gagal -> caller berhenti sebelum panggil LLM" sekarang jadi tanggung jawab
# kode orkestrasi level-caller (belum ada di modul manapun sampai fase ini),
# bukan lagi milik persona_generator.py.


def test_empty_universe_without_error_continues_with_grounding_none(setup_generate):
    llm = setup_generate([json.dumps(valid_payload())], universe=[])
    res = call_generate(llm, "2025-06-30")
    assert res["success"] is True and res["grounding"] == "none"
    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# Fase 11a: sample_articles (headline) disimpan di metadata run
# ---------------------------------------------------------------------------

_SAMPLE_KEYS = {"article_id", "ticker", "headline", "publisher", "published_at"}


def test_generate_stores_sample_articles_with_expected_shape(setup_generate):
    llm = setup_generate([json.dumps(valid_payload())])
    result = call_generate(llm, "2025-06-30")
    assert result["success"] and result["grounding"] == "news"
    samples = result["sample_articles"]
    assert samples and len(samples) == len(result["article_ids"])
    assert [s["article_id"] for s in samples] == result["article_ids"]
    for s in samples:
        assert set(s) == _SAMPLE_KEYS
        assert s["ticker"].startswith("T") and s["headline"].startswith("H")
        assert s["publisher"] == "Reuters" and s["published_at"].startswith("2025-06-")


def test_sample_articles_roundtrip_through_persistence(setup_generate):
    llm = setup_generate([json.dumps(valid_payload())])
    first = call_get_or_generate(llm, "2025-06-30")
    second = call_get_or_generate(llm, "2025-06-30")
    assert second["from_cache"] is True
    assert second["sample_articles"] == first["sample_articles"] != []


def test_sample_articles_empty_when_grounding_none(setup_generate):
    llm = setup_generate([json.dumps(valid_payload())], universe=make_universe(1), news={"T000": [art(1)]})
    result = call_generate(llm, "2025-06-30")
    assert result["grounding"] == "none"
    assert result["sample_articles"] == [] and result["article_ids"] == []


def _seed_pre_sample_json_db(persona_count=8):
    """DB dengan skema SEBELUM kolom sample_json (universe_key/screening_json sudah ada)."""
    import sqlite3
    conn = sqlite3.connect(pg.PERSONA_DB_PATH)
    conn.execute("""CREATE TABLE persona_runs (run_id TEXT PRIMARY KEY, as_of_key TEXT NOT NULL,
        universe_key TEXT NOT NULL DEFAULT '', screening_json TEXT NOT NULL DEFAULT '{}',
        generated_at TEXT NOT NULL, model TEXT NOT NULL, temperature_used REAL NOT NULL, attempt INTEGER NOT NULL,
        grounding TEXT NOT NULL, article_ids_json TEXT NOT NULL, personas_json TEXT NOT NULL,
        warnings_json TEXT NOT NULL)""")
    screening = pg._screening_params(None, None)
    conn.execute(
        "INSERT INTO persona_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("old-run", "2025-06-30", pg._universe_key(screening), json.dumps(screening), "2025-07-01T00:00:00+00:00",
         "m", 1.0, 1, "news", "[1, 2, 3]",
         json.dumps([persona_dict(i) for i in range(persona_count)]), "[]"),
    )
    conn.commit()
    conn.close()


def test_old_run_without_sample_json_is_readable_with_empty_sample_articles(setup_generate):
    llm = setup_generate([])  # tidak boleh ada panggilan LLM
    _seed_pre_sample_json_db()
    result = call_get_or_generate(llm, "2025-06-30")
    assert result["success"] and result["from_cache"] is True
    assert result["run_id"] == "old-run"
    assert result["sample_articles"] == []          # fallback "tanpa headline", bukan crash
    assert result["article_ids"] == [1, 2, 3]
    assert len(result["personas"]) == 8
    assert llm.calls == []


def test_sample_json_migration_preserves_existing_rows():
    import sqlite3
    _seed_pre_sample_json_db()
    conn = pg._connect()   # menjalankan migrasi
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(persona_runs)")}
        assert "sample_json" in cols
        row = conn.execute(
            "SELECT run_id, universe_key, article_ids_json, sample_json FROM persona_runs").fetchone()
    finally:
        conn.close()
    assert row[0] == "old-run" and row[2] == "[1, 2, 3]"
    assert row[1] == pg._universe_key(pg._screening_params(None, None))   # data lama tidak berubah
    assert row[3] is None
    pg._connect().close()   # migrasi idempoten (kolom sudah ada -> tidak error)


# ---------------------------------------------------------------------------
# Tahap 4: build_grounding_from_graph (docs/design/tahap4_persona_from_graph_design.md §2)
# ---------------------------------------------------------------------------

class FakeTokenizer:
    """encode() = 1 'token' per karakter -- deterministik, tanpa model asli.
    Cukup untuk menguji ALGORITMA anggaran token; angka token nyata sudah
    diverifikasi terpisah di investigasi/desain lewat tokenizer Qwen asli."""
    def encode(self, text):
        return list(text)


def fake_entity(name, degree, labels=("Company",), summary="s"):
    return {
        "name": name,
        "labels": list(labels),
        "summary": summary,
        "related_edges": [{"edge_name": "EDGE", "fact": f"fact {i}"} for i in range(degree)],
    }


def test_build_grounding_from_graph_empty_entities_returns_none(monkeypatch):
    monkeypatch.setattr(pg, "_get_grounding_tokenizer", lambda: FakeTokenizer())
    result = pg.build_grounding_from_graph({"entities": []})
    assert result.grounding == "none" and result.source == "zep_graph"
    assert result.news_lines == []
    assert result.provenance == {
        "source_entity_count": 0, "source_entity_names": [],
        "source_grounding_tokens": 0, "filter_method_used": "top_degree_token_budget",
    }


def test_build_grounding_from_graph_missing_entities_key_returns_none_not_error(monkeypatch):
    monkeypatch.setattr(pg, "_get_grounding_tokenizer", lambda: FakeTokenizer())
    result = pg.build_grounding_from_graph({})
    assert result.grounding == "none"


def test_build_grounding_from_graph_all_entities_fit_under_budget(monkeypatch):
    """EDGE CASE 1 desain: universe kecil, total di bawah budget -> semua masuk."""
    monkeypatch.setattr(pg, "_get_grounding_tokenizer", lambda: FakeTokenizer())
    entities = [fake_entity(f"E{i}", degree=1) for i in range(5)]
    result = pg.build_grounding_from_graph({"entities": entities}, token_budget=100_000)
    assert result.grounding == "zep_graph"
    assert len(result.news_lines) == 5
    assert result.provenance["source_entity_count"] == 5
    assert set(result.provenance["source_entity_names"]) == {f"E{i}" for i in range(5)}


def test_build_grounding_from_graph_orders_by_degree_descending(monkeypatch):
    monkeypatch.setattr(pg, "_get_grounding_tokenizer", lambda: FakeTokenizer())
    entities = [
        fake_entity("Low", degree=1),
        fake_entity("High", degree=10),
        fake_entity("Mid", degree=5),
    ]
    result = pg.build_grounding_from_graph({"entities": entities}, token_budget=100_000)
    assert result.provenance["source_entity_names"] == ["High", "Mid", "Low"]


def test_build_grounding_from_graph_first_entity_always_included_even_over_budget(monkeypatch):
    """EDGE CASE 2 desain: entity pertama (degree tertinggi) SELALU masuk apa
    adanya walau sendirian > token_budget; entity kedua yang membuat total
    melebihi budget TIDAK masuk."""
    monkeypatch.setattr(pg, "_get_grounding_tokenizer", lambda: FakeTokenizer())
    huge = fake_entity("Huge", degree=10, summary="x" * 200)  # >> budget kecil di bawah
    small = fake_entity("Small", degree=1)
    result = pg.build_grounding_from_graph({"entities": [huge, small]}, token_budget=10)
    assert result.grounding == "zep_graph"
    assert result.provenance["source_entity_names"] == ["Huge"]
    assert result.provenance["source_grounding_tokens"] > 10  # sengaja melebihi budget
    assert "Small" not in result.provenance["source_entity_names"]


def test_build_grounding_from_graph_stops_entirely_does_not_skip_and_continue(monkeypatch):
    """STOP (desain §2) berarti loop berhenti total di entity pertama yang
    melebihi budget -- entity berikutnya yang lebih kecil TIDAK diperiksa lagi,
    bukan cuma dilewati lalu lanjut mencari entity lain yang muat."""
    monkeypatch.setattr(pg, "_get_grounding_tokenizer", lambda: FakeTokenizer())
    e1 = fake_entity("First", degree=3, summary="a" * 5)
    e2 = fake_entity("Second", degree=2, summary="b" * 500)  # jauh > sisa budget -> stop
    e3 = fake_entity("Third", degree=1, summary="c")  # kecil, TAPI tidak pernah diperiksa
    e1_tokens = len(pg._format_entity_text(e1))
    result = pg.build_grounding_from_graph(
        {"entities": [e1, e2, e3]}, token_budget=e1_tokens + 5
    )
    assert result.provenance["source_entity_names"] == ["First"]


def test_grounding_tokenizer_singleton_loaded_once(monkeypatch):
    import transformers

    calls = []

    class FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(model_id):
            calls.append(model_id)
            return FakeTokenizer()

    monkeypatch.setattr(transformers, "AutoTokenizer", FakeAutoTokenizer)
    entities = [fake_entity("A", degree=1)]
    pg.build_grounding_from_graph({"entities": entities}, token_budget=100_000)
    pg.build_grounding_from_graph({"entities": entities}, token_budget=100_000)
    assert len(calls) == 1
    assert calls[0] == pg._GROUNDING_TOKENIZER_MODEL_ID


# ---------------------------------------------------------------------------
# Tahap 4: cache key backward-compat (docs/design/tahap4_persona_from_graph_design.md §3)
# ---------------------------------------------------------------------------

def test_screening_params_news_hash_matches_pre_tahap4_baseline():
    """Regresi eksplisit: replikasi INDEPENDEN algoritma _screening_params/
    _universe_key SEBELUM Tahap 4 (tanpa grounding_source sama sekali) --
    bukan sekadar memanggil ulang fungsi yang sama secara sirkular."""
    import hashlib as _hashlib
    old_screening = {"sectors": ["Health Care", "Information Technology"], "market_cap_tiers": None}
    old_payload = json.dumps(old_screening, sort_keys=True, ensure_ascii=False)
    old_hash = _hashlib.sha256(old_payload.encode("utf-8")).hexdigest()[:16]

    new_screening = pg._screening_params(["Information Technology", "Health Care"], None)
    assert new_screening == old_screening  # grounding_source="news" tidak menambah key
    assert pg._universe_key(new_screening) == old_hash


def test_screening_params_zep_graph_hash_differs_from_news_for_same_sectors():
    news = pg._screening_params(["Energy"], None, grounding_source="news")
    zep = pg._screening_params(["Energy"], None, grounding_source="zep_graph")
    assert news != zep
    assert pg._universe_key(news) != pg._universe_key(zep)


# ---------------------------------------------------------------------------
# Tahap 4: persistence provenance_json (docs/design/tahap4_persona_from_graph_design.md §6)
# ---------------------------------------------------------------------------

def test_provenance_json_saved_for_zep_graph_and_null_for_news(setup_generate, monkeypatch):
    monkeypatch.setattr(pg, "_get_grounding_tokenizer", lambda: FakeTokenizer())

    llm = setup_generate([json.dumps(valid_payload())])
    news_result = call_generate(llm, "2025-06-30")
    assert news_result["success"]

    entities = [fake_entity("Acme", degree=3)]
    zep_grounding = pg.build_grounding_from_graph({"entities": entities}, token_budget=100_000)
    zep_grounding.provenance["source_graph_id"] = "mirofish_universe_test_abcd1234"
    setup_generate([json.dumps(valid_payload())])
    zep_result = pg.generate_personas(zep_grounding, as_of_date="2025-06-30", sectors=["Energy"])
    assert zep_result["success"]
    assert zep_result["source_graph_id"] == "mirofish_universe_test_abcd1234"
    assert zep_result["source_entity_count"] == 1
    assert zep_result["source_entity_names"] == ["Acme"]
    assert zep_result["filter_method_used"] == "top_degree_token_budget"

    import sqlite3
    conn = sqlite3.connect(pg.PERSONA_DB_PATH)
    rows = {row[0]: row[1] for row in conn.execute("SELECT run_id, provenance_json FROM persona_runs")}
    conn.close()
    assert rows[news_result["run_id"]] is None
    assert rows[zep_result["run_id"]] is not None
    saved = json.loads(rows[zep_result["run_id"]])
    assert saved["source_graph_id"] == "mirofish_universe_test_abcd1234"
    assert saved["source_entity_names"] == ["Acme"]


def test_old_run_without_provenance_json_column_is_readable(setup_generate):
    llm = setup_generate([])  # tidak boleh ada panggilan LLM
    _seed_pre_sample_json_db()  # skema ini juga belum punya provenance_json
    result = call_get_or_generate(llm, "2025-06-30")
    assert result["success"] and result["from_cache"] is True
    assert result["grounding_source"] == "news"
    assert result["source_graph_id"] is None
    assert result["source_entity_count"] == 0
    assert result["source_entity_names"] == []
    assert result["source_grounding_tokens"] == 0
    assert result["filter_method_used"] is None
    assert llm.calls == []


def test_provenance_json_migration_preserves_existing_rows():
    _seed_pre_sample_json_db()
    conn = pg._connect()   # menjalankan migrasi (nambah sample_json DAN provenance_json)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(persona_runs)")}
        assert "provenance_json" in cols
        row = conn.execute("SELECT run_id, provenance_json FROM persona_runs").fetchone()
    finally:
        conn.close()
    assert row[0] == "old-run" and row[1] is None
    pg._connect().close()   # migrasi idempoten (kolom sudah ada -> tidak error)


# ---------------------------------------------------------------------------
# Tahap 4: end-to-end (mock) build_grounding_from_graph -> generate_personas
# ---------------------------------------------------------------------------

def test_end_to_end_zep_graph_grounding_produces_all_design_fields(setup_generate, monkeypatch):
    monkeypatch.setattr(pg, "_get_grounding_tokenizer", lambda: FakeTokenizer())
    entities = [
        fake_entity("Ari Emanuel", degree=10, labels=["Person"]),
        fake_entity("Verizon", degree=10),
    ]
    grounding = pg.build_grounding_from_graph({"entities": entities}, token_budget=100_000)
    grounding.provenance["source_graph_id"] = "mirofish_universe_live_deadbeef"

    llm = setup_generate([json.dumps(valid_payload())])
    result = pg.generate_personas(
        grounding, as_of_date=None, sectors=["Energy", "Communication Services"]
    )

    assert result["success"] is True
    assert set(result.keys()) == {
        "success", "run_id", "as_of_date", "screening", "universe_key", "generated_at",
        "model", "temperature_used", "attempt", "grounding", "grounding_source",
        "article_ids", "sample_articles", "source_graph_id", "source_entity_count",
        "source_entity_names", "source_grounding_tokens", "filter_method_used",
        "personas", "warnings", "from_cache", "persisted",
    }
    assert result["grounding"] == "zep_graph" and result["grounding_source"] == "zep_graph"
    assert result["article_ids"] == [] and result["sample_articles"] == []
    assert result["source_graph_id"] == "mirofish_universe_live_deadbeef"
    assert result["source_entity_count"] == 2
    assert set(result["source_entity_names"]) == {"Ari Emanuel", "Verizon"}
    assert result["filter_method_used"] == "top_degree_token_budget"
    assert len(result["personas"]) == 8


# ---------------------------------------------------------------------------
# BUGFIX (verifikasi E2E 2026-09-24): _build_persona_prompt hardcode
# `grounding == "news"` membuat grounding="zep_graph" diam-diam jatuh ke
# jalur tanpa grounding sama sekali (12.935 token entity Zep tidak pernah
# sampai ke LLM). Tes SEBELUMNYA lolos karena hanya memeriksa BENTUK dict
# hasil, bukan ISI PROMPT yang benar-benar dikirim -- tes di bawah memeriksa
# string prompt itu sendiri, supaya kelas bug ini tertangkap otomatis.
# ---------------------------------------------------------------------------

def test_build_persona_prompt_includes_grounding_for_zep_graph_source():
    """Perbaikan inti: grounding='zep_graph' HARUS menyertakan news_lines di
    prompt, sama seperti grounding='news' -- sebelumnya kondisi hardcode
    'grounding == "news"' membuat 'zep_graph' diam-diam jatuh ke jalur tanpa
    grounding sama sekali."""
    entity_lines = [
        "Person: Ari Emanuel - Ari Emanuel is TKO's executive chair and CEO.",
        "Company: Verizon - Verizon reported a 24.4% increase in free cash flow in Q2.",
    ]
    _, user = pg._build_persona_prompt(entity_lines, "zep_graph")
    assert "Ari Emanuel is TKO's executive chair and CEO." in user
    assert "Verizon reported a 24.4% increase in free cash flow in Q2." in user
    assert "MARKET CONTEXT SAMPLE (2 items)" in user


def test_build_persona_prompt_news_source_behavior_unchanged():
    """Regresi: jalur 'news' (perilaku LAMA, sebelum bug ditemukan) tidak
    berubah oleh perbaikan kondisi `grounding != "none"`."""
    _, user = pg._build_persona_prompt(["[2025-06-27] NVDA | Reuters | H"], "news")
    assert "NVDA" in user
    assert "MARKET CONTEXT SAMPLE (1 items)" in user
    _, user_empty = pg._build_persona_prompt([], "news")
    # news_lines kosong -> tetap TIDAK menyertakan blok (kondisi `and news_lines`
    # tidak berubah, hanya perbandingan grounding-nya yang diperluas).
    assert "MARKET CONTEXT SAMPLE" not in user_empty


def test_build_persona_prompt_none_excludes_grounding_even_with_nonempty_news_lines():
    """grounding='none' HARUS tetap mengecualikan blok grounding APAPUN isi
    news_lines-nya -- 'none' adalah satu-satunya nilai yang sengaja
    dikecualikan oleh kondisi `grounding != "none"`."""
    entity_lines = ["Company: Acme - some real fact that must not leak into the prompt."]
    _, user = pg._build_persona_prompt(entity_lines, "none")
    assert "MARKET CONTEXT SAMPLE" not in user
    assert "Acme" not in user


def test_generate_personas_integration_prompt_actually_contains_zep_entities(monkeypatch):
    """TEST INTEGRASI -- capture ARGUMEN NYATA yang diteruskan ke
    create_chat_completion (bukan cuma mock return value-nya), buktikan
    grounding.news_lines dari build_grounding_from_graph BENAR-BENAR sampai
    ke prompt yang dikirim LLM. Ini test yang seharusnya sudah ada sejak
    implementasi Tahap 4 -- ditambahkan sekarang setelah bug ditemukan
    verifikasi E2E 2026-09-24. Test ini GAGAL pada kode sebelum bugfix ini
    (prompt yang di-capture tidak akan mengandung "Ari Emanuel"/"Verizon")."""
    monkeypatch.setattr(pg, "_get_grounding_tokenizer", lambda: FakeTokenizer())
    entities = [
        fake_entity("Ari Emanuel", degree=10, labels=["Person"],
                    summary="TKO's executive chair and CEO"),
        fake_entity("Verizon", degree=8,
                    summary="reported a 24.4% increase in free cash flow"),
    ]
    grounding = pg.build_grounding_from_graph({"entities": entities}, token_budget=100_000)
    assert grounding.grounding == "zep_graph"  # sanity: bukan "none"

    llm = FakeLLM([json.dumps(valid_payload())])
    monkeypatch.setattr(pg, "create_chat_completion", llm)
    monkeypatch.setattr(pg, "extract_chat_completion_text", lambda r: r.choices[0].message.content)

    result = pg.generate_personas(grounding, as_of_date=None, sectors=["Energy"])
    assert result["success"] is True

    sent_user_prompt = llm.calls[0]["messages"][1]["content"]
    assert "Ari Emanuel" in sent_user_prompt
    assert "TKO's executive chair and CEO" in sent_user_prompt
    assert "Verizon" in sent_user_prompt
    assert "reported a 24.4% increase in free cash flow" in sent_user_prompt
    assert "MARKET CONTEXT SAMPLE" in sent_user_prompt
