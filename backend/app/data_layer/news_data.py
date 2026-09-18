"""
News data layer — Finnhub Company News.

Implements Fase 8c, mengikuti PERSIS keputusan desain di
docs/design/fase8b_news_schema.md (sumber & batasan dipilih di
docs/investigations/fase8a_news_data.md). Pola modular sama seperti
market_data.py/universe.py: fetch_news_data() = "raw fetch" (selalu request
sungguhan ke Finnhub kalau bukan out-of-range), get_news_data() = wrapper
ber-cache (lihat cache.py) — kode lain HARUS memanggil get_news_data(), bukan
fetch_news_data() langsung.

============================================================================
KETERBATASAN PENTING — free tier Finnhub (lihat fase8a untuk bukti empirisnya)
============================================================================
- Historical lookback ~360 hari MUNDUR DARI HARI FETCH DILAKUKAN (rolling
  window, BUKAN tanggal tetap) — diverifikasi lewat binary search di fase8a:
  360 hari mundur masih ada data, 362 hari mundur sudah 0 artikel.
- Rate limit 60 request/menit per API key (header X-Ratelimit-* dikonfirmasi
  nyata berkurang saat testing, bukan cuma dari dokumentasi).
- Field `related` mentah Finnhub cuma echo dari ticker yang di-query, BUKAN
  daftar ticker relevan sungguhan — makanya field itu di sini disimpan ulang
  sebagai `related_ticker` (lihat NewsArticle), dengan makna yang eksplisit.
============================================================================

TIDAK ADA cap jumlah artikel di modul ini (Fase 8b bagian 3: window pencarian
tanpa cap) — SEMUA artikel yang lolos leakage guard disimpan, 0 sampai
berapa pun banyaknya. TIDAK ADA logika dedup lintas as_of_date di sini (out of
scope Fase 8c — itu ranah Fase 9/entity extraction kalau diperlukan nanti).
"""

import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from ..utils.logger import get_logger
from .cache import get_cached, set_cache

logger = get_logger('mirofish.data_layer.news_data')

# Load .env mandiri (self-contained) — modul ini TIDAK mengimpor app/config.py
# supaya tidak menyentuh file lain di luar scope Fase 8c. override=False supaya
# tidak menimpa env var yang sudah di-set (mis. oleh app/config.py saat Flask
# app boot, atau oleh test harness).
_ENV_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env')
if os.path.exists(_ENV_PATH):
    load_dotenv(_ENV_PATH, override=False)
else:
    load_dotenv(override=False)

_FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"

# Fase 8b bagian 3: window pencarian tetap = 90 hari, konstanta tunggal
# (bukan parameter per-call seperti `period` di fetch_price_data).
_NEWS_LOOKBACK_WINDOW_DAYS = 90

# Fase 8a: batas historis Finnhub free tier, diverifikasi ~360 hari (360 masih
# ada data, 362 sudah 0). Rolling dari HARI FETCH, dihitung ulang tiap panggilan.
_FINNHUB_HISTORICAL_LIMIT_DAYS = 360

# Dinaikkan kalau BENTUK/MAKNA field cache berubah (pola sama seperti
# _FUNDAMENTAL_SCHEMA_VERSION/_SHARIA_SCHEMA_VERSION di market_data.py).
_NEWS_SCHEMA_VERSION = 1


@dataclass
class NewsArticle:
    """Satu item berita, field persis Fase 8b bagian 1 (bukan field mentah Finnhub apa adanya)."""
    article_id: int          # wajib, dedup key (Finnhub `id`)
    headline: str            # wajib
    summary: Optional[str]   # nullable
    publisher: Optional[str] # nullable (Finnhub `source`)
    url: str                 # wajib
    published_at: str        # wajib, ISO 8601 UTC string (dikonversi dari epoch Finnhub `datetime`)
    related_ticker: str      # wajib, = ticker yang di-query — BUKAN field mentah Finnhub `related`


def _epoch_to_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _news_error(ticker: str, error: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "success": False,
        "error": error,
        "as_of_date": as_of_date,
        "schema_version": _NEWS_SCHEMA_VERSION,
        "window_start": None,
        "window_end": None,
        "out_of_range": None,
        "caveat": None,
        "articles": None,
    }


def _out_of_range_caveat(ticker: str, as_of_date: str, fetch_date: date, days_back: int) -> str:
    """Prosa instruktif, pola sama seperti zero_weight_caveat (portfolio_agent.py):
    jelaskan apa yang BUKAN penyebab, lalu apa yang SUNGGUH terjadi — lihat Fase 8b bagian 4."""
    return (
        f"Tidak ada artikel Finnhub yang dikembalikan untuk as_of_date={as_of_date} karena "
        f"tanggal ini berada DI LUAR jangkauan historis free tier Finnhub "
        f"(~{_FINNHUB_HISTORICAL_LIMIT_DAYS} hari mundur dari tanggal fetch dilakukan, "
        f"{fetch_date.isoformat()} saat cache ini ditulis — days_back={days_back}, ini adalah "
        f"rolling window dari hari fetch, BUKAN tanggal tetap). Ini BUKAN berarti tidak ada "
        f"berita nyata tentang {ticker} pada tanggal tersebut, dan BUKAN kegagalan API atau "
        f"rate-limit — ini murni keterbatasan cakupan Finnhub free tier. Jangan "
        f"diinterpretasikan sebagai 'tidak ada berita relevan' oleh konsumen downstream; "
        f"perlakukan sebagai NULL/tidak diketahui, bukan sinyal negatif."
    )


def fetch_news_data(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    "Raw fetch" — selalu memanggil Finnhub (kecuali out-of-range, lihat di bawah),
    tidak mengecek cache. Business code sebaiknya memanggil get_news_data().

    Args:
        ticker: kode saham, mis. "AAPL"
        as_of_date: None = mode live (window 90 hari s.d. hari ini, tanpa cek
            out-of-range). Diisi string ISO "YYYY-MM-DD" = mode historis:
            window [as_of_date - 90 hari, as_of_date]; kalau as_of_date lebih
            dari ~360 hari mundur dari HARI FETCH ini dijalankan, Finnhub SAMA
            SEKALI TIDAK dipanggil — langsung ditandai out_of_range=True
            (lihat Fase 8b bagian 4 & skenario (c)).

    Returns:
        {
            "ticker": str, "success": bool, "error": str | None,
            "as_of_date": str | None,
            "schema_version": int,
            "window_start": str | None, "window_end": str | None,  # ISO date, None kalau out_of_range atau gagal
            "out_of_range": bool | None,  # None hanya kalau success=False (gagal beneran, lihat _news_error)
            "caveat": str | None,         # diisi HANYA kalau out_of_range=True
            "articles": List[dict] | None,  # None kalau success=False; else list NewsArticle
                                              # (asdict), TIDAK dibatasi jumlahnya, urut
                                              # published_at descending
        }

    PENTING: out_of_range=True TETAP success=True (bukan kegagalan teknis — permintaan
    valid, jawabannya "di luar jangkauan sumber ini", pola sama seperti
    fundamental["is_point_in_time"]=False di market_data.py). Kegagalan Finnhub
    SUNGGUHAN (network error / 429 / status non-200 / JSON tidak valid / API key
    kosong) mengembalikan success=False, error=<pesan>, TIDAK di-retry otomatis
    di sini — pemanggil (orkestrasi batch Fase 8c) yang bertanggung jawab atas
    retry/throttling.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _news_error(ticker, "ticker tidak boleh kosong", as_of_date)

    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return _news_error(
            ticker,
            "FINNHUB_API_KEY tidak dikonfigurasi (isi di .env lokal — jangan hardcode, jangan commit)",
            as_of_date,
        )

    fetch_date = datetime.now(timezone.utc).date()

    as_of: Optional[date] = None
    if as_of_date is not None:
        try:
            as_of = date.fromisoformat(as_of_date)
        except ValueError:
            return _news_error(
                ticker,
                f"as_of_date '{as_of_date}' bukan format tanggal ISO yang valid (YYYY-MM-DD)",
                as_of_date,
            )

    # --- out-of-range check, SEBELUM memanggil Finnhub sama sekali (hemat API call) ---
    if as_of is not None:
        days_back = (fetch_date - as_of).days
        if days_back > _FINNHUB_HISTORICAL_LIMIT_DAYS:
            logger.info(
                f"{ticker}: as_of_date={as_of_date} di luar jangkauan Finnhub free tier "
                f"(days_back={days_back} > {_FINNHUB_HISTORICAL_LIMIT_DAYS}) — skip fetch"
            )
            return {
                "ticker": ticker,
                "success": True,
                "error": None,
                "as_of_date": as_of_date,
                "schema_version": _NEWS_SCHEMA_VERSION,
                "window_start": None,
                "window_end": None,
                "out_of_range": True,
                "caveat": _out_of_range_caveat(ticker, as_of_date, fetch_date, days_back),
                "articles": [],
            }

    window_end = as_of if as_of is not None else fetch_date
    window_start = window_end - timedelta(days=_NEWS_LOOKBACK_WINDOW_DAYS)

    try:
        resp = requests.get(
            _FINNHUB_NEWS_URL,
            params={
                "symbol": ticker,
                "from": window_start.isoformat(),
                "to": window_end.isoformat(),
                "token": api_key,
            },
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f"Fetch berita {ticker} gagal (kesalahan jaringan): {e}")
        return _news_error(ticker, f"Permintaan ke Finnhub gagal: {e}", as_of_date)

    if resp.status_code == 429:
        # JANGAN retry otomatis di sini — bisa memperparah kalau dipanggil dalam
        # batch besar (lihat Fase 8b bagian 5: throttling adalah urusan orkestrasi
        # pemanggil, bukan state yang disimpan di skema/di sini).
        logger.warning(f"Fetch berita {ticker} kena rate limit Finnhub (429)")
        return _news_error(
            ticker,
            "Finnhub rate limit terlampaui (HTTP 429) — tidak di-retry otomatis; "
            "pemanggil bertanggung jawab atas throttling/retry saat fetch batch",
            as_of_date,
        )
    if resp.status_code != 200:
        logger.warning(f"Fetch berita {ticker} gagal, status={resp.status_code}: {resp.text[:200]}")
        return _news_error(
            ticker, f"Finnhub mengembalikan status {resp.status_code}: {resp.text[:200]}", as_of_date
        )

    try:
        raw_items = resp.json()
    except ValueError as e:
        logger.warning(f"Fetch berita {ticker} gagal parse JSON: {e}")
        return _news_error(ticker, f"Respons Finnhub bukan JSON valid: {e}", as_of_date)

    if not isinstance(raw_items, list):
        logger.warning(f"Fetch berita {ticker}: respons Finnhub bukan list ({type(raw_items).__name__})")
        return _news_error(ticker, "Respons Finnhub tidak sesuai bentuk yang diharapkan (bukan list)", as_of_date)

    articles: List[NewsArticle] = []
    for item in raw_items:
        article_id = item.get("id")
        headline = item.get("headline")
        url = item.get("url")
        epoch = item.get("datetime")
        if article_id is None or not headline or not url or epoch is None:
            logger.debug(f"{ticker}: skip item Finnhub dengan field wajib kosong (id={article_id!r})")
            continue

        published_at = _epoch_to_iso(int(epoch))

        # LEAKAGE GUARD (defense in depth) — sama pola seperti fetch_price_data
        # (market_data.py ~line 421-434): parameter `to` yang dikirim ke Finnhub
        # SEHARUSNYA sudah membatasi, tapi tidak dipercaya penuh untuk sesuatu
        # sekrusial no-lookahead-bias. Filter ulang manual di sini, WAJIB, baik
        # hasilnya 0 artikel maupun ratusan (Fase 8b bagian 3, aturan no.3).
        if as_of is not None:
            published_date = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
            if published_date > as_of:
                logger.debug(
                    f"{ticker}: buang artikel {article_id} published_at={published_at} "
                    f"> as_of_date={as_of_date} (leakage guard)"
                )
                continue

        summary = item.get("summary")
        if isinstance(summary, str):
            summary = summary.strip() or None
        else:
            summary = None

        publisher = item.get("source")
        if isinstance(publisher, str):
            publisher = publisher.strip() or None
        else:
            publisher = None

        articles.append(NewsArticle(
            article_id=int(article_id),
            headline=headline,
            summary=summary,
            publisher=publisher,
            url=url,
            published_at=published_at,
            related_ticker=ticker,
        ))

    # Urut published_at descending — MURNI keterbacaan, BUKAN pemotongan jumlah
    # (Fase 8b bagian 3: tanpa cap, skenario (a)/(b)/(d) semua lewat jalur ini
    # tanpa percabangan khusus berdasarkan volume).
    articles.sort(key=lambda a: a.published_at, reverse=True)

    return {
        "ticker": ticker,
        "success": True,
        "error": None,
        "as_of_date": as_of_date,
        "schema_version": _NEWS_SCHEMA_VERSION,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "out_of_range": False,
        "caveat": None,
        "articles": [asdict(a) for a in articles],
    }


def get_news_data(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Wrapper ber-cache (recommended entry point — business code Fase 9+ HARUS
    memanggil ini, bukan fetch_news_data() langsung).

    Cache key (ticker, as_of_date, data_type="news") — pola identik dengan
    get_price_data/get_fundamental_data (lihat cache.py, tidak diubah di sini).
    TTL mengikuti cache.py apa adanya: as_of_date diisi (baik out_of_range=True
    maupun False, keduanya "success=True" secara desain) -> tidak pernah expire;
    as_of_date=None (live) -> TTL LIVE_DATA_TTL_MINUTES existing di cache.py.
    Tidak ada konstanta TTL baru di modul ini.
    """
    ticker = (ticker or "").strip().upper()

    cached = get_cached(ticker, "news", as_of_date)
    if cached is not None and cached.get("schema_version") == _NEWS_SCHEMA_VERSION:
        logger.info(f"Cache hit for {ticker}/news" + (f"@{as_of_date}" if as_of_date else " (live)"))
        return cached
    if cached is not None:
        logger.info(
            f"Cache hit but stale schema (cached v{cached.get('schema_version')!r} != "
            f"v{_NEWS_SCHEMA_VERSION}), refetching: {ticker}/news"
            + (f"@{as_of_date}" if as_of_date else " (live)")
        )

    logger.info(f"Cache miss, fetching from Finnhub: {ticker}/news" + (f"@{as_of_date}" if as_of_date else " (live)"))
    result = fetch_news_data(ticker, as_of_date=as_of_date)

    if result.get("success"):
        set_cache(ticker, "news", result, as_of_date)

    return result


if __name__ == "__main__":
    import json
    import time

    if not os.environ.get("FINNHUB_API_KEY"):
        print("FINNHUB_API_KEY tidak diset di .env — demo di bawah akan mengembalikan success=False.")

    print(f"\n{'=' * 60}\nget_news_data('AAPL') — mode live\n{'=' * 60}")
    live = get_news_data("AAPL")
    print(f"success={live['success']} out_of_range={live['out_of_range']} "
          f"n_articles={len(live['articles']) if live['articles'] is not None else None}")

    print(f"\n{'=' * 60}\nget_news_data('AAPL', as_of_date=beberapa bulan lalu) — dalam jangkauan\n{'=' * 60}")
    in_range_date = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
    historical = get_news_data("AAPL", as_of_date=in_range_date)
    print(f"as_of_date={in_range_date} success={historical['success']} "
          f"out_of_range={historical['out_of_range']} "
          f"n_articles={len(historical['articles']) if historical['articles'] is not None else None}")
    if historical["articles"]:
        print("Contoh artikel pertama:")
        print(json.dumps(historical["articles"][0], indent=2, ensure_ascii=False))

    print(f"\n{'=' * 60}\nget_news_data('AAPL', as_of_date=2 tahun lalu) — di luar jangkauan\n{'=' * 60}")
    out_of_range_date = (datetime.now(timezone.utc).date() - timedelta(days=730)).isoformat()
    oor = get_news_data("AAPL", as_of_date=out_of_range_date)
    print(f"as_of_date={out_of_range_date} success={oor['success']} out_of_range={oor['out_of_range']}")
    print(f"caveat: {oor['caveat']}")
    assert oor["articles"] == [], "out_of_range harus tetap articles=[] bukan None"

    print(f"\n{'=' * 60}\nCache hit check (panggilan ke-2 harus jauh lebih cepat)\n{'=' * 60}")
    t0 = time.monotonic()
    get_news_data("AAPL", as_of_date=in_range_date)
    print(f"panggilan ke-2 (harusnya cache hit): {time.monotonic() - t0:.4f}s")
