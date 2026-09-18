"""
Fase 8c — tes untuk news_data.py (Finnhub Company News), mengikuti keputusan
desain di docs/design/fase8b_news_schema.md.

Offline: `requests.get` di-patch dengan stub yang merekam pemanggilan dan
mengembalikan respons palsu. Tidak ada jaringan sungguhan ke Finnhub. Cache
SQLite diisolasi per test lewat tmp_path (monkeypatch cache.DB_PATH) supaya
tidak menyentuh backend/app/data_layer/market_cache.db yang sungguhan.

Fokus (titik paling krusial yang WAJIB tertutup tes, bukan cuma smoke test):
  - out_of_range TIDAK PERNAH memanggil Finnhub sama sekali (hemat API call)
  - leakage guard membuang artikel published_at > as_of_date, bahkan kalau
    Finnhub sendiri (lewat respons palsu di sini) mengembalikan yang harusnya
    sudah difilter oleh parameter `to` -- defense in depth, bukan asumsi
  - TIDAK ADA cap jumlah artikel di manapun (Fase 8b bagian 3)
  - related_ticker = ticker yang di-query, BUKAN field mentah Finnhub `related`
  - rate limit 429 -> success=False, error, TIDAK di-retry otomatis
  - cache hit menghindari panggilan Finnhub kedua; schema_version basi = miss
  - out_of_range tetap ke-cache (success=True secara desain)
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app.data_layer import cache as cache_module
from app.data_layer import news_data
from app.data_layer.news_data import (
    _NEWS_SCHEMA_VERSION,
    _epoch_to_iso,
    fetch_news_data,
    get_news_data,
)


# ---------------------------------------------------------------------------
# Fixtures: isolasi DB cache + API key palsu, autouse supaya semua tes dapat
# state bersih tanpa boilerplate berulang.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_cache_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "DB_PATH", str(tmp_path / "test_news_cache.db"))


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "test-finnhub-key")


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, json_error=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self._json_error = json_error
        self.text = text

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


class _FakeGet:
    """Stub untuk requests.get -- merekam tiap panggilan, dan mengembalikan
    response palsu (atau raise) sesuai konfigurasi."""

    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _epoch_for(d: date, hour: int = 12) -> int:
    return int(datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=timezone.utc).timestamp())


def _raw_item(article_id, pub_date: date, headline="Headline", summary="Summary",
              source="Yahoo", related="ZZZZ", url=None, hour=12):
    return {
        "category": "company",
        "datetime": _epoch_for(pub_date, hour=hour),
        "headline": headline,
        "id": article_id,
        "image": "https://example.com/img.png",
        "related": related,  # SENGAJA beda dari ticker yang di-query di beberapa tes --
                              # membuktikan related_ticker TIDAK dibaca dari field ini.
        "source": source,
        "summary": summary,
        "url": url or f"https://finnhub.io/api/news?id={article_id}",
    }


# ---------------------------------------------------------------------------
# out_of_range: TIDAK BOLEH memanggil Finnhub sama sekali
# ---------------------------------------------------------------------------
def test_out_of_range_never_calls_finnhub(monkeypatch):
    fake_get = _FakeGet()
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    far_past = "2000-01-01"  # pasti > 360 hari mundur dari kapanpun tes ini dijalankan
    result = fetch_news_data("AAPL", as_of_date=far_past)

    assert fake_get.calls == [], "out_of_range wajib skip Finnhub sepenuhnya (hemat API call)"
    assert result["success"] is True
    assert result["error"] is None
    assert result["out_of_range"] is True
    assert result["articles"] == []
    assert result["window_start"] is None
    assert result["window_end"] is None
    assert "AAPL" in result["caveat"]
    assert "360" in result["caveat"]
    assert "rate-limit" not in result["caveat"].lower() or "bukan kegagalan" in result["caveat"].lower()


def test_out_of_range_boundary_361_days_skips_finnhub(monkeypatch):
    fake_get = _FakeGet()
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    as_of = date.today() - timedelta(days=361)
    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    assert fake_get.calls == []
    assert result["out_of_range"] is True


def test_in_range_boundary_360_days_still_calls_finnhub(monkeypatch):
    as_of = date.today() - timedelta(days=360)
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=[]))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    assert len(fake_get.calls) == 1, "days_back == 360 (bukan > 360) harus tetap memanggil Finnhub"
    assert result["out_of_range"] is False


def test_out_of_range_result_gets_cached_and_second_call_still_skips_network(monkeypatch):
    fake_get = _FakeGet()
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    far_past = "2000-01-01"
    first = get_news_data("AAPL", as_of_date=far_past)
    second = get_news_data("AAPL", as_of_date=far_past)

    assert fake_get.calls == []  # baik pertama (skip out-of-range) maupun kedua (cache hit)
    assert first == second
    assert second["out_of_range"] is True


# ---------------------------------------------------------------------------
# leakage guard: defense-in-depth, WAJIB jalan meski parameter `to` API
# seharusnya sudah cukup
# ---------------------------------------------------------------------------
def test_leakage_guard_drops_articles_published_after_as_of_date(monkeypatch):
    as_of = date.today() - timedelta(days=10)
    raw = [
        _raw_item(1, as_of - timedelta(days=1)),          # boleh: sebelum as_of_date
        _raw_item(2, as_of),                                # boleh: tepat di as_of_date
        _raw_item(3, as_of + timedelta(days=1)),           # HARUS DIBUANG: setelah as_of_date
        _raw_item(4, as_of + timedelta(days=5)),           # HARUS DIBUANG
    ]
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=raw))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    ids = {a["article_id"] for a in result["articles"]}
    assert ids == {1, 2}, "artikel dengan published_at > as_of_date wajib dibuang oleh leakage guard"
    assert all(a["published_at"] <= f"{as_of.isoformat()}T23:59:59Z" for a in result["articles"])


def test_leakage_guard_applies_even_with_many_leaking_articles(monkeypatch):
    # Simulasi ekstrem: Finnhub (lewat stub ini) mengembalikan LEBIH BANYAK artikel
    # bocor daripada artikel valid -- guard tidak boleh "menyerah" atau berhenti
    # di artikel bocor pertama.
    as_of = date.today() - timedelta(days=10)
    raw = [_raw_item(i, as_of + timedelta(days=i)) for i in range(1, 21)]  # semua bocor
    raw.append(_raw_item(999, as_of))  # satu-satunya yang valid
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=raw))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    assert [a["article_id"] for a in result["articles"]] == [999]


# ---------------------------------------------------------------------------
# TIDAK ADA cap jumlah artikel (Fase 8b bagian 3)
# ---------------------------------------------------------------------------
def test_no_article_count_cap(monkeypatch):
    as_of = date.today() - timedelta(days=10)
    raw = [_raw_item(i, as_of - timedelta(days=i % 90)) for i in range(1, 251)]  # 250 artikel valid
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=raw))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    assert len(result["articles"]) == 250, "tidak boleh ada pemotongan jumlah artikel di manapun"


def test_zero_articles_in_window_is_valid_not_error(monkeypatch):
    as_of = date.today() - timedelta(days=10)
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=[]))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    assert result["success"] is True
    assert result["error"] is None
    assert result["out_of_range"] is False
    assert result["articles"] == []
    assert result["caveat"] is None


def test_articles_sorted_by_published_at_descending(monkeypatch):
    as_of = date.today() - timedelta(days=10)
    raw = [
        _raw_item(1, as_of - timedelta(days=5)),
        _raw_item(2, as_of - timedelta(days=1)),
        _raw_item(3, as_of - timedelta(days=3)),
    ]
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=raw))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    published = [a["published_at"] for a in result["articles"]]
    assert published == sorted(published, reverse=True)
    assert [a["article_id"] for a in result["articles"]] == [2, 3, 1]


# ---------------------------------------------------------------------------
# related_ticker = ticker yang di-query, BUKAN field mentah Finnhub `related`
# ---------------------------------------------------------------------------
def test_related_ticker_overrides_raw_finnhub_related_field(monkeypatch):
    as_of = date.today() - timedelta(days=10)
    raw = [_raw_item(1, as_of, related="COMPLETELY_DIFFERENT_TICKER")]
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=raw))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("MSFT", as_of_date=as_of.isoformat())

    assert result["articles"][0]["related_ticker"] == "MSFT"
    assert "related" not in result["articles"][0]  # field mentah Finnhub tidak ikut disimpan


# ---------------------------------------------------------------------------
# field wajib/nullable (Fase 8b bagian 1)
# ---------------------------------------------------------------------------
def test_items_missing_required_fields_are_skipped(monkeypatch):
    as_of = date.today() - timedelta(days=10)
    raw = [
        _raw_item(1, as_of),
        {**_raw_item(2, as_of), "headline": ""},       # headline kosong -> skip
        {**_raw_item(3, as_of), "id": None},            # id kosong -> skip
        {**_raw_item(4, as_of), "url": None},            # url kosong -> skip
        {**_raw_item(5, as_of), "datetime": None},       # datetime kosong -> skip
    ]
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=raw))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    assert [a["article_id"] for a in result["articles"]] == [1]


def test_empty_summary_and_publisher_normalized_to_none(monkeypatch):
    as_of = date.today() - timedelta(days=10)
    raw = [_raw_item(1, as_of, summary="   ", source="")]
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=raw))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    article = result["articles"][0]
    assert article["summary"] is None
    assert article["publisher"] is None


def test_epoch_to_iso_format():
    # 2026-09-17T10:04:00Z, sama format seperti sampel yfinance di fase8a
    epoch = int(datetime(2026, 9, 17, 10, 4, 0, tzinfo=timezone.utc).timestamp())
    assert _epoch_to_iso(epoch) == "2026-09-17T10:04:00Z"


# ---------------------------------------------------------------------------
# window & request params
# ---------------------------------------------------------------------------
def test_request_params_and_window(monkeypatch):
    as_of = date.today() - timedelta(days=10)
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=[]))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=as_of.isoformat())

    assert len(fake_get.calls) == 1
    params = fake_get.calls[0]["params"]
    expected_start = (as_of - timedelta(days=90)).isoformat()
    assert params["symbol"] == "AAPL"
    assert params["from"] == expected_start
    assert params["to"] == as_of.isoformat()
    assert params["token"] == "test-finnhub-key"
    assert result["window_start"] == expected_start
    assert result["window_end"] == as_of.isoformat()


def test_live_mode_window_ends_today_and_skips_out_of_range_check(monkeypatch):
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=[]))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=None)

    assert len(fake_get.calls) == 1
    today = date.today().isoformat()
    assert fake_get.calls[0]["params"]["to"] == today
    assert result["out_of_range"] is False
    assert result["as_of_date"] is None


def test_live_mode_never_enters_out_of_range_logic(monkeypatch):
    """Bukti STRUKTURAL (bukan cuma asumsi dari out_of_range=False) bahwa mode live
    (as_of_date=None) sama sekali tidak masuk ke cabang out_of_range/days_back:
    paksa _FINNHUB_HISTORICAL_LIMIT_DAYS jadi -1, sehingga days_back APA PUN (bahkan 0)
    pasti > threshold dan akan mentrigger out_of_range KALAU cabang itu sempat dievaluasi.
    Kalau ada regresi di masa depan yang membuat mode live ikut lewat cabang tsb (mis.
    as_of diam-diam di-default ke hari ini), tes ini akan gagal (out_of_range jadi True,
    Finnhub tidak dipanggil) -- bukan cuma lolos karena kebetulan."""
    monkeypatch.setattr(news_data, "_FINNHUB_HISTORICAL_LIMIT_DAYS", -1)
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=[]))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=None)

    assert len(fake_get.calls) == 1, "mode live wajib tetap fetch Finnhub, tidak boleh ketrigger out_of_range"
    assert result["out_of_range"] is False


def test_live_mode_ttl_expires_after_live_data_ttl_minutes(monkeypatch):
    """Membuktikan mode live TIDAK 'tidak pernah expire' seperti snapshot historis --
    setelah fetched_at lebih tua dari LIVE_DATA_TTL_MINUTES (konstanta EXISTING di
    cache.py, TIDAK ada konstanta TTL baru di news_data.py), cache dianggap basi dan
    Finnhub dipanggil ulang. Melengkapi test_live_mode_cache_hit_reuses_existing_ttl_policy
    di atas, yang cuma membuktikan cache-hit pada panggilan langsung berurutan (bisa saja
    lolos meski TTL-nya keliru "tidak pernah expire") -- di sini fetched_at dimundurkan
    langsung di DB untuk membuktikan batasnya sungguh 30 menit, bukan permanen."""
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=[]))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    get_news_data("AAPL")
    assert len(fake_get.calls) == 1

    stale_fetched_at = (
        datetime.now(timezone.utc) - timedelta(minutes=cache_module.LIVE_DATA_TTL_MINUTES + 5)
    ).isoformat()
    conn = sqlite3.connect(cache_module.DB_PATH)
    with conn:
        conn.execute(
            "UPDATE market_data_cache SET fetched_at = ? "
            "WHERE ticker = ? AND data_type = ? AND as_of_date IS NULL",
            (stale_fetched_at, "AAPL", "news"),
        )
    conn.close()

    get_news_data("AAPL")
    assert len(fake_get.calls) == 2, "live mode wajib TTL-bound (LIVE_DATA_TTL_MINUTES), bukan tidak pernah expire"


# ---------------------------------------------------------------------------
# kegagalan Finnhub SUNGGUHAN (bukan out_of_range) -> success=False
# ---------------------------------------------------------------------------
def test_rate_limit_429_returns_error_without_retry(monkeypatch):
    fake_get = _FakeGet(response=_FakeResponse(429, text="rate limit exceeded"))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date=(date.today() - timedelta(days=10)).isoformat())

    assert len(fake_get.calls) == 1, "429 TIDAK BOLEH di-retry otomatis di dalam fungsi ini"
    assert result["success"] is False
    assert "429" in result["error"] or "rate limit" in result["error"].lower()
    assert result["out_of_range"] is None
    assert result["articles"] is None


def test_network_error_returns_error(monkeypatch):
    fake_get = _FakeGet(raise_exc=news_data.requests.exceptions.ConnectionError("boom"))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL")

    assert result["success"] is False
    assert "boom" in result["error"] or "Finnhub" in result["error"]


def test_non_200_status_returns_error(monkeypatch):
    fake_get = _FakeGet(response=_FakeResponse(500, text="server error"))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL")

    assert result["success"] is False
    assert "500" in result["error"]


def test_invalid_json_response_returns_error(monkeypatch):
    fake_get = _FakeGet(response=_FakeResponse(200, json_error=ValueError("bad json")))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL")

    assert result["success"] is False


def test_non_list_json_response_returns_error(monkeypatch):
    fake_get = _FakeGet(response=_FakeResponse(200, json_data={"error": "invalid token"}))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL")

    assert result["success"] is False


def test_missing_api_key_returns_error_without_network_call(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    fake_get = _FakeGet()
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL")

    assert fake_get.calls == []
    assert result["success"] is False
    assert "FINNHUB_API_KEY" in result["error"]


def test_empty_ticker_returns_error_without_network_call(monkeypatch):
    fake_get = _FakeGet()
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("")

    assert fake_get.calls == []
    assert result["success"] is False


def test_invalid_as_of_date_format_returns_error_without_network_call(monkeypatch):
    fake_get = _FakeGet()
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    result = fetch_news_data("AAPL", as_of_date="18-09-2026")

    assert fake_get.calls == []
    assert result["success"] is False


# ---------------------------------------------------------------------------
# get_news_data(): cache wrapper
# ---------------------------------------------------------------------------
def test_cache_hit_avoids_second_finnhub_call(monkeypatch):
    as_of = (date.today() - timedelta(days=10)).isoformat()
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=[_raw_item(1, date.today() - timedelta(days=10))]))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    first = get_news_data("AAPL", as_of_date=as_of)
    second = get_news_data("AAPL", as_of_date=as_of)

    assert len(fake_get.calls) == 1, "panggilan kedua wajib cache hit, bukan fetch ulang"
    assert first == second


def test_failed_fetch_is_not_cached(monkeypatch):
    fake_get = _FakeGet(response=_FakeResponse(500, text="server error"))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    as_of = (date.today() - timedelta(days=10)).isoformat()
    get_news_data("AAPL", as_of_date=as_of)
    get_news_data("AAPL", as_of_date=as_of)

    assert len(fake_get.calls) == 2, "hasil gagal (success=False) tidak boleh di-cache, tiap panggilan fetch ulang"


def test_stale_schema_version_triggers_refetch(monkeypatch):
    as_of = (date.today() - timedelta(days=10)).isoformat()
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=[]))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    stale = {
        "ticker": "AAPL", "success": True, "error": None, "as_of_date": as_of,
        "schema_version": _NEWS_SCHEMA_VERSION - 1,  # versi lama, sengaja beda
        "window_start": None, "window_end": None, "out_of_range": False,
        "caveat": None, "articles": [],
    }
    cache_module.set_cache("AAPL", "news", stale, as_of)

    result = get_news_data("AAPL", as_of_date=as_of)

    assert len(fake_get.calls) == 1, "cache dengan schema_version basi wajib diperlakukan sebagai miss"
    assert result["schema_version"] == _NEWS_SCHEMA_VERSION


def test_live_mode_cache_hit_reuses_existing_ttl_policy(monkeypatch):
    fake_get = _FakeGet(response=_FakeResponse(200, json_data=[]))
    monkeypatch.setattr(news_data.requests, "get", fake_get)

    get_news_data("AAPL")
    get_news_data("AAPL")

    assert len(fake_get.calls) == 1, "mode live juga harus cache-hit di dalam TTL (LIVE_DATA_TTL_MINUTES existing)"
