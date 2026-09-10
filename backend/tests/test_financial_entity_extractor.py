"""
Phase 2a — tes ekstraksi entitas finansial.

Dua bagian:
  1. Tes unit offline (fixture dict, tanpa jaringan) — memverifikasi skema
     EntityNode dipertahankan, tipe entitas benar, null dilewati.
  2. Blok __main__ pada modul (dijalankan lewat perintah di README task) yang
     memanggil get_stock_context("AAPL", "2024-06-01") sungguhan.
"""

from app.services.financial_entity_extractor import (
    ENTITY_TYPE_COMPANY,
    ENTITY_TYPE_FUNDAMENTAL_METRIC,
    ENTITY_TYPE_SECTOR,
    ENTITY_TYPE_TECHNICAL_SIGNAL,
    ENTITY_TYPE_VALUATION_METRIC,
    build_filtered_entities,
    extract_financial_entities,
    extract_financial_entity_nodes,
)
from app.services.zep_entity_reader import EntityNode

# Bentuk persis output get_stock_context() — AAPL @ 2024-06-01 (dari cache
# Phase 1). ROE & debt/equity sengaja TIDAK ada (belum disediakan data layer).
STOCK_CONTEXT_AAPL = {
    "ticker": "AAPL",
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


def test_output_preserves_entitynode_schema():
    entities = extract_financial_entities(STOCK_CONTEXT_AAPL)
    assert entities, "harus menghasilkan entitas"
    for entity in entities:
        assert set(entity) == _ENTITY_NODE_KEYS
        assert entity["labels"][-1] == "Entity"           # label default Zep
        assert entity["labels"][0] in {
            ENTITY_TYPE_COMPANY, ENTITY_TYPE_SECTOR, ENTITY_TYPE_VALUATION_METRIC,
            ENTITY_TYPE_FUNDAMENTAL_METRIC, ENTITY_TYPE_TECHNICAL_SIGNAL,
        }
        assert entity["related_edges"] == []              # Phase 2c
        assert entity["related_nodes"] == []
        # round-trip lewat EntityNode -> get_entity_type() tetap bekerja
        node = EntityNode(
            uuid=entity["uuid"], name=entity["name"], labels=entity["labels"],
            summary=entity["summary"], attributes=entity["attributes"],
        )
        assert node.get_entity_type() == entity["labels"][0]


def test_entity_types_and_values():
    by_type: dict[str, list[dict]] = {}
    for entity in extract_financial_entities(STOCK_CONTEXT_AAPL):
        by_type.setdefault(entity["labels"][0], []).append(entity)

    # company: 1, ticker menempel
    assert len(by_type[ENTITY_TYPE_COMPANY]) == 1
    assert by_type[ENTITY_TYPE_COMPANY][0]["attributes"]["ticker"] == "AAPL"

    # sector: GICS sector + industry
    assert len(by_type[ENTITY_TYPE_SECTOR]) == 1
    sector_attrs = by_type[ENTITY_TYPE_SECTOR][0]["attributes"]
    assert sector_attrs["gics_sector"] == "Technology"
    assert sector_attrs["industry"] == "Consumer Electronics"

    # valuation_metric: P/E, P/B, market cap — 3 entitas terpisah, value menempel
    valuation = {e["attributes"]["metric"]: e["attributes"]["value"]
                 for e in by_type[ENTITY_TYPE_VALUATION_METRIC]}
    assert valuation == {
        "pe_ratio": 36.65178,
        "pb_ratio": 43.474186,
        "market_cap": 4669700046848,
    }

    # fundamental_metric: hanya revenue growth + profit margin (ROE / D/E absen)
    fundamental_metrics = {e["attributes"]["metric"]
                           for e in by_type[ENTITY_TYPE_FUNDAMENTAL_METRIC]}
    assert fundamental_metrics == {"revenue_growth_yoy", "profit_margin"}

    # technical_signal: RSI, MACD, price-vs-SMA50/200, Bollinger — 5 entitas
    tech = {e["attributes"]["indicator"]: e["attributes"]["signal"]
            for e in by_type[ENTITY_TYPE_TECHNICAL_SIGNAL]}
    assert set(tech) == {
        "rsi_14", "macd", "price_vs_sma50", "price_vs_sma200", "bollinger_position",
    }
    assert tech["rsi_14"] == "neutral"       # 67.29 -> belum overbought (>=70)
    assert tech["macd"] == "bearish"         # histogram < 0
    assert tech["price_vs_sma50"] == "above"
    assert tech["bollinger_position"] == "near upper band"


def test_point_in_time_flag_propagated_to_fundamental_entities():
    entities = extract_financial_entities(STOCK_CONTEXT_AAPL)
    for entity in entities:
        if entity["labels"][0] in {ENTITY_TYPE_VALUATION_METRIC, ENTITY_TYPE_FUNDAMENTAL_METRIC}:
            assert entity["attributes"]["is_point_in_time"] is False


def test_nulls_are_skipped_not_fabricated():
    ctx = _deep_copy(STOCK_CONTEXT_AAPL)
    ctx["fundamental"]["pe_ratio"] = None
    ctx["fundamental"]["revenue_growth_yoy"] = float("nan")
    ctx["price"]["technical_indicators"]["rsi_14"] = None
    ctx["price"]["technical_indicators"]["macd_signal"] = {
        "macd": None, "signal": None, "histogram": None,
    }

    metrics = {e["attributes"].get("metric") for e in extract_financial_entities(ctx)}
    indicators = {e["attributes"].get("indicator") for e in extract_financial_entities(ctx)}

    assert "pe_ratio" not in metrics            # null -> dilewati
    assert "revenue_growth_yoy" not in metrics  # NaN -> dilewati
    assert "rsi_14" not in indicators
    assert "macd" not in indicators
    # yang lain tetap ada
    assert "pb_ratio" in metrics
    assert "bollinger_position" in indicators


def test_missing_fundamental_section_still_yields_company_entity():
    ctx = _deep_copy(STOCK_CONTEXT_AAPL)
    ctx["fundamental"] = {"ticker": "AAPL", "success": False, "error": "boom"}
    ctx["success"] = False

    entities = extract_financial_entities(ctx)
    types = {e["labels"][0] for e in entities}
    assert types == {ENTITY_TYPE_COMPANY, ENTITY_TYPE_TECHNICAL_SIGNAL}


def test_missing_price_section_skips_technical_signals():
    ctx = _deep_copy(STOCK_CONTEXT_AAPL)
    ctx["price"] = {"ticker": "AAPL", "success": False, "error": "boom",
                    "technical_indicators": None}

    types = {e["labels"][0] for e in extract_financial_entities(ctx)}
    assert ENTITY_TYPE_TECHNICAL_SIGNAL not in types
    assert ENTITY_TYPE_VALUATION_METRIC in types


def test_helpers_return_pipeline_compatible_shapes():
    nodes = extract_financial_entity_nodes(STOCK_CONTEXT_AAPL)
    assert all(isinstance(n, EntityNode) for n in nodes)

    bundle = build_filtered_entities(STOCK_CONTEXT_AAPL)
    assert bundle.filtered_count == len(nodes) == bundle.total_count
    assert ENTITY_TYPE_COMPANY in bundle.entity_types


def _deep_copy(value):
    import copy
    return copy.deepcopy(value)
