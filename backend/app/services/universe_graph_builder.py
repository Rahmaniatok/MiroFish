"""
Tahap 3 (Feed ke Zep) — orkestrasi produksi.

Mengikuti PERSIS docs/design/tahap3_zep_feed_design.md (desain sudah disetujui,
termasuk koreksi angka waktu skenario besar ≈2,31 jam). Modul ini TIDAK
mendesain ulang apapun — setiap keputusan (siklus hidup create+delete per run,
fail-fast pada batch partial/failed, cakupan ticker tanpa cap, retry Finnhub
tepat 1x tanpa backoff, kontrak output FilteredEntities bukan graph_id,
timeout dinamis) sudah final di dokumen desain.

Referensi yang DIBACA, TIDAK diubah: graph_builder.py, app/utils/zep.py,
news_data.py, universe.py, zep_entity_reader.py.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from ..data_layer.news_data import get_news_data
from ..data_layer.universe import screen_universe
from ..utils.logger import get_logger
from .graph_builder import GraphBuilderService
from .zep_entity_reader import ZepEntityReader

logger = get_logger("mirofish.universe_graph_builder")

# --- §1 desain: model timeout dinamis (476,9 + 1,527 x item_count) x margin 2 ---
_TIMEOUT_MODEL_INTERCEPT_SECONDS = 476.9
_TIMEOUT_MODEL_SLOPE_SECONDS_PER_ITEM = 1.527
_TIMEOUT_MARGIN_MULTIPLIER = 2
_TIMEOUT_FLOOR_SECONDS = 600

# --- §2 desain: cap artikel per ticker (TIDAK ADA cap jumlah TICKER) ---
_ARTICLES_PER_TICKER = 3

# --- §4 desain: ontology final, verifikasi limit tier Flex sudah dilakukan
# secara live (10 entity + 10 edge = 20 total DITERIMA PENUH, dikonfirmasi
# read-back — lihat laporan Langkah 0). Ontology final ini (3+2=5 total) jauh
# di bawah limit, tidak butuh penyesuaian.
ONTOLOGY: Dict[str, Any] = {
    "entity_types": [
        {
            "name": "Company",
            "description": "A publicly traded company.",
            "attributes": [{"name": "ticker", "description": "Stock ticker symbol"}],
        },
        {
            "name": "Person",
            "description": "A named individual (executive, analyst, etc).",
            "attributes": [],
        },
        {
            "name": "MarketEvent",
            "description": "A market-relevant event.",
            "attributes": [{"name": "event_type", "description": "Type of market event"}],
        },
    ],
    "edge_types": [
        {
            "name": "INVOLVES",
            "description": "A MarketEvent involves a Company.",
            "source_targets": [{"source": "MarketEvent", "target": "Company"}],
        },
        {
            "name": "COMMENTS_ON",
            "description": "A Person comments on a Company.",
            "source_targets": [{"source": "Person", "target": "Company"}],
        },
    ],
}


@dataclass(frozen=True)
class NewsItem:
    """Satu artikel siap-kirim-ke-Zep (§2 desain)."""

    ticker: str
    headline: str
    summary: Optional[str]
    publisher: Optional[str]
    published_at: str
    article_id: int
    url: str


@dataclass(frozen=True)
class FailedTicker:
    """Ticker yang gagal fetch berita di KEDUA percobaan (§2 desain)."""

    ticker: str
    error: str


def compute_timeout(item_count: int) -> int:
    """Timeout ingest dinamis — §1 desain, BUKAN konstanta tetap.

    model_wait(n) = 476.9 + 1.527 * n
    timeout(n)    = max(600, ceil(2 * model_wait(n)))

    Verifikasi manual dari dokumen desain: compute_timeout(129) == 1348,
    compute_timeout(1500) == 5535.
    """
    if item_count < 0:
        raise ValueError("item_count must not be negative")
    model_wait = _TIMEOUT_MODEL_INTERCEPT_SECONDS + _TIMEOUT_MODEL_SLOPE_SECONDS_PER_ITEM * item_count
    return max(_TIMEOUT_FLOOR_SECONDS, math.ceil(_TIMEOUT_MARGIN_MULTIPLIER * model_wait))


def fetch_news_for_universe(
    tickers: List[str],
    as_of_date: Optional[str] = None,
) -> Tuple[List[NewsItem], List[FailedTicker]]:
    """Fetch berita untuk SEMUA ticker (tanpa cap), retry tepat 1x langsung.

    §2 desain, PERSIS:
    - get_news_data(ticker, as_of_date) untuk setiap ticker.
    - success=False -> retry SATU KALI LANGSUNG, tanpa sleep/backoff.
    - Gagal di kedua percobaan -> FailedTicker, TIDAK memblokir ticker lain.
    - Dedup GLOBAL by article_id lintas SEMUA ticker (bukan per-ticker).
    - tickers kosong -> ValueError eksplisit (bukan return kosong diam-diam).
    """
    if not tickers:
        raise ValueError("tickers must not be empty")

    news_items: List[NewsItem] = []
    failed_tickers: List[FailedTicker] = []
    seen_article_ids: Set[int] = set()

    for ticker in tickers:
        result = get_news_data(ticker, as_of_date=as_of_date)
        if not result.get("success"):
            # Retry TEPAT satu kali, langsung, tanpa delay/backoff — investigasi
            # §2.2 mengukur 80% recovery hanya dari retry langsung, timeout
            # yang sama; menambah backoff di sini menambah waktu tanpa bukti perlu.
            result = get_news_data(ticker, as_of_date=as_of_date)

        if not result.get("success"):
            failed_tickers.append(
                FailedTicker(ticker=ticker, error=result.get("error") or "unknown Finnhub fetch error")
            )
            continue

        articles = result.get("articles") or []
        picked = 0
        for article in articles:
            if picked >= _ARTICLES_PER_TICKER:
                break
            article_id = article.get("article_id")
            if article_id in seen_article_ids:
                continue
            seen_article_ids.add(article_id)
            news_items.append(
                NewsItem(
                    ticker=ticker,
                    headline=article.get("headline") or "",
                    summary=article.get("summary"),
                    publisher=article.get("publisher"),
                    published_at=article.get("published_at") or "",
                    article_id=article_id,
                    url=article.get("url") or "",
                )
            )
            picked += 1

    return news_items, failed_tickers


def build_zep_batch(news_items: List[NewsItem]) -> List[str]:
    """1 NewsItem -> 1 elemen `chunks` untuk add_text_batches (§3 desain).

    Format PERSIS: f"[{published_at}] {ticker}: {headline}. {summary}"
    `published_at` WAJIB di teks — add_text_batches tidak meneruskan
    `created_at`, jadi ini satu-satunya sinyal tanggal yang benar untuk
    ekstraksi Zep. summary None/kosong -> bagian itu dihilangkan, bukan
    "None" literal. Tidak ada logika pemotongan tambahan (limit 10.000
    karakter sudah ditangani validate_batch_chunks yang sudah ada).
    """
    chunks: List[str] = []
    for item in news_items:
        chunk = f"[{item.published_at}] {item.ticker}: {item.headline}."
        if item.summary:
            chunk = f"{chunk} {item.summary}"
        chunks.append(chunk)
    return chunks


def _failure_result(
    *,
    error: str,
    failed_tickers: List[FailedTicker],
    item_count: int,
    as_of_date: Optional[str],
) -> Dict[str, Any]:
    """Bentuk kontrak gagal (§7/§8 desain) — TIDAK PERNAH ada key
    "filtered_entities" (bukan None, bukan list kosong)."""
    return {
        "success": False,
        "error": error,
        "failed_tickers": [asdict(f) for f in failed_tickers],
        "item_count": item_count,
        "as_of_date": as_of_date,
    }


def build_universe_graph(
    as_of_date: Optional[str] = None,
    sectors: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Orkestrasi utama Tahap 3 — §5 + §8 desain, urutan PERSIS.

    screen_universe (tanpa cap) -> fetch_news_for_universe (+1 retry) ->
    build_zep_batch -> create_graph -> set_ontology -> add_text_batches ->
    _wait_for_batch (timeout dinamis §1) -> filter_defined_entities (BACA
    SEBELUM HAPUS) -> delete_graph (SELALU, jalur sukses maupun gagal) ->
    return FilteredEntities ke Tahap 4 (bukan graph_id — graph sudah dihapus).
    """
    candidates = screen_universe(sectors=sectors, market_cap_tiers=market_cap_tiers, as_of_date=as_of_date)
    tickers = [c["ticker"] for c in candidates]

    if not tickers:
        # screen_universe tidak menghasilkan kandidat sama sekali. Ini bukan
        # kasus eksplisit di §8 desain (yang mengasumsikan tickers non-kosong
        # masuk ke fetch_news_for_universe), tapi konsisten dengan §8 titik 1
        # ("semua ticker gagal" -> error sebelum panggil Zep): tidak ada
        # ticker sama sekali harus diperlakukan setara, bukan meneruskan list
        # kosong ke fetch_news_for_universe (yang akan raise ValueError untuk
        # kasus programming-error, bukan untuk hasil screening yang sah kosong).
        return _failure_result(
            error="screen_universe returned no candidate tickers",
            failed_tickers=[],
            item_count=0,
            as_of_date=as_of_date,
        )

    news_items, failed_tickers = fetch_news_for_universe(tickers, as_of_date=as_of_date)

    if not news_items:
        # §8 titik 1: SEMUA ticker gagal fetch -> JANGAN panggil Zep sama sekali.
        return _failure_result(
            error=f"all {len(tickers)} ticker(s) failed to fetch news (0 items collected)",
            failed_tickers=failed_tickers,
            item_count=0,
            as_of_date=as_of_date,
        )

    chunks = build_zep_batch(news_items)
    item_count = len(chunks)

    builder = GraphBuilderService()
    graph_id = f"mirofish_universe_{as_of_date or 'live'}_{uuid.uuid4().hex[:8]}"

    try:
        builder.create_graph("MiroFish Universe Graph", graph_id=graph_id)
    except Exception as e:
        # §8 titik 2: graph TIDAK terkonfirmasi ada -> TIDAK ADA delete_graph.
        logger.error("create_graph failed for %s: %s", graph_id, e)
        return _failure_result(
            error=f"create_graph failed: {e}",
            failed_tickers=failed_tickers,
            item_count=item_count,
            as_of_date=as_of_date,
        )

    # Dari titik ini graph SUDAH ada di Zep -> delete_graph WAJIB dipanggil di
    # setiap jalur keluar (sukses maupun gagal), makanya dibungkus finally.
    try:
        try:
            builder.set_ontology(graph_id, ONTOLOGY)
        except Exception as e:
            # §8 titik 3: beda dari titik 2 -- graph kosong SUDAH ada, jadi
            # delete_graph tetap dipanggil (di blok finally di bawah).
            logger.error("set_ontology failed for %s: %s", graph_id, e)
            return _failure_result(
                error=f"set_ontology failed: {e}",
                failed_tickers=failed_tickers,
                item_count=item_count,
                as_of_date=as_of_date,
            )

        timeout = compute_timeout(item_count)

        try:
            submission = builder.add_text_batches(graph_id, chunks)
        except Exception as e:
            # §8 titik 4.
            logger.error("add_text_batches failed for %s: %s", graph_id, e)
            return _failure_result(
                error=f"add_text_batches failed: {e}",
                failed_tickers=failed_tickers,
                item_count=item_count,
                as_of_date=as_of_date,
            )

        ingest_start = time.monotonic()
        try:
            builder._wait_for_batch(submission, timeout=timeout)
        except (RuntimeError, TimeoutError) as e:
            # §8 titik 5 (partial/failed/invalid/canceled) DAN titik 6
            # (TimeoutError) -- PERLAKUAN SAMA PERSIS, tidak ada partial
            # acceptance, konsisten fail-fast.
            logger.error("batch ingestion failed for %s: %s", graph_id, e)
            return _failure_result(
                error=f"batch ingestion failed: {e}",
                failed_tickers=failed_tickers,
                item_count=item_count,
                as_of_date=as_of_date,
            )
        ingest_seconds = time.monotonic() - ingest_start

        try:
            filtered = ZepEntityReader().filter_defined_entities(graph_id)
        except Exception as e:
            # §8 titik 7: batch SUKSES penuh tapi baca-balik gagal -> graph
            # TETAP dihapus (Tahap 4 tidak pernah pegang graph_id hidup, jadi
            # mempertahankan graph di sini hanya menyisakan sampah tak
            # terjangkau). Trade-off yang disadari: seluruh biaya ingest
            # hangus sia-sia di sini -- bukan celah, konsekuensi langsung dari
            # kombinasi fail-fast + kontrak FilteredEntities yang sudah final.
            logger.error("filter_defined_entities failed for %s (batch already succeeded): %s", graph_id, e)
            return _failure_result(
                error=f"filter_defined_entities failed: {e}",
                failed_tickers=failed_tickers,
                item_count=item_count,
                as_of_date=as_of_date,
            )

        return {
            "success": True,
            "filtered_entities": filtered.to_dict(),
            "item_count": item_count,
            "failed_tickers": [asdict(f) for f in failed_tickers],
            "ontology_used": ONTOLOGY,
            "ingest_seconds": ingest_seconds,
            "as_of_date": as_of_date,
        }
    finally:
        # SELALU dipanggil -- jalur sukses (setelah baca entity) maupun semua
        # jalur gagal di titik 3-7. Kegagalan delete di sini (mis. saat
        # set_ontology juga gagal di titik 3) HANYA di-log, TIDAK PERNAH
        # menimpa error utama yang sudah di-return di atas.
        try:
            builder.delete_graph(graph_id)
        except Exception as cleanup_error:
            logger.warning("delete_graph failed during cleanup for %s: %s", graph_id, cleanup_error)
