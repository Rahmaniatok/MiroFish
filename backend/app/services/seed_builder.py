"""
Phase 2b — Bangun "seed" graph dari sebuah ticker.

============================================================================
KENAPA FILE INI ADA
============================================================================
Pipeline MiroFish yang asli memulai proses "seed -> graph" dari TEKS BEBAS:

  1. Interface 1 `/ontology/generate` (api/graph.py) — user meng-upload
     PDF/MD/TXT, teksnya di-ekstrak (`FileParser.extract_text`) lalu
     DISIMPAN sebagai satu string mentah lewat
     `ProjectManager.save_extracted_text()`. String itulah "seed".
  2. Interface 2 `/build` — string seed dipotong (`TextProcessor.split_text`)
     dan dikirim ke Zep Cloud (`GraphBuilderService.add_text_batches`,
     `data_type="text"`); Zep meng-ekstrak instance entitas dari teks.
  3. `ZepEntityReader.filter_defined_entities(graph_id)` membaca instance
     entitas itu kembali keluar sebagai `FilteredEntities`.

Phase 2a sudah mengganti langkah (3): `financial_entity_extractor` membuat
`EntityNode` LANGSUNG dari dict `get_stock_context(ticker, as_of_date)` —
tanpa teks, tanpa Zep, tanpa `GraphBuilderService`. Yang belum ada: sesuatu
yang MEMULAI proses itu dari sebuah ticker saja, bukan dari dict
stock_context yang dirakit manual di skrip tes.

Modul INI adalah titik-mulai tersebut. `build_seed_from_ticker()`
menggantikan peran langkah (1)+(2) di atas:

  ticker (+ as_of_date)
        |
        v
  get_stock_context()                (data layer Phase 1)
        |
        v
  extract_financial_entities()       (Phase 2a)
        |
        v
  FilteredEntities                   <-- "seed" siap dikonsumsi Phase 2c
                                         (pembangunan edge antar entitas)

KONTRAK OUTPUT: `FilteredEntities` — struktur BUNDEL ENTITAS yang sudah
dipakai MiroFish sebagai input tahap graph-building (lihat
`zep_entity_reader.FilteredEntities` dan `build_filtered_entities` di
Phase 2a). TIDAK ada struktur "Seed" baru yang diciptakan.

Phase 2c (pembangunan edge / `related_edges` / `related_nodes`), persona,
dan simulasi TIDAK disentuh modul ini — berhenti tepat setelah menghasilkan
`FilteredEntities` yang berbentuk benar.
============================================================================
"""

from typing import Any, Dict, Optional

from ..data_layer.market_data import get_stock_context
from ..utils.logger import get_logger
from .financial_entity_extractor import build_filtered_entities
from .zep_entity_reader import FilteredEntities

logger = get_logger('mirofish.seed_builder')


class SeedBuildError(RuntimeError):
    """
    Dilempar kalau seed TIDAK bisa dibangun dengan aman untuk sebuah ticker
    (ticker invalid, kedua sisi data — harga & fundamental — gagal, dst).

    Sengaja dilempar sebagai exception, BUKAN dikembalikan sebagai seed
    parsial/rusak: tahap berikutnya (Phase 2c) tidak boleh menerima bundel
    entitas yang cuma berisi placeholder.
    """


def _describe_context_failure(stock_context: Dict[str, Any]) -> str:
    price = stock_context.get("price") or {}
    fundamental = stock_context.get("fundamental") or {}
    return (
        f"price(success={price.get('success')}, error={price.get('error')!r}); "
        f"fundamental(success={fundamental.get('success')}, error={fundamental.get('error')!r})"
    )


def build_seed_from_ticker(
    ticker: str,
    as_of_date: Optional[str] = None,
) -> FilteredEntities:
    """
    Bangun "seed" graph untuk satu ticker.

    Langkah:
      1. `get_stock_context(ticker, as_of_date)`  (data layer Phase 1)
      2. `extract_financial_entities()` via `build_filtered_entities()` (Phase 2a)
      3. kembalikan `FilteredEntities` — bentuk yang dikonsumsi Phase 2c.

    Args:
        ticker: kode saham (mis. "AAPL"). Di-normalize upper/strip di
            get_stock_context.
        as_of_date: None = konteks "live" (hari ini). String ISO
            "YYYY-MM-DD" = konteks historis pada tanggal itu. Bagian harga
            dijamin bebas lookahead; bagian fundamental TIDAK point-in-time
            (flag menempel di attributes tiap entitas).

    Returns:
        `FilteredEntities(entities=[EntityNode...], entity_types, total_count,
        filtered_count)`. `EntityNode.related_edges` / `.related_nodes` masih
        kosong — diisi Phase 2c.

    Raises:
        SeedBuildError: kalau `get_stock_context` melempar exception, ATAU
            kalau harga DAN fundamental sama-sama gagal (satu-satunya entitas
            yang tersisa cuma placeholder `Company`), ATAU kalau hasil
            ekstraksi kosong.

    Catatan (mode lenient): kalau HANYA salah satu sisi yang gagal (mis.
    fundamental gagal tapi harga sukses), seed parsial TETAP dikembalikan —
    entitas yang bisa dibuat (Company + technical_signal) tetap berguna —
    dengan warning di log. Kegagalan total baru dianggap fatal.
    """
    ticker_label = (ticker or "").strip().upper() or "<kosong>"

    try:
        stock_context = get_stock_context(ticker, as_of_date=as_of_date)
    except Exception as error:  # noqa: BLE001 — bungkus jadi error seed yang jelas
        logger.error("[%s] get_stock_context melempar exception: %s", ticker_label, error)
        raise SeedBuildError(
            f"Tidak bisa membangun seed untuk {ticker_label!r}: "
            f"get_stock_context gagal ({type(error).__name__}: {error})"
        ) from error

    price = stock_context.get("price") or {}
    fundamental = stock_context.get("fundamental") or {}
    price_ok = bool(price.get("success"))
    fundamental_ok = bool(fundamental.get("success"))

    if not price_ok and not fundamental_ok:
        detail = _describe_context_failure(stock_context)
        logger.error(
            "[%s] harga DAN fundamental sama-sama gagal @ %s -> seed tidak dibangun (%s)",
            ticker_label, as_of_date or "live", detail,
        )
        raise SeedBuildError(
            f"Tidak bisa membangun seed untuk {ticker_label!r} @ {as_of_date or 'live'}: "
            f"harga dan fundamental sama-sama gagal. {detail}. "
            f"Cek apakah ticker valid / sumber data tersedia."
        )

    if not stock_context.get("success"):
        # lenient: satu sisi gagal, tetap lanjut dengan seed parsial.
        logger.warning(
            "[%s] seed PARSIAL @ %s (price_ok=%s, fundamental_ok=%s) — "
            "entitas dari sisi yang gagal akan dilewati (%s)",
            ticker_label, as_of_date or "live", price_ok, fundamental_ok,
            _describe_context_failure(stock_context),
        )

    seed = build_filtered_entities(stock_context)

    if seed.filtered_count == 0:
        raise SeedBuildError(
            f"Tidak bisa membangun seed untuk {ticker_label!r}: "
            f"ekstraksi entitas menghasilkan 0 entitas."
        )

    logger.info(
        "[%s] seed dibangun @ %s: %d entitas, tipe=%s",
        ticker_label, as_of_date or "live", seed.filtered_count,
        sorted(seed.entity_types),
    )
    return seed


if __name__ == "__main__":
    # Demo / tes manual — lihat instruksi task Phase 2b.
    #   cd backend && python -m app.services.seed_builder
    import json

    TICKER = "AAPL"
    AS_OF = "2024-06-01"

    print(f"{'=' * 72}\nbuild_seed_from_ticker({TICKER!r}, as_of_date={AS_OF!r})\n{'=' * 72}")

    seed = build_seed_from_ticker(TICKER, as_of_date=AS_OF)

    print(f"\ntype(seed)              : {type(seed).__module__}.{type(seed).__name__}")
    print(f"seed.total_count        : {seed.total_count}")
    print(f"seed.filtered_count     : {seed.filtered_count}")
    print(f"seed.entity_types       : {sorted(seed.entity_types)}")
    print(f"len(seed.entities)      : {len(seed.entities)}")
    print(f"seed.entities[0] type   : {type(seed.entities[0]).__name__}")

    by_type: Dict[str, list] = {}
    for node in seed.entities:
        by_type.setdefault(node.get_entity_type() or "?", []).append(node)

    for entity_type in sorted(by_type):
        group = by_type[entity_type]
        print(f"\n{'-' * 72}\n{entity_type}  ({len(group)})\n{'-' * 72}")
        for node in group:
            print(f"  • {node.name}   [uuid={node.uuid}]")
            print(f"      labels              : {node.labels}")
            print(f"      summary             : {node.summary}")
            print(f"      attributes          : {json.dumps(node.attributes, ensure_ascii=False)}")
            print(f"      related_edges/nodes : {node.related_edges} / {node.related_nodes}  (diisi Phase 2c)")

    print(f"\n{'=' * 72}\nseed.to_dict() — bentuk persis yang dikonsumsi Phase 2c\n{'=' * 72}")
    print(json.dumps(seed.to_dict(), ensure_ascii=False, indent=2))
