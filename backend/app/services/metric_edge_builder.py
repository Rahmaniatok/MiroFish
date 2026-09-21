"""
Fase 9 (baru) — edge Company-to-Company DITURUNKAN DARI METRIK, TIDAK ADA LLM.

============================================================================
KENAPA FILE INI ADA
============================================================================
Mengikuti PERSIS docs/design/fase9a_metric_based_edges.md (yang menggantikan
docs/design/fase9a_entity_extraction_schema.md, SUPERSEDED setelah validasi
empiris menemukan cross-mention rate berita cuma 5.4%, di bawah ambang 10%).

Fase 2 (`financial_entity_extractor.py` + `entity_edge_builder.py`) membangun
bintang STRUKTURAL per ticker: Company -> Sector/ValuationMetric/
FundamentalMetric/TechnicalSignal/ShariaScreen. Modul INI menambahkan LAPISAN
edge Company-to-Company DI ATAS bintang itu, murni dari tiga metrik yang
SUDAH ADA di data layer:

  1. price_correlated      - korelasi Pearson return harian (reuse
                              build_returns_matrix, portfolio_optimizer.py)
  2. fundamentally_similar - kemiripan 9 rasio fundamental (reuse
                              get_fundamental_data, market_data.py)
  3. same_sector            - sub-industry sama (reuse field Sector Fase 2a,
                              `gics_sector`/`industry`)

TIDAK ADA panggilan LLM di modul ini sama sekali. Bintang Fase 2 TIDAK
diubah - edge di sini di-APPEND ke `related_edges` Company node yang sudah
ada (has_sector/has_metric/has_signal/has_screen dari Fase 2c tetap utuh).

Karena edge Company-to-Company butuh KEDUA ujung Company berada di satu
graph yang sama, `build_universe_seed()` di bawah menggabungkan hasil
`build_seed_from_ticker()` (Fase 2b, TIDAK diubah) untuk banyak ticker
sekaligus sebelum menambahkan edge metrik - pola struktural sama seperti
`build_universe_seed` ilustratif di dokumen desain lama (SUPERSEDED), cuma
isi langkah "bangun edge Company-to-Company"-nya sekarang murni komputasi.

Ketiga jenis relasi SEMUA simetris (lihat _METRIC_EDGE_SPEC) - TIDAK ADA
mekanisme kunci-merge arah-sadar/`conflicting_direction` seperti desain versi
berita (SUPERSEDED); tidak relevan di sini karena tidak ada sumber yang bisa
"melaporkan arah" secara keliru - semua angka dihitung langsung dari data.

TIDAK ADA caching untuk edge Company-to-Company (bagian 8 dokumen desain) -
dihitung fresh tiap panggilan. Cache Fase 1 di bawahnya (`get_price_data`/
`get_fundamental_data`, `cache.py`) TIDAK disentuh, tetap menyerap biaya
network seperti biasa.
============================================================================
"""

import math
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

from ..data_layer.market_data import get_fundamental_data
from ..utils.logger import get_logger
from .financial_entity_extractor import ENTITY_TYPE_COMPANY
from .portfolio_optimizer import build_returns_matrix
from .seed_builder import SeedBuildError, build_seed_from_ticker
from .zep_entity_reader import EntityNode, FilteredEntities

logger = get_logger('mirofish.metric_edge_builder')


# ---------------------------------------------------------------------------
# Konstanta - persis bagian 2/4/5 dokumen desain
# ---------------------------------------------------------------------------
RELATION_PRICE_CORRELATED = "price_correlated"
RELATION_FUNDAMENTALLY_SIMILAR = "fundamentally_similar"
RELATION_SAME_SECTOR = "same_sector"

# (relation_type) -> spec. Mirror gaya _EDGE_SPEC (entity_edge_builder.py) /
# _NEWS_EDGE_SPEC (dokumen lama, SUPERSEDED). SEMUA symmetric=True - bagian 4
# dokumen desain: tidak ada satu pun dari ketiga metrik ini punya arah.
_METRIC_EDGE_SPEC: Dict[str, Dict[str, Any]] = {
    RELATION_PRICE_CORRELATED: {"symmetric": True, "noun": "price correlation"},
    RELATION_FUNDAMENTALLY_SIMILAR: {"symmetric": True, "noun": "fundamental similarity"},
    RELATION_SAME_SECTOR: {"symmetric": True, "noun": "sub-industry peer"},
}

_TOP_K = 5  # bagian 2.1/2.2/2.3 dokumen desain
_MIN_METRIC_OVERLAP = 3  # bagian 1.2
_WINSORIZE_LIMIT = 3.0  # bagian 1.2
_DEFAULT_CORRELATION_LOOKBACK = "1y"  # bagian 1.1 - sama default portfolio_optimizer.py

# Union 9 metrik numerik dari _VALUATION_METRIC_SPECS + _FUNDAMENTAL_METRIC_SPECS
# (financial_entity_extractor.py) - field FLAT persis get_fundamental_data.
_NUMERIC_METRICS: Tuple[str, ...] = (
    "pe_ratio", "pb_ratio", "market_cap", "revenue_growth_yoy",
    "profit_margin", "roe", "debt_to_equity", "eps_growth", "dividend_yield",
)


def _normalize_tickers(tickers: List[str]) -> List[str]:
    return sorted({(t or "").strip().upper() for t in tickers if t and t.strip()})


# ---------------------------------------------------------------------------
# Top-K union - generik, dipakai oleh ketiga jenis edge (bagian 2 dokumen)
# ---------------------------------------------------------------------------
def _topk_union(
    pair_values: Dict[Tuple[str, str], float],
    tickers: List[str],
    top_k: int,
    rank_key,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """
    `pair_values`: dict keyed by SORTED 2-tuple (a, b) dengan a < b.
    `rank_key`: fungsi transform nilai sebelum dibandingkan untuk ranking
    (mis. `abs` untuk korelasi - bagian 2.1 revisi sign-ambiguity: ranking
    top-K pakai |correlation_value|, TAPI nilai yang disimpan di edge tetap
    nilai ASLI/`pair_values[k]`, bukan hasil `rank_key`).

    Union (bagian 2.1): edge (a, b) terbentuk kalau SALAH SATU dari a atau b
    punya yang lain di top-K miliknya sendiri - TIDAK harus keduanya saling
    menominasi. `rank_a`/`rank_b` di hasil sesuai posisi (a=elemen pertama
    kunci sorted, b=elemen kedua) - BUKAN "siapa lebih penting".
    """
    per_ticker_ranked: Dict[str, List[Tuple[str, float]]] = {}
    for t in tickers:
        peers: List[Tuple[str, float]] = []
        for other in tickers:
            if other == t:
                continue
            key = tuple(sorted((t, other)))
            if key in pair_values:
                peers.append((other, pair_values[key]))
        peers.sort(key=lambda kv: rank_key(kv[1]), reverse=True)
        per_ticker_ranked[t] = peers[:top_k]

    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for t, ranked in per_ticker_ranked.items():
        for position, (peer, value) in enumerate(ranked, start=1):
            key = tuple(sorted((t, peer)))
            if key not in result:
                result[key] = {"value": value, "rank_a": None, "rank_b": None}
            first, second = key
            if t == first:
                result[key]["rank_a"] = position
            else:
                result[key]["rank_b"] = position
    return result


# ---------------------------------------------------------------------------
# 1. price_correlated (bagian 1.1 / 2.1 / 5.1)
# ---------------------------------------------------------------------------
def _price_correlation_matrix(
    tickers: List[str],
    as_of_date: Optional[str] = None,
    lookback: str = _DEFAULT_CORRELATION_LOOKBACK,
) -> Dict[str, Any]:
    """
    Komputasi INTI korelasi, dipakai bersama oleh `compute_price_correlation_edges`
    (top-K GLOBAL, bagian 2.1) DAN `compute_sector_edges` (top-K DALAM GRUP,
    bagian 2.3, REUSE - bukan dihitung ulang). Correlation VALUE dihitung SEKALI
    di sini; dua fungsi di atas cuma beda cara MERANKING/MEMFILTERnya.

    Reuse `build_returns_matrix` (portfolio_optimizer.py) APA ADANYA untuk input
    return harian - Pearson `.corr()` (pandas) DIHITUNG BARU di sini, BUKAN
    Ledoit-Wolf shrinkage covariance yang dipakai optimizer (tujuan beda, lihat
    bagian 1.1 dokumen desain).

    Returns:
        {
            "pair_values": {(a, b) sorted: float, ...},  # Pearson correlation
            "period": {"start", "end", "lookback", "n_observations"},
            "cols": [...],     # ticker yang punya cukup riwayat harga
            "dropped": [...],  # ticker yang di-drop oleh build_returns_matrix
        }
    """
    returns = build_returns_matrix(tickers, as_of_date=as_of_date, lookback=lookback)
    cols = list(returns.columns)
    dropped = sorted(set(tickers) - set(cols))
    if dropped:
        # build_returns_matrix/portfolio_optimizer SUDAH log alasan detail per
        # ticker - di sini cukup catat dampaknya ke edge korelasi, tidak
        # menduplikasi loggingnya.
        logger.info(
            "price_correlated: %d ticker tanpa cukup riwayat harga, di-drop dari "
            "matriks korelasi (lihat log build_returns_matrix untuk alasan detail): %s",
            len(dropped), dropped,
        )

    pair_values: Dict[Tuple[str, str], float] = {}
    period: Dict[str, Any] = {}
    if len(cols) >= 2:
        corr = returns.corr()
        for a, b in combinations(cols, 2):
            pair_values[(a, b)] = float(corr.loc[a, b])
        period = {
            "start": returns.index.min().strftime("%Y-%m-%d"),
            "end": returns.index.max().strftime("%Y-%m-%d"),
            "lookback": lookback,
            "n_observations": int(returns.shape[0]),
        }

    return {"pair_values": pair_values, "period": period, "cols": cols, "dropped": dropped}


def _edges_from_correlation_matrix(
    matrix: Dict[str, Any],
    as_of_date: Optional[str],
) -> List[Dict[str, Any]]:
    pair_values = matrix["pair_values"]
    period = matrix["period"]
    cols = matrix["cols"]
    if len(cols) < 2:
        return []

    # Ranking top-K by |correlation_value| (revisi sign-ambiguity, bagian 2.1) -
    # nilai yang DISIMPAN di edge tetap pair_values[key] asli (bisa negatif).
    ranked = _topk_union(pair_values, cols, _TOP_K, rank_key=abs)

    edges: List[Dict[str, Any]] = []
    for (a, b), info in ranked.items():
        edges.append({
            "relation_type": RELATION_PRICE_CORRELATED,
            "ticker_a": a,
            "ticker_b": b,
            "attributes": {
                "relation_type": RELATION_PRICE_CORRELATED,
                "symmetric": True,
                "correlation_value": info["value"],
                "correlation_period": dict(period),
                "as_of_date": as_of_date,
                "rank_a": info["rank_a"],
                "rank_b": info["rank_b"],
            },
        })
    return edges


def compute_price_correlation_edges(
    tickers: List[str],
    as_of_date: Optional[str] = None,
    lookback: str = _DEFAULT_CORRELATION_LOOKBACK,
) -> List[Dict[str, Any]]:
    """
    Edge `price_correlated`: top-5 per ticker (union), ranking by
    |correlation_value|, `correlation_value` tersimpan dengan tanda ASLI
    (bagian 2.1 dokumen desain, termasuk revisi sign-ambiguity).

    Ticker yang di-drop `build_returns_matrix` (riwayat harga kurang) otomatis
    tidak menyumbang/menerima edge jenis ini - tidak ada cabang kode khusus.
    """
    tickers = _normalize_tickers(tickers)
    matrix = _price_correlation_matrix(tickers, as_of_date=as_of_date, lookback=lookback)
    return _edges_from_correlation_matrix(matrix, as_of_date=as_of_date)


# ---------------------------------------------------------------------------
# 2. fundamentally_similar (bagian 1.2 / 2.2 / 5.2)
# ---------------------------------------------------------------------------
def _zscore_fundamentals(
    fundamentals: Dict[str, Dict[str, Any]],
    tickers: List[str],
) -> Tuple[Dict[str, Dict[str, float]], List[str]]:
    """
    Z-score per metrik ATAS POPULASI universe run ini (skip null saat hitung
    mean/std), `market_cap` di-log10 dulu (WAJIB), lalu winsorize [-3, +3].

    GUARD std=0 (Langkah 0 - ditemukan sebagai gap saat review dokumen, belum
    muncul di data real yang dites tapi struktural mungkin terjadi): metrik
    dengan std=0 di populasi run ini (semua ticker nilainya identik persis)
    di-SKIP TOTAL dari z-score run itu - dianggap "tidak informatif untuk
    membedakan siapa pun di run ini", BUKAN division-by-zero/error.

    Returns (z, std0_metrics) - `z[metric][ticker] -> float`, `std0_metrics`
    = daftar metrik yang di-skip karena std=0.
    """
    raw: Dict[str, Dict[str, float]] = {m: {} for m in _NUMERIC_METRICS}
    for t in tickers:
        f = fundamentals.get(t)
        if not f or not f.get("success"):
            continue
        for m in _NUMERIC_METRICS:
            v = f.get(m)
            if v is None:
                continue
            if m == "market_cap":
                if v <= 0:
                    continue
                v = math.log10(v)
            raw[m][t] = float(v)

    z: Dict[str, Dict[str, float]] = {m: {} for m in _NUMERIC_METRICS}
    std0_metrics: List[str] = []
    for m in _NUMERIC_METRICS:
        values = list(raw[m].values())
        if len(values) < 2:
            continue
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        std = math.sqrt(variance)
        if std == 0.0:
            std0_metrics.append(m)
            logger.info(
                "fundamentally_similar: metrik '%s' std=0 di populasi run ini "
                "(%d ticker semua bernilai identik) -> di-skip TOTAL dari "
                "z-score run ini, tidak informatif untuk membedakan, bukan error",
                m, len(values),
            )
            continue
        for t, v in raw[m].items():
            zc = (v - mean) / std
            zc = max(-_WINSORIZE_LIMIT, min(_WINSORIZE_LIMIT, zc))
            z[m][t] = zc
    return z, std0_metrics


def _rms_distance(
    z: Dict[str, Dict[str, float]], a: str, b: str
) -> Tuple[Optional[float], List[str]]:
    """RMS distance (bagian 1.2) - dibagi jumlah metrik overlap AKTUAL untuk
    pasangan ini, bukan total 9, supaya jarak antar pasangan adil dibandingkan
    apa pun jumlah metrik yang tersedia (bagian 7 - data fundamental parsial)."""
    overlap = [m for m in _NUMERIC_METRICS if a in z[m] and b in z[m]]
    if len(overlap) < _MIN_METRIC_OVERLAP:
        return None, overlap
    sq_sum = sum((z[m][a] - z[m][b]) ** 2 for m in overlap)
    return math.sqrt(sq_sum / len(overlap)), overlap


def compute_fundamental_similarity_edges(
    tickers: List[str],
    as_of_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Edge `fundamentally_similar`: top-5 per ticker (union), ranking by
    `similarity_score = 1 / (1 + rms_distance)` (selalu positif, tidak ada
    ambiguitas tanda seperti korelasi).

    Pasangan dengan < 3 metrik overlap DI-SKIP sepenuhnya (bagian 1.2/7),
    dicatat dengan alasan eksplisit. Ticker dengan < 3 metrik non-null TOTAL
    secara struktural tidak akan pernah lolos minimum overlap dengan siapa
    pun -> nol edge jenis ini untuk ticker itu, tanpa memengaruhi
    partisipasinya di price_correlated/same_sector (bagian 7).
    """
    tickers = _normalize_tickers(tickers)
    fundamentals = {t: get_fundamental_data(t, as_of_date=as_of_date) for t in tickers}

    z, _std0_metrics = _zscore_fundamentals(fundamentals, tickers)

    pair_values: Dict[Tuple[str, str], float] = {}
    pair_metrics: Dict[Tuple[str, str], List[str]] = {}
    for a, b in combinations(tickers, 2):
        dist, overlap = _rms_distance(z, a, b)
        if dist is None:
            logger.info(
                "fundamentally_similar: pasangan %s-%s di-skip, overlap=%d "
                "metrics < minimum %d", a, b, len(overlap), _MIN_METRIC_OVERLAP,
            )
            continue
        key = (a, b)
        pair_values[key] = 1.0 / (1.0 + dist)
        pair_metrics[key] = overlap

    ranked = _topk_union(pair_values, tickers, _TOP_K, rank_key=lambda v: v)

    edges: List[Dict[str, Any]] = []
    for (a, b), info in ranked.items():
        metrics_compared = pair_metrics[(a, b)]
        edges.append({
            "relation_type": RELATION_FUNDAMENTALLY_SIMILAR,
            "ticker_a": a,
            "ticker_b": b,
            "attributes": {
                "relation_type": RELATION_FUNDAMENTALLY_SIMILAR,
                "symmetric": True,
                "similarity_score": info["value"],
                "metrics_compared": list(metrics_compared),
                "n_metrics_compared": len(metrics_compared),
                "as_of_date": as_of_date,
                "rank_a": info["rank_a"],
                "rank_b": info["rank_b"],
            },
        })
    return edges


# ---------------------------------------------------------------------------
# 3. same_sector (bagian 1.3 / 2.3 / 5.3)
# ---------------------------------------------------------------------------
def compute_sector_edges(
    tickers: List[str],
    as_of_date: Optional[str],
    correlation_values: Dict[Tuple[str, str], float],
) -> List[Dict[str, Any]]:
    """
    Edge `same_sector`: group by `industry` (sub-industry, field Fase 2a) SAMA
    PERSIS, lalu top-5 DALAM GRUP ranking by |correlation_value| - REUSE angka
    dari `correlation_values` (bagian 2.1), TIDAK DIHITUNG ULANG.

    `correlation_values`: dict pairwise PENUH dari `_price_correlation_matrix`
    (`matrix["pair_values"]`) - SENGAJA BUKAN list edge `price_correlated`
    yang sudah top-5-difilter-global. Kalau cuma reuse edge yang sudah lolos
    top-5 global, banyak pasangan dalam satu sub-industry tidak akan punya
    correlation_value untuk dipakai ranking (mereka mungkin tidak masuk top-5
    GLOBAL siapa pun, meski relevan sebagai peer DALAM sub-industry-nya) -
    itu akan menyimpang dari bagian 2.3 dokumen desain ("top-K DALAM grup",
    bukan "irisan top-K global DENGAN grup"). Diklarifikasi & dikonfirmasi
    eksplisit saat implementasi (lihat ringkasan sesi).

    Pasangan dalam grup yang correlation_value-nya TIDAK diketahui (mis. salah
    satu ticker di-drop dari matriks korelasi karena riwayat harga kurang)
    TIDAK dianggap berkorelasi 0/None secara diam-diam - pasangan itu murni
    tidak masuk kandidat ranking top-K sektor sama sekali.

    `shared_sector`/`shared_sub_industry` SELALU True untuk edge yang
    terbentuk lewat mekanisme ini (sub-industry sama -> otomatis sektor besar
    juga sama, bagian 2.3).
    """
    tickers = _normalize_tickers(tickers)
    fundamentals = {t: get_fundamental_data(t, as_of_date=as_of_date) for t in tickers}

    groups: Dict[Tuple[str, str], List[str]] = {}
    for t in tickers:
        f = fundamentals.get(t) or {}
        if not f.get("success"):
            continue
        sector = f.get("sector")
        industry = f.get("industry")
        if not sector or not industry:
            logger.info(
                "same_sector: %s tanpa sector/industry terisi -> tidak masuk "
                "grup sub-industry manapun (0 edge same_sector untuk ticker ini)",
                t,
            )
            continue
        groups.setdefault((sector, industry), []).append(t)

    edges: List[Dict[str, Any]] = []
    for (sector, industry), members in groups.items():
        if len(members) < 2:
            continue  # tidak ada pasangan mungkin dalam grup berisi 1 ticker

        pair_values: Dict[Tuple[str, str], float] = {}
        for a, b in combinations(members, 2):
            key = (a, b)
            if key in correlation_values:
                pair_values[key] = correlation_values[key]

        ranked = _topk_union(pair_values, members, _TOP_K, rank_key=abs)
        for (a, b), info in ranked.items():
            edges.append({
                "relation_type": RELATION_SAME_SECTOR,
                "ticker_a": a,
                "ticker_b": b,
                "attributes": {
                    "relation_type": RELATION_SAME_SECTOR,
                    "symmetric": True,
                    "shared_sector": True,
                    "shared_sub_industry": True,
                    "sector": sector,
                    "sub_industry": industry,
                    "correlation_value": info["value"],
                    "as_of_date": as_of_date,
                    "rank_a": info["rank_a"],
                    "rank_b": info["rank_b"],
                },
            })
    return edges


# ---------------------------------------------------------------------------
# Merakit edge jadi bentuk outgoing/incoming (konvensi Fase 2c) & append ke graph
# ---------------------------------------------------------------------------
def _node_ref(node: EntityNode) -> Dict[str, Any]:
    return {"uuid": node.uuid, "name": node.name, "labels": list(node.labels), "summary": node.summary}


def _fact_for_edge(edge: Dict[str, Any], company_a: EntityNode, company_b: EntityNode) -> str:
    relation_type = edge["relation_type"]
    attrs = edge["attributes"]
    if relation_type == RELATION_PRICE_CORRELATED:
        period = attrs["correlation_period"]
        return (
            f"{company_a.name} and {company_b.name} show a price_correlated "
            f"relationship (Pearson r={attrs['correlation_value']:.3f} over the "
            f"{period.get('lookback')} window ending {period.get('end')})."
        )
    if relation_type == RELATION_FUNDAMENTALLY_SIMILAR:
        return (
            f"{company_a.name} and {company_b.name} show a fundamentally_similar "
            f"relationship (similarity_score={attrs['similarity_score']:.3f} over "
            f"{attrs['n_metrics_compared']} compared metrics)."
        )
    if relation_type == RELATION_SAME_SECTOR:
        return (
            f"{company_a.name} and {company_b.name} share the {attrs['sub_industry']} "
            f"sub-industry as of {attrs['as_of_date']} "
            f"(correlation_value={attrs['correlation_value']:.3f})."
        )
    raise ValueError(f"unknown relation_type {relation_type!r}")  # pragma: no cover


def _append_edges_to_graph(seed: FilteredEntities, edges: List[Dict[str, Any]]) -> None:
    """
    Rakit tiap edge (bagian 4: TIDAK digabung, 3 jenis relation_type tetap
    terpisah) jadi pasangan outgoing/incoming persis konvensi Fase 2c, lalu
    APPEND ke `related_edges` Company node yang bersangkutan - TIDAK reset
    (bintang Fase 2c: has_sector/has_metric/has_signal/has_screen tetap utuh).

    Arah outgoing/incoming: `ticker_a` (alfabetis lebih awal, dari kunci
    sorted `_topk_union`) = outgoing source, `ticker_b` = incoming target -
    pilihan ARBITRER (ketiga relation_type simetris, bagian 4), murni supaya
    representasi deterministik/reproducible, bukan klaim arah semantik.
    """
    companies: Dict[str, EntityNode] = {
        node.attributes.get("ticker"): node
        for node in seed.entities
        if node.get_entity_type() == ENTITY_TYPE_COMPANY
    }

    appended = 0
    skipped_unknown = 0
    for edge in edges:
        a, b = edge["ticker_a"], edge["ticker_b"]
        company_a, company_b = companies.get(a), companies.get(b)
        if company_a is None or company_b is None:
            skipped_unknown += 1
            logger.warning(
                "metric edge %s(%s,%s) merujuk ticker tanpa Company node di seed "
                "gabungan (a_found=%s, b_found=%s) -> dilewati",
                edge["relation_type"], a, b, company_a is not None, company_b is not None,
            )
            continue

        fact = _fact_for_edge(edge, company_a, company_b)
        attrs = dict(edge["attributes"])

        company_a.related_edges.append({
            "direction": "outgoing",
            "edge_name": edge["relation_type"],
            "fact": fact,
            "target_node_uuid": company_b.uuid,
            "attributes": dict(attrs),
        })
        company_b.related_edges.append({
            "direction": "incoming",
            "edge_name": edge["relation_type"],
            "fact": fact,
            "source_node_uuid": company_a.uuid,
            "attributes": dict(attrs),
        })

        for node, neighbour in ((company_a, company_b), (company_b, company_a)):
            if not any(rn.get("uuid") == neighbour.uuid for rn in node.related_nodes):
                node.related_nodes.append(_node_ref(neighbour))

        appended += 1

    logger.info(
        "metric_edge_builder: %d edge ditambahkan ke graph gabungan (%d dilewati - "
        "ticker tanpa Company node)", appended, skipped_unknown,
    )


# ---------------------------------------------------------------------------
# API publik - orkestrasi penuh (bagian 6 dokumen: melengkapi Fase 2, tidak mengganti)
# ---------------------------------------------------------------------------
def build_universe_seed(
    tickers: List[str],
    as_of_date: Optional[str] = None,
    lookback: str = _DEFAULT_CORRELATION_LOOKBACK,
) -> FilteredEntities:
    """
    Bangun graph gabungan multi-ticker: tiap ticker lewat `build_seed_from_ticker`
    (Fase 2b, TIDAK diubah) -> concat entities jadi SATU `FilteredEntities` ->
    tambahkan 3 jenis edge Company-to-Company (murni komputasi metrik, TIDAK
    ADA LLM) sebagai lapisan tambahan, APPEND ke `related_edges` Company node
    yang sudah punya edge struktural Fase 2c.

    Ticker individual yang gagal `build_seed_from_ticker` (SeedBuildError -
    price DAN fundamental sama-sama gagal, atau 0 entitas) di-skip dari graph
    gabungan dengan warning, TIDAK menggagalkan seluruh run (pola lenient
    yang sama seperti `screen_universe`/`screen_and_rank`).

    TIDAK ADA caching di level fungsi ini (bagian 8 dokumen desain) - fresh
    compute setiap panggilan. Cache Fase 1 di bawahnya (get_price_data/
    get_fundamental_data) tetap berlaku seperti biasa, tidak disentuh.
    """
    normalized = _normalize_tickers(tickers)
    if not normalized:
        raise ValueError("build_universe_seed membutuhkan minimal 1 ticker")

    per_ticker_seeds = []
    for ticker in normalized:
        try:
            per_ticker_seeds.append(build_seed_from_ticker(ticker, as_of_date=as_of_date))
        except SeedBuildError as exc:
            logger.warning(
                "build_universe_seed: %s dilewati dari graph gabungan (%s)", ticker, exc
            )

    merged_entities: List[EntityNode] = [
        node for seed in per_ticker_seeds for node in seed.entities
    ]
    merged = FilteredEntities(
        entities=merged_entities,
        entity_types={n.get_entity_type() for n in merged_entities if n.get_entity_type()},
        total_count=len(merged_entities),
        filtered_count=len(merged_entities),
    )

    corr_matrix = _price_correlation_matrix(normalized, as_of_date=as_of_date, lookback=lookback)
    price_edges = _edges_from_correlation_matrix(corr_matrix, as_of_date=as_of_date)
    fundamental_edges = compute_fundamental_similarity_edges(normalized, as_of_date=as_of_date)
    sector_edges = compute_sector_edges(normalized, as_of_date, corr_matrix["pair_values"])

    _append_edges_to_graph(merged, price_edges + fundamental_edges + sector_edges)

    logger.info(
        "build_universe_seed: %d ticker diminta, %d berhasil, %d entitas total, "
        "edge metrik: price_correlated=%d fundamentally_similar=%d same_sector=%d",
        len(normalized), len(per_ticker_seeds), len(merged_entities),
        len(price_edges), len(fundamental_edges), len(sector_edges),
    )
    return merged


if __name__ == "__main__":
    # Demo/tes manual (LIVE - butuh network):
    #   cd backend && python -m app.services.metric_edge_builder
    TICKERS = ["NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "MU", "JPM", "BAC", "MSFT"]

    print(f"{'=' * 70}\nbuild_universe_seed({TICKERS!r})\n{'=' * 70}")
    seed = build_universe_seed(TICKERS, as_of_date=None)
    print(f"total entitas: {seed.total_count}")

    companies = [n for n in seed.entities if n.get_entity_type() == ENTITY_TYPE_COMPANY]
    for c in companies:
        by_type: Dict[str, int] = {}
        for e in c.related_edges:
            if e["direction"] == "outgoing":
                by_type[e["edge_name"]] = by_type.get(e["edge_name"], 0) + 1
        print(f"  {c.attributes.get('ticker')}: {len(c.related_edges)} edge total "
              f"(outgoing per tipe: {by_type})")
