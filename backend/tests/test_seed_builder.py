"""
Phase 2b — tes `build_seed_from_ticker`.

Offline: `get_stock_context` di-patch dengan fixture dict (bentuk persis
output data layer Phase 1). Tidak ada jaringan / yfinance / Zep.

Yang diverifikasi:
  - sukses  -> `FilteredEntities` berbentuk benar (siap dikonsumsi Phase 2c)
  - kedua sisi data gagal -> `SeedBuildError` (bukan seed parsial)
  - satu sisi gagal (lenient) -> seed parsial tetap dikembalikan
  - get_stock_context melempar exception -> dibungkus jadi `SeedBuildError`
"""

import copy

import pytest

from app.services import seed_builder
from app.services.seed_builder import SeedBuildError, build_seed_from_ticker
from app.services.zep_entity_reader import EntityNode, FilteredEntities

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
        "skipped_fields": {},
    },
}

_ENTITY_NODE_KEYS = {
    "uuid", "name", "labels", "summary", "attributes",
    "related_edges", "related_nodes",
}


def _patch_context(monkeypatch, value):
    """Patch get_stock_context yang dipakai seed_builder dengan sebuah nilai/callable."""
    def fake_get_stock_context(ticker, as_of_date=None):
        if callable(value):
            return value(ticker, as_of_date)
        return copy.deepcopy(value)

    monkeypatch.setattr(seed_builder, "get_stock_context", fake_get_stock_context)


def test_success_returns_filtered_entities_shape(monkeypatch):
    _patch_context(monkeypatch, STOCK_CONTEXT_AAPL)

    seed = build_seed_from_ticker("AAPL", as_of_date="2024-06-01")

    assert isinstance(seed, FilteredEntities)
    assert seed.total_count == seed.filtered_count == len(seed.entities)
    assert seed.filtered_count > 0
    assert "Company" in seed.entity_types

    for node in seed.entities:
        assert isinstance(node, EntityNode)
        assert set(node.to_dict()) == _ENTITY_NODE_KEYS
        assert node.labels[-1] == "Entity"
        # Phase 2c: edge sudah terisi secara default (bintang berpusat di Company)
        assert node.related_edges != []
        assert node.related_nodes != []
        # ticker + as_of_date terbawa di attributes (tidak hilang tanpa wrapper)
        assert node.attributes.get("ticker") == "AAPL"
        assert node.attributes.get("as_of_date") == "2024-06-01"

    # Company terhubung ke semua entitas lain; tiap entitas lain balik ke Company
    company = next(n for n in seed.entities if n.get_entity_type() == "Company")
    assert len(company.related_edges) == len(seed.entities) - 1
    assert all(e["direction"] == "outgoing" for e in company.related_edges)
    for leaf in seed.entities:
        if leaf is company:
            continue
        assert [e["direction"] for e in leaf.related_edges] == ["incoming"]
        assert leaf.related_edges[0]["source_node_uuid"] == company.uuid


def test_entity_types_match_phase_2a(monkeypatch):
    _patch_context(monkeypatch, STOCK_CONTEXT_AAPL)
    seed = build_seed_from_ticker("AAPL", as_of_date="2024-06-01")
    assert seed.entity_types == {
        "Company", "Sector", "ValuationMetric", "FundamentalMetric", "TechnicalSignal",
    }


def test_both_sides_fail_raises(monkeypatch):
    ctx = copy.deepcopy(STOCK_CONTEXT_AAPL)
    ctx["success"] = False
    ctx["price"] = {"ticker": "BADX", "success": False, "error": "no price data",
                    "technical_indicators": None}
    ctx["fundamental"] = {"ticker": "BADX", "success": False, "error": "not found"}
    _patch_context(monkeypatch, ctx)

    with pytest.raises(SeedBuildError) as excinfo:
        build_seed_from_ticker("BADX", as_of_date="2024-06-01")
    assert "BADX" in str(excinfo.value)


def test_partial_failure_is_lenient(monkeypatch):
    """Fundamental gagal, harga sukses -> seed parsial tetap dikembalikan."""
    ctx = copy.deepcopy(STOCK_CONTEXT_AAPL)
    ctx["success"] = False
    ctx["fundamental"] = {"ticker": "AAPL", "success": False, "error": "boom"}
    _patch_context(monkeypatch, ctx)

    seed = build_seed_from_ticker("AAPL", as_of_date="2024-06-01")

    assert isinstance(seed, FilteredEntities)
    # Company (placeholder) + technical_signal dari harga; TIDAK ada fundamental
    assert "TechnicalSignal" in seed.entity_types
    assert "ValuationMetric" not in seed.entity_types
    assert "FundamentalMetric" not in seed.entity_types


def test_get_stock_context_exception_is_wrapped(monkeypatch):
    def boom(ticker, as_of_date=None):
        raise ValueError("yfinance exploded")

    _patch_context(monkeypatch, boom)

    with pytest.raises(SeedBuildError) as excinfo:
        build_seed_from_ticker("AAPL", as_of_date="2024-06-01")
    assert "get_stock_context gagal" in str(excinfo.value)


def test_seed_to_dict_is_phase_2c_ready(monkeypatch):
    _patch_context(monkeypatch, STOCK_CONTEXT_AAPL)
    payload = build_seed_from_ticker("AAPL", as_of_date="2024-06-01").to_dict()

    assert set(payload) == {"entities", "entity_types", "total_count", "filtered_count"}
    assert isinstance(payload["entities"], list)
    assert set(payload["entities"][0]) == _ENTITY_NODE_KEYS
