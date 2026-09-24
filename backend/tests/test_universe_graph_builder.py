"""
Tahap 3 (Feed ke Zep) — tes `universe_graph_builder.py`
(docs/design/tahap3_zep_feed_design.md).

Semua tes offline: Zep Cloud dan Finnhub di-monkeypatch, TIDAK ada panggilan
API sungguhan. Verifikasi limit ontology tier Flex (Langkah 0 desain) adalah
uji manual TERPISAH terhadap Zep Cloud sungguhan, tidak diulang di sini.
"""

import pytest

from app.services import universe_graph_builder as ugb


# ---------------------------------------------------------------------------
# Fakes -- merekam panggilan ke SATU shared `calls` list supaya urutan lintas
# GraphBuilderService dan ZepEntityReader bisa diverifikasi bersama.
# ---------------------------------------------------------------------------


class _FakeSubmission:
    def __init__(self, item_count):
        self.item_count = item_count


class _FakeBuilder:
    def __init__(
        self,
        calls,
        *,
        create_graph_error=None,
        set_ontology_error=None,
        add_text_batches_error=None,
        wait_error=None,
        delete_graph_error=None,
    ):
        self._calls = calls
        self.create_graph_error = create_graph_error
        self.set_ontology_error = set_ontology_error
        self.add_text_batches_error = add_text_batches_error
        self.wait_error = wait_error
        self.delete_graph_error = delete_graph_error
        self.last_timeout = None

    def create_graph(self, name, *, graph_id):
        self._calls.append(("create_graph", graph_id))
        if self.create_graph_error:
            raise self.create_graph_error
        return graph_id

    def set_ontology(self, graph_id, ontology):
        self._calls.append(("set_ontology", graph_id))
        if self.set_ontology_error:
            raise self.set_ontology_error

    def add_text_batches(self, graph_id, chunks):
        self._calls.append(("add_text_batches", graph_id, len(chunks)))
        if self.add_text_batches_error:
            raise self.add_text_batches_error
        return _FakeSubmission(len(chunks))

    def _wait_for_batch(self, submission, timeout):
        self._calls.append(("wait_for_batch", timeout))
        self.last_timeout = timeout
        if self.wait_error:
            raise self.wait_error

    def delete_graph(self, graph_id):
        self._calls.append(("delete_graph", graph_id))
        if self.delete_graph_error:
            raise self.delete_graph_error


class _FakeFilteredEntities:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class _FakeEntityReader:
    def __init__(self, calls, *, entities_result=None, error=None):
        self._calls = calls
        self._entities_result = entities_result
        self._error = error

    def filter_defined_entities(self, graph_id):
        self._calls.append(("filter_defined_entities", graph_id))
        if self._error:
            raise self._error
        return self._entities_result


def _news_result(articles=None, success=True, error=None):
    return {
        "success": success,
        "error": error,
        "articles": articles if articles is not None else [],
    }


def _article(article_id, headline="H", summary="S", published_at="2026-01-01T00:00:00Z"):
    return {
        "article_id": article_id,
        "headline": headline,
        "summary": summary,
        "publisher": "Reuters",
        "published_at": published_at,
        "url": "https://example.com",
    }


def _wire_universe_graph_success_deps(
    monkeypatch,
    calls,
    *,
    tickers=("AAA",),
    get_news_data_fn=None,
    builder=None,
    reader=None,
):
    """Pasang default fakes untuk build_universe_graph -- tiap test bisa
    override sebagian lewat parameter."""
    monkeypatch.setattr(
        ugb, "screen_universe", lambda **kw: [{"ticker": t} for t in tickers]
    )
    if get_news_data_fn is None:
        def get_news_data_fn(ticker, as_of_date=None):
            return _news_result(articles=[_article(article_id=1)])
    monkeypatch.setattr(ugb, "get_news_data", get_news_data_fn)

    if builder is None:
        builder = _FakeBuilder(calls)
    monkeypatch.setattr(ugb, "GraphBuilderService", lambda *a, **kw: builder)

    if reader is None:
        reader = _FakeEntityReader(
            calls,
            entities_result=_FakeFilteredEntities(
                {"entities": [], "entity_types": [], "total_count": 0, "filtered_count": 0}
            ),
        )
    monkeypatch.setattr(ugb, "ZepEntityReader", lambda *a, **kw: reader)

    return builder, reader


# ---------------------------------------------------------------------------
# compute_timeout
# ---------------------------------------------------------------------------


def test_compute_timeout_matches_design_doc_129_items():
    assert ugb.compute_timeout(129) == 1348


def test_compute_timeout_matches_design_doc_1500_items():
    assert ugb.compute_timeout(1500) == 5535


def test_compute_timeout_floor_is_specified_but_unreachable_for_valid_item_counts():
    """Desain §1 menspesifikasikan `max(600, ...)` sebagai floor untuk
    item_count kecil. TEMUAN (dilaporkan, bukan diperbaiki sendiri --
    formula ini sudah final/disetujui): dengan model_wait(n) = 476,9 + 1,527n
    dan margin x2, bahkan di n=0 hasilnya 2*476,9 = 953,8 -> ceil 954, yang
    SUDAH di atas 600. Floor ini secara matematis TIDAK PERNAH aktif untuk
    item_count non-negatif berapapun -- kode tetap mengikuti formula desain
    verbatim (termasuk floor-nya, sebagai proteksi harfiah sesuai dokumen),
    tes ini mendokumentasikan bahwa floor itu saat ini dead code."""
    assert ugb.compute_timeout(0) == 954
    assert ugb.compute_timeout(1) == 957


def test_compute_timeout_rejects_negative_item_count():
    with pytest.raises(ValueError):
        ugb.compute_timeout(-1)


# ---------------------------------------------------------------------------
# fetch_news_for_universe
# ---------------------------------------------------------------------------


def test_fetch_news_success_on_first_try_does_not_retry(monkeypatch):
    call_count = {"AAA": 0}

    def fake_get_news_data(ticker, as_of_date=None):
        call_count[ticker] += 1
        return _news_result(articles=[_article(article_id=1)])

    monkeypatch.setattr(ugb, "get_news_data", fake_get_news_data)

    news_items, failed = ugb.fetch_news_for_universe(["AAA"])

    assert call_count["AAA"] == 1
    assert len(news_items) == 1
    assert failed == []


def test_fetch_news_retries_exactly_once_with_no_delay(monkeypatch):
    call_count = {"AAA": 0}
    sleep_calls = []
    monkeypatch.setattr(ugb.time, "sleep", lambda *_a, **_kw: sleep_calls.append(True))

    def fake_get_news_data(ticker, as_of_date=None):
        call_count[ticker] += 1
        if call_count[ticker] == 1:
            return _news_result(success=False, error="timeout")
        return _news_result(articles=[_article(article_id=1)])

    monkeypatch.setattr(ugb, "get_news_data", fake_get_news_data)

    news_items, failed = ugb.fetch_news_for_universe(["AAA"])

    assert call_count["AAA"] == 2
    assert sleep_calls == []
    assert len(news_items) == 1
    assert failed == []


def test_fetch_news_fails_both_attempts_does_not_block_other_tickers(monkeypatch):
    call_count = {"AAA": 0, "BBB": 0}

    def fake_get_news_data(ticker, as_of_date=None):
        call_count[ticker] += 1
        if ticker == "AAA":
            return _news_result(success=False, error="permanent failure")
        return _news_result(articles=[_article(article_id=2)])

    monkeypatch.setattr(ugb, "get_news_data", fake_get_news_data)

    news_items, failed = ugb.fetch_news_for_universe(["AAA", "BBB"])

    assert call_count["AAA"] == 2
    assert call_count["BBB"] == 1
    assert len(failed) == 1
    assert failed[0].ticker == "AAA"
    assert failed[0].error == "permanent failure"
    assert len(news_items) == 1
    assert news_items[0].ticker == "BBB"


def test_fetch_news_dedups_article_id_across_different_tickers(monkeypatch):
    # Kasus nyata NWSA/NWS: dua ticker, artikel identik (article_id sama).
    def fake_get_news_data(ticker, as_of_date=None):
        return _news_result(articles=[_article(article_id=999)])

    monkeypatch.setattr(ugb, "get_news_data", fake_get_news_data)

    news_items, failed = ugb.fetch_news_for_universe(["NWSA", "NWS"])

    assert failed == []
    matching = [item for item in news_items if item.article_id == 999]
    assert len(matching) == 1
    assert matching[0].ticker == "NWSA"  # ticker pertama dalam urutan yang menang


def test_fetch_news_empty_tickers_raises_value_error():
    with pytest.raises(ValueError):
        ugb.fetch_news_for_universe([])


# ---------------------------------------------------------------------------
# build_zep_batch
# ---------------------------------------------------------------------------


def test_build_zep_batch_includes_published_at_and_summary():
    items = [
        ugb.NewsItem(
            ticker="AAPL",
            headline="Apple hits record high",
            summary="Shares rose 5%.",
            publisher="Reuters",
            published_at="2026-01-15T00:00:00Z",
            article_id=1,
            url="https://example.com",
        )
    ]
    chunks = ugb.build_zep_batch(items)
    assert len(chunks) == 1
    assert chunks[0] == "[2026-01-15T00:00:00Z] AAPL: Apple hits record high. Shares rose 5%."


def test_build_zep_batch_drops_summary_when_none_without_literal_none():
    items = [
        ugb.NewsItem(
            ticker="AAPL",
            headline="Apple hits record high",
            summary=None,
            publisher="Reuters",
            published_at="2026-01-15T00:00:00Z",
            article_id=1,
            url="https://example.com",
        )
    ]
    chunks = ugb.build_zep_batch(items)
    assert chunks[0] == "[2026-01-15T00:00:00Z] AAPL: Apple hits record high."
    assert "None" not in chunks[0]


# ---------------------------------------------------------------------------
# build_universe_graph
# ---------------------------------------------------------------------------


def test_build_universe_graph_empty_universe_returns_error_before_zep_calls(monkeypatch):
    # screen_universe kosong -- bukan kasus eksplisit di §8 desain, diperlakukan
    # setara dengan "semua ticker gagal fetch" (lihat komentar di kode).
    calls = []
    monkeypatch.setattr(ugb, "screen_universe", lambda **kw: [])
    builder = _FakeBuilder(calls)
    monkeypatch.setattr(ugb, "GraphBuilderService", lambda *a, **kw: builder)

    result = ugb.build_universe_graph(as_of_date=None)

    assert result["success"] is False
    assert result["item_count"] == 0
    assert "filtered_entities" not in result
    assert calls == []


def test_build_universe_graph_all_tickers_fail_fetch_never_calls_zep(monkeypatch):
    calls = []

    def fake_get_news_data(ticker, as_of_date=None):
        return _news_result(success=False, error="network error")

    _wire_universe_graph_success_deps(
        monkeypatch, calls, tickers=("AAA", "BBB"), get_news_data_fn=fake_get_news_data
    )

    result = ugb.build_universe_graph(as_of_date=None)

    assert result["success"] is False
    assert result["item_count"] == 0
    assert len(result["failed_tickers"]) == 2
    assert "filtered_entities" not in result
    assert calls == []  # GraphBuilderService/ZepEntityReader NEVER touched


def test_build_universe_graph_create_graph_fails_no_cleanup_attempted(monkeypatch):
    calls = []
    builder = _FakeBuilder(calls, create_graph_error=RuntimeError("create boom"))
    _wire_universe_graph_success_deps(monkeypatch, calls, builder=builder)

    result = ugb.build_universe_graph(as_of_date=None)

    assert result["success"] is False
    assert "create boom" in result["error"]
    assert "filtered_entities" not in result
    assert [c[0] for c in calls] == ["create_graph"]
    assert "delete_graph" not in [c[0] for c in calls]


def test_build_universe_graph_set_ontology_fails_attempts_cleanup_and_keeps_original_error(monkeypatch):
    calls = []
    builder = _FakeBuilder(
        calls,
        set_ontology_error=RuntimeError("ontology boom"),
        delete_graph_error=RuntimeError("delete also boom"),
    )
    _wire_universe_graph_success_deps(monkeypatch, calls, builder=builder)

    result = ugb.build_universe_graph(as_of_date=None)

    assert result["success"] is False
    assert "ontology boom" in result["error"]
    assert "delete also boom" not in result["error"]
    assert "filtered_entities" not in result
    assert [c[0] for c in calls] == ["create_graph", "set_ontology", "delete_graph"]


def test_build_universe_graph_add_text_batches_fails_cleans_up(monkeypatch):
    calls = []
    builder = _FakeBuilder(calls, add_text_batches_error=RuntimeError("submit boom"))
    _wire_universe_graph_success_deps(monkeypatch, calls, builder=builder)

    result = ugb.build_universe_graph(as_of_date=None)

    assert result["success"] is False
    assert "submit boom" in result["error"]
    assert "filtered_entities" not in result
    assert [c[0] for c in calls] == ["create_graph", "set_ontology", "add_text_batches", "delete_graph"]


def test_build_universe_graph_partial_batch_status_fails_fast_and_cleans_up(monkeypatch):
    calls = []
    builder = _FakeBuilder(calls, wait_error=RuntimeError("batch ended as partial"))
    _wire_universe_graph_success_deps(monkeypatch, calls, builder=builder)

    result = ugb.build_universe_graph(as_of_date=None)

    assert result["success"] is False
    assert "filtered_entities" not in result
    assert [c[0] for c in calls] == [
        "create_graph",
        "set_ontology",
        "add_text_batches",
        "wait_for_batch",
        "delete_graph",
    ]


def test_build_universe_graph_timeout_error_is_treated_identically_to_partial(monkeypatch):
    calls_partial = []
    builder_partial = _FakeBuilder(calls_partial, wait_error=RuntimeError("batch ended as partial"))
    _wire_universe_graph_success_deps(monkeypatch, calls_partial, builder=builder_partial)
    result_partial = ugb.build_universe_graph(as_of_date=None)

    calls_timeout = []
    builder_timeout = _FakeBuilder(calls_timeout, wait_error=TimeoutError("ingest timed out"))
    _wire_universe_graph_success_deps(monkeypatch, calls_timeout, builder=builder_timeout)
    result_timeout = ugb.build_universe_graph(as_of_date=None)

    assert result_partial["success"] is False
    assert result_timeout["success"] is False
    assert set(result_partial.keys()) == set(result_timeout.keys())
    assert "filtered_entities" not in result_partial
    assert "filtered_entities" not in result_timeout
    assert [c[0] for c in calls_partial] == [c[0] for c in calls_timeout]


def test_build_universe_graph_entity_read_fails_after_successful_batch_still_cleans_up(monkeypatch):
    calls = []
    builder = _FakeBuilder(calls)
    reader = _FakeEntityReader(calls, error=RuntimeError("read boom"))
    _wire_universe_graph_success_deps(monkeypatch, calls, builder=builder, reader=reader)

    result = ugb.build_universe_graph(as_of_date=None)

    assert result["success"] is False
    assert "read boom" in result["error"]
    assert "filtered_entities" not in result
    assert [c[0] for c in calls] == [
        "create_graph",
        "set_ontology",
        "add_text_batches",
        "wait_for_batch",
        "filter_defined_entities",
        "delete_graph",
    ]


def test_build_universe_graph_success_path_call_order_and_contract_shape(monkeypatch):
    calls = []
    entities_payload = {
        "entities": [{"uuid": "u1", "name": "Acme", "labels": ["Company"], "summary": "", "attributes": {}}],
        "entity_types": ["Company"],
        "total_count": 1,
        "filtered_count": 1,
    }
    builder = _FakeBuilder(calls)
    reader = _FakeEntityReader(calls, entities_result=_FakeFilteredEntities(entities_payload))
    _wire_universe_graph_success_deps(
        monkeypatch,
        calls,
        tickers=("AAA",),
        get_news_data_fn=lambda ticker, as_of_date=None: _news_result(
            articles=[_article(article_id=1)]
        ),
        builder=builder,
        reader=reader,
    )

    result = ugb.build_universe_graph(as_of_date="2026-01-15")

    assert result["success"] is True
    assert [c[0] for c in calls] == [
        "create_graph",
        "set_ontology",
        "add_text_batches",
        "wait_for_batch",
        "filter_defined_entities",
        "delete_graph",
    ]
    # delete_graph HARUS setelah filter_defined_entities, bukan sebelum.
    assert calls.index(("delete_graph", calls[-1][1])) == len(calls) - 1

    assert set(result.keys()) == {
        "success",
        "filtered_entities",
        "item_count",
        "failed_tickers",
        "ontology_used",
        "ingest_seconds",
        "as_of_date",
        "graph_id",
    }
    assert result["filtered_entities"] == entities_payload
    assert result["item_count"] == 1
    assert result["failed_tickers"] == []
    assert result["ontology_used"] == ugb.ONTOLOGY
    assert result["ingest_seconds"] >= 0
    assert result["as_of_date"] == "2026-01-15"

    # Aditif Tahap 4 (docs/design/tahap4_persona_from_graph_design.md §4):
    # graph_id historis untuk audit -- format PERSIS f"mirofish_universe_
    # {as_of_date or 'live'}_{uuid8hex}", dan SAMA dengan graph_id yang
    # benar-benar diteruskan ke create_graph/delete_graph (bukan nilai lain).
    import re

    assert re.fullmatch(r"mirofish_universe_2026-01-15_[0-9a-f]{8}", result["graph_id"])
    assert result["graph_id"] == calls[0][1] == calls[-1][1]

    # Timeout yang dipakai HARUS sesuai compute_timeout(item_count) aktual,
    # bukan default 600.
    assert builder.last_timeout == ugb.compute_timeout(1)
    assert builder.last_timeout != 600
