"""
Phase 2c — Pembangunan EDGE antar entitas finansial.

============================================================================
KENAPA FILE INI ADA
============================================================================
Phase 2a (`financial_entity_extractor`) menghasilkan daftar `EntityNode`
DATAR: Company, Sector, ValuationMetric, FundamentalMetric, TechnicalSignal —
semuanya dengan `related_edges` / `related_nodes` KOSONG.

Di pipeline MiroFish asli, edge antar entitas dibuat oleh Zep Cloud (LLM Zep
meng-ekstrak entitas + relasi dari teks sekaligus di `add_text_batches`).
Jalur Phase 2a/2b tidak lewat sana sama sekali, jadi TIDAK ada edge yang
terbentuk untuk entitas kita. Modul INI mengisi kekosongan itu.

Edge yang dibuat SENGAJA deterministik & struktural — BUKAN hasil penalaran
LLM. Setiap entitas non-Company sudah membawa `ticker` yang sama di
`attributes`, jadi "milik siapa" sudah pasti; kita tinggal menautkan:

  Company --[has_sector]--> Sector
  Company --[has_metric]--> setiap ValuationMetric
  Company --[has_metric]--> setiap FundamentalMetric
  Company --[has_signal]--> setiap TechnicalSignal

(bintang berpusat di Company; tidak ada edge metric<->metric).

BENTUK EDGE mengikuti konvensi asli `ZepEntityReader.filter_defined_entities`
persis (lihat zep_entity_reader.py):

  related_edges[i] (di node SUMBER / outgoing):
    {"direction": "outgoing", "edge_name": str, "fact": str,
     "target_node_uuid": str}
  related_edges[i] (di node TARGET / incoming):
    {"direction": "incoming", "edge_name": str, "fact": str,
     "source_node_uuid": str}
  related_nodes[i] (di kedua ujung, tetangga saja, dedup by uuid):
    {"uuid": str, "name": str, "labels": list[str], "summary": str}

Setiap edge muncul di DUA tempat: `outgoing` di Company, `incoming` di
entitas tujuan. String `fact` sama persis di kedua arah (itu fakta milik
edge-nya, cuma dilihat dari ujung berbeda) — sama seperti perilaku Zep.

Modul ini TIDAK memanggil LLM, TIDAK menyentuh Zep, TIDAK membuat entitas
baru. Hanya mengisi `related_edges` / `related_nodes` pada EntityNode yang
sudah ada, lalu mengembalikan `FilteredEntities` yang sama.
============================================================================
"""

from typing import Any, Dict, List, Tuple

from ..utils.logger import get_logger
from .financial_entity_extractor import (
    ENTITY_TYPE_COMPANY,
    ENTITY_TYPE_FUNDAMENTAL_METRIC,
    ENTITY_TYPE_SECTOR,
    ENTITY_TYPE_SHARIA_SCREEN,
    ENTITY_TYPE_TECHNICAL_SIGNAL,
    ENTITY_TYPE_VALUATION_METRIC,
)
from .zep_entity_reader import EntityNode, FilteredEntities

logger = get_logger('mirofish.entity_edge_builder')


# (tipe entitas target) -> (edge_name, kata benda untuk string `fact`)
_EDGE_SPEC: Dict[str, Tuple[str, str]] = {
    ENTITY_TYPE_SECTOR: ("has_sector", "sector"),
    ENTITY_TYPE_VALUATION_METRIC: ("has_metric", "valuation metric"),
    ENTITY_TYPE_FUNDAMENTAL_METRIC: ("has_metric", "fundamental metric"),
    ENTITY_TYPE_TECHNICAL_SIGNAL: ("has_signal", "technical signal"),
    ENTITY_TYPE_SHARIA_SCREEN: ("has_screen", "compliance screen"),
}


def _node_ref(node: EntityNode) -> Dict[str, Any]:
    """Ringkasan tetangga untuk `related_nodes` (konvensi filter_defined_entities)."""
    return {
        "uuid": node.uuid,
        "name": node.name,
        "labels": list(node.labels),
        "summary": node.summary,
    }


def _fact(company: EntityNode, target: EntityNode, noun: str) -> str:
    ticker = company.attributes.get("ticker") or company.name
    return f"{company.name} ({ticker}) has {noun} {target.name}."


def build_entity_edges(seed: FilteredEntities) -> FilteredEntities:
    """
    Isi `related_edges` / `related_nodes` pada entitas di `seed` dengan edge
    struktural deterministik berpusat di Company.

    MUTASI DI TEMPAT: `EntityNode` di `seed.entities` diubah langsung
    (`related_edges` / `related_nodes` ditimpa), lalu objek `seed` yang sama
    dikembalikan — sama seperti `ZepEntityReader` mengisi field itu setelah
    membuat node. Aman dipanggil dari `build_seed_from_ticker` karena di sana
    node selalu baru dibuat oleh `build_filtered_entities`.

    Idempoten: memanggil dua kali menghasilkan edge yang sama (field ditimpa,
    bukan ditambah).

    Args:
        seed: hasil `build_filtered_entities()` (Phase 2a) — daftar EntityNode
            datar dengan edge kosong.

    Returns:
        `seed` yang sama, dengan edge terisi. Kalau tidak ada entitas Company
        (semestinya tak terjadi — Phase 2a selalu membuat 1), `seed`
        dikembalikan apa adanya dengan warning di log.
    """
    entities: List[EntityNode] = list(seed.entities)

    # reset dulu supaya idempoten
    for node in entities:
        node.related_edges = []
        node.related_nodes = []

    companies = [n for n in entities if n.get_entity_type() == ENTITY_TYPE_COMPANY]
    if not companies:
        logger.warning(
            "seed tanpa entitas Company -> tidak ada edge yang dibangun (%d entitas)",
            len(entities),
        )
        return seed
    if len(companies) > 1:
        logger.warning(
            "seed punya %d entitas Company (diharapkan 1); memakai yang pertama: %s",
            len(companies), companies[0].uuid,
        )
    company = companies[0]

    company_related_nodes: List[Dict[str, Any]] = []
    edge_count = 0
    skipped: Dict[str, int] = {}

    for target in entities:
        if target is company:
            continue
        target_type = target.get_entity_type()
        spec = _EDGE_SPEC.get(target_type or "")
        if spec is None:
            skipped[target_type or "<none>"] = skipped.get(target_type or "<none>", 0) + 1
            continue

        edge_name, noun = spec
        fact = _fact(company, target, noun)

        # outgoing di Company
        company.related_edges.append({
            "direction": "outgoing",
            "edge_name": edge_name,
            "fact": fact,
            "target_node_uuid": target.uuid,
        })
        company_related_nodes.append(_node_ref(target))

        # incoming di target (fact sama persis, arah berbeda)
        target.related_edges.append({
            "direction": "incoming",
            "edge_name": edge_name,
            "fact": fact,
            "source_node_uuid": company.uuid,
        })
        target.related_nodes = [_node_ref(company)]

        edge_count += 1

    company.related_nodes = company_related_nodes

    logger.info(
        "[%s] edge struktural dibangun: %d edge dari Company '%s'%s",
        company.attributes.get("ticker") or company.name,
        edge_count, company.name,
        f"; tipe entitas tanpa edge: {skipped}" if skipped else "",
    )
    return seed


def count_edges(seed: FilteredEntities) -> int:
    """
    Jumlah edge UNIK dalam graph = jumlah entri `outgoing` di semua node
    (tiap edge = satu outgoing + satu incoming; hitung satu arah saja).
    """
    return sum(
        1
        for node in seed.entities
        for edge in node.related_edges
        if edge.get("direction") == "outgoing"
    )
