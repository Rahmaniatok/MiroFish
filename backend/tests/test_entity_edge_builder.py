"""
Phase 2c — tes pembangunan edge antar entitas finansial.

Offline: `get_stock_context` di-patch dengan fixture dict (bentuk persis
output data layer Phase 1, AAPL @ 2024-06-01 dengan 6 fundamental metric
seperti cache live pasca Phase 1f). Tidak ada jaringan.

Yang diverifikasi:
  - Company punya satu outgoing edge ke SETIAP entitas non-Company
  - edge_name benar per tipe (has_sector / has_metric / has_signal)
  - tiap entitas leaf punya tepat satu incoming edge balik ke Company
  - related_nodes dua arah konsisten (konvensi filter_defined_entities)
  - string `fact` identik di kedua arah
  - count_edges == jumlah entitas leaf
  - idempoten (panggil dua kali -> hasil sama, tidak dobel)
  - build_seed_from_ticker() memanggilnya otomatis
"""

import copy

import pytest

from app.services import seed_builder
from app.services.entity_edge_builder import build_entity_edges, count_edges
from app.services.financial_entity_extractor import build_filtered_entities
from app.services.seed_builder import build_seed_from_ticker

STOCK_CONTEXT_AAPL = {
    "ticker": "AAPL",
    "company_name": "Apple Inc.",
    "as_of_date": "2024-06-01",
    "success": True,
    "price": {
        "ticker": "AAPL",
        "success": True,
        "error": None,
        "period": "1y",
        "as_of_date": "2024-06-01",
        "latest_close": 190.4347,
        "latest_date": "2024-05-31",
        "stats": {"52w_high": 195.72, "52w_low": 163.22},
        "technical_indicators": {
            "price_vs_sma50": 8.61,
            "price_vs_sma200": 6.33,
            "rsi_14": 67.29,
            "macd_signal": {"macd": 4.2141, "signal": 4.3303, "histogram": -0.1162},
            "bollinger_position": 0.8127,
            "volume_vs_avg": 1.2413,
            "change_1d_pct": 0.5,
        },
    },
    "fundamental": {
        "ticker": "AAPL",
        "success": True,
        "error": None,
        "as_of_date": "2024-06-01",
        "is_point_in_time": False,
        "warning": "nilai terkini, bukan snapshot historis",
        "company_name": "Apple Inc.",
        "pe_ratio": 36.65178,
        "pb_ratio": 43.474186,
        "market_cap": 4669700046848,
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "revenue_growth_yoy": 0.164,
        "profit_margin": 0.27618998,
        "roe": 1.4875101,
        "debt_to_equity": 0.78445,
        "eps_growth": 0.287,
        "dividend_yield": 0.0032815575,
        "skipped_fields": {},
    },
}

# Company + Sector(1) + ValuationMetric(3) + FundamentalMetric(6) + TechnicalSignal(5)
_EXPECTED_LEAF_COUNT = 1 + 3 + 6 + 5   # semua kecuali Company
_EDGE_NAME_BY_TYPE = {
    "Sector": "has_sector",
    "ValuationMetric": "has_metric",
    "FundamentalMetric": "has_metric",
    "TechnicalSignal": "has_signal",
}


def _seed():
    return build_filtered_entities(copy.deepcopy(STOCK_CONTEXT_AAPL))


def _company(seed):
    return next(n for n in seed.entities if n.get_entity_type() == "Company")


def _leaves(seed):
    return [n for n in seed.entities if n.get_entity_type() != "Company"]


def test_company_has_outgoing_edge_to_every_leaf():
    seed = build_entity_edges(_seed())
    company = _company(seed)

    assert len(company.related_edges) == _EXPECTED_LEAF_COUNT
    assert all(e["direction"] == "outgoing" for e in company.related_edges)

    targets = {e["target_node_uuid"] for e in company.related_edges}
    assert targets == {leaf.uuid for leaf in _leaves(seed)}

    # edge_name sesuai tipe target
    by_uuid = {n.uuid: n for n in seed.entities}
    for edge in company.related_edges:
        target_type = by_uuid[edge["target_node_uuid"]].get_entity_type()
        assert edge["edge_name"] == _EDGE_NAME_BY_TYPE[target_type]
        assert "source_node_uuid" not in edge   # outgoing -> hanya target


def test_each_leaf_points_back_to_company():
    seed = build_entity_edges(_seed())
    company = _company(seed)

    for leaf in _leaves(seed):
        assert len(leaf.related_edges) == 1
        edge = leaf.related_edges[0]
        assert edge["direction"] == "incoming"
        assert edge["source_node_uuid"] == company.uuid
        assert "target_node_uuid" not in edge

        assert leaf.related_nodes == [{
            "uuid": company.uuid,
            "name": company.name,
            "labels": list(company.labels),
            "summary": company.summary,
        }]


def test_company_related_nodes_lists_all_leaves():
    seed = build_entity_edges(_seed())
    company = _company(seed)
    listed = {n["uuid"] for n in company.related_nodes}
    assert listed == {leaf.uuid for leaf in _leaves(seed)}
    for ref in company.related_nodes:
        assert set(ref) == {"uuid", "name", "labels", "summary"}


def test_fact_string_identical_both_directions():
    seed = build_entity_edges(_seed())
    company = _company(seed)
    out_by_target = {e["target_node_uuid"]: e["fact"] for e in company.related_edges}
    for leaf in _leaves(seed):
        assert leaf.related_edges[0]["fact"] == out_by_target[leaf.uuid]
        assert leaf.name in leaf.related_edges[0]["fact"]
        assert "AAPL" in leaf.related_edges[0]["fact"]


def test_count_edges():
    seed = build_entity_edges(_seed())
    assert count_edges(seed) == _EXPECTED_LEAF_COUNT   # 15 untuk AAPL


def test_idempotent():
    seed = build_entity_edges(_seed())
    first = count_edges(seed)
    company_edges_first = len(_company(seed).related_edges)

    build_entity_edges(seed)   # panggil lagi di objek yang sama
    assert count_edges(seed) == first
    assert len(_company(seed).related_edges) == company_edges_first


def test_no_company_returns_seed_unchanged(caplog):
    seed = _seed()
    seed.entities = [n for n in seed.entities if n.get_entity_type() != "Company"]
    out = build_entity_edges(seed)
    assert out is seed
    assert all(n.related_edges == [] for n in out.entities)


def test_build_seed_from_ticker_populates_edges_by_default(monkeypatch):
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(STOCK_CONTEXT_AAPL),
    )
    seed = build_seed_from_ticker("AAPL", as_of_date="2024-06-01")
    assert count_edges(seed) == _EXPECTED_LEAF_COUNT
    company = _company(seed)
    assert len(company.related_edges) == _EXPECTED_LEAF_COUNT


def test_print_edge_structure(monkeypatch, capsys):
    """Cetak struktur edge untuk verifikasi manual (jalankan dengan -s)."""
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(STOCK_CONTEXT_AAPL),
    )
    import json

    seed = build_seed_from_ticker("AAPL", as_of_date="2024-06-01")
    company = _company(seed)
    a_metric = next(
        n for n in seed.entities if n.get_entity_type() == "FundamentalMetric"
    )

    with capsys.disabled():
        print(f"\n{'=' * 72}")
        print("Company node")
        print(f"{'=' * 72}")
        print(f"name  : {company.name}  [uuid={company.uuid}]")
        print(f"related_edges ({len(company.related_edges)}):")
        print(json.dumps(company.related_edges, ensure_ascii=False, indent=2))
        print(f"related_nodes ({len(company.related_nodes)}):")
        print(json.dumps(company.related_nodes, ensure_ascii=False, indent=2))

        print(f"\n{'=' * 72}")
        print(f"One metric node — {a_metric.name}")
        print(f"{'=' * 72}")
        print(f"uuid  : {a_metric.uuid}")
        print(f"related_edges ({len(a_metric.related_edges)}):")
        print(json.dumps(a_metric.related_edges, ensure_ascii=False, indent=2))
        print(f"related_nodes ({len(a_metric.related_nodes)}):")
        print(json.dumps(a_metric.related_nodes, ensure_ascii=False, indent=2))

        print(f"\n{'=' * 72}")
        print(f"total edge (unik) dalam graph : {count_edges(seed)}")
        print(f"{'=' * 72}")
