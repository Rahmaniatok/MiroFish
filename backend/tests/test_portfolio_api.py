"""
Phase 7a — HTTP tests for the portfolio/universe API (Flask test client, no
running server, no network).

Reuses the same offline fixtures as the service-layer tests:
  - `seed_builder.get_stock_context` patched with the Phase 5b/5e 9-ticker
    fixture set (`tests.test_consensus_screener._CONTEXTS`, JPM/GS non-compliant).
  - `portfolio_optimizer.get_price_data` patched with the same deterministic
    synthetic price series used by `test_portfolio_optimizer.py`.
  - `LLMClient.chat` patched with a canned response for /ask (no live LLM).
  - `/debate` calls with `use_llm=False` so it exercises the deterministic
    template path (no live LLM).

These tests assert the API is a thin wrapper: /build's JSON body must match
`build_portfolio()` called directly, byte for byte.
"""

import copy

import pytest

from app import create_app
from app.services import portfolio_optimizer
from app.services import seed_builder
from app.services.portfolio_optimizer import build_portfolio
from app.utils.llm_client import LLMClient

from tests.test_consensus_screener import _CONTEXTS
from tests.test_portfolio_optimizer import _PRICE_OHLCV, _CANDIDATES, _MAX_WEIGHT


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(_CONTEXTS[ticker.strip().upper()]),
    )

    def _fake_get_price_data(ticker, as_of_date=None):
        t = (ticker or "").strip().upper()
        if t not in _PRICE_OHLCV:
            return {"ticker": t, "success": False, "error": "no synthetic series", "ohlcv": None}
        return {"ticker": t, "success": True, "error": None, "ohlcv": copy.deepcopy(_PRICE_OHLCV[t])}

    monkeypatch.setattr(portfolio_optimizer, "get_price_data", _fake_get_price_data)
    return None


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def _build_body(**overrides):
    body = {
        "candidate_tickers": _CANDIDATES,
        "as_of_date": "2024-06-01",
        "top_k": 25,
        "max_weight": _MAX_WEIGHT,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# /api/universe/screen
# --------------------------------------------------------------------------- #
def test_universe_screen_returns_expected_shape(client, monkeypatch):
    from app.api import universe as universe_api

    fake_constituents = [
        {"ticker": "AAPL", "company_name": "Apple Inc.", "gics_sector": "Information Technology"},
        {"ticker": "MSFT", "company_name": "Microsoft Corp.", "gics_sector": "Information Technology"},
        {"ticker": "JPM", "company_name": "JPMorgan Chase", "gics_sector": "Financials"},
    ]

    def _fake_screen_universe(sectors=None, market_cap_tiers=None, as_of_date=None):
        rows = [r for r in fake_constituents if sectors is None or r["gics_sector"] in sectors]
        return [
            {**r, "market_cap": 1.0e12, "market_cap_tier": "mega"}
            for r in rows
        ]

    monkeypatch.setattr(universe_api, "screen_universe", _fake_screen_universe)

    resp = client.get(
        "/api/universe/screen",
        query_string={"sectors": "Information Technology", "market_cap_tiers": "mega"},
    )
    assert resp.status_code == 200
    data = resp.json
    assert isinstance(data, list)
    assert {r["ticker"] for r in data} == {"AAPL", "MSFT"}
    for row in data:
        assert set(row) == {"ticker", "company_name", "gics_sector", "market_cap", "market_cap_tier"}


def test_universe_screen_unknown_tier_is_400(client, monkeypatch):
    from app.api import universe as universe_api

    def _raising(sectors=None, market_cap_tiers=None, as_of_date=None):
        raise ValueError(f"未知的市值档位: {set(market_cap_tiers)}，合法取值: ('mega', 'large', 'mid', 'small')")

    monkeypatch.setattr(universe_api, "screen_universe", _raising)

    resp = client.get("/api/universe/screen", query_string={"market_cap_tiers": "not_a_real_tier"})
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "error" in resp.json


# --------------------------------------------------------------------------- #
# /api/portfolio/graph
# --------------------------------------------------------------------------- #
def test_portfolio_graph_returns_seed_dict(client, patched):
    from app.services.seed_builder import build_seed_from_ticker

    resp = client.get("/api/portfolio/graph", query_string={"ticker": "AAPL", "as_of_date": "2024-06-01"})
    assert resp.status_code == 200

    expected = build_seed_from_ticker("AAPL", "2024-06-01").to_dict()
    assert resp.json == expected
    assert len(resp.json["entities"]) > 0
    assert all("related_edges" in e for e in resp.json["entities"])


def test_portfolio_graph_missing_ticker_is_400(client):
    resp = client.get("/api/portfolio/graph")
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "ticker" in resp.json["error"]


def test_portfolio_graph_bad_ticker_is_400(client, monkeypatch):
    from app.api import portfolio as portfolio_api
    from app.services.seed_builder import SeedBuildError

    def _raising(ticker, as_of_date=None):
        raise SeedBuildError(f"could not build a seed for {ticker!r}")

    monkeypatch.setattr(portfolio_api, "build_seed_from_ticker", _raising)

    resp = client.get("/api/portfolio/graph", query_string={"ticker": "ZZZZINVALID"})
    assert resp.status_code == 400
    assert resp.json["success"] is False


# --------------------------------------------------------------------------- #
# /api/portfolio/build
# --------------------------------------------------------------------------- #
def test_portfolio_build_matches_direct_call(client, patched):
    resp = client.post("/api/portfolio/build", json=_build_body())
    assert resp.status_code == 200

    expected = build_portfolio(
        _CANDIDATES, as_of_date="2024-06-01", top_k=25, max_weight=_MAX_WEIGHT,
    )
    assert resp.json == expected

    # sanity on the well-known Phase 5e result shape
    assert "JPM" not in resp.json["weights"]
    assert "GS" not in resp.json["weights"]
    assert abs(sum(resp.json["weights"].values()) - 1.0) < 1e-4


def test_portfolio_build_model_param_matches_direct_call(client, patched):
    resp = client.post("/api/portfolio/build", json=_build_body(model="hrp"))
    assert resp.status_code == 200

    expected = build_portfolio(
        _CANDIDATES, as_of_date="2024-06-01", top_k=25, max_weight=_MAX_WEIGHT, model="hrp",
    )
    assert resp.json == expected
    assert resp.json["portfolio"]["model"] == "hrp"


def test_portfolio_build_unknown_model_is_400(client, patched):
    resp = client.post("/api/portfolio/build", json=_build_body(model="not_a_real_model"))
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "model" in resp.json["error"]


def test_portfolio_build_missing_candidate_tickers_is_400(client, patched):
    resp = client.post("/api/portfolio/build", json={"top_k": 25, "max_weight": 0.2})
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "candidate_tickers" in resp.json["error"]


def test_portfolio_build_infeasible_cap_is_400(client, patched):
    resp = client.post(
        "/api/portfolio/build",
        json=_build_body(candidate_tickers=["AAPL", "MSFT", "KO"], max_weight=0.20),
    )
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "max_weight" in resp.json["error"]


def test_portfolio_build_malformed_body_is_400(client):
    resp = client.post(
        "/api/portfolio/build",
        data="not json",
        content_type="text/plain",
    )
    assert resp.status_code == 400
    assert resp.json["success"] is False


# --------------------------------------------------------------------------- #
# /api/portfolio/ask
# --------------------------------------------------------------------------- #
def test_portfolio_ask_returns_answer(client, patched, monkeypatch):
    portfolio = build_portfolio(_CANDIDATES, as_of_date="2024-06-01", top_k=25, max_weight=_MAX_WEIGHT)

    monkeypatch.setattr(LLMClient, "chat", lambda self, messages, **kw: "MSFT is at the 20% cap.")

    resp = client.post(
        "/api/portfolio/ask",
        json={"question": "Why does MSFT have 20% weight?", "portfolio": portfolio},
    )
    assert resp.status_code == 200
    assert resp.json == {"answer": "MSFT is at the 20% cap."}


def test_portfolio_ask_not_found_ticker_needs_no_llm(client, patched, monkeypatch):
    portfolio = build_portfolio(_CANDIDATES, as_of_date="2024-06-01", top_k=25, max_weight=_MAX_WEIGHT)

    def _boom(self, messages, **kw):
        raise AssertionError("LLM should not be called for a not-found ticker")

    monkeypatch.setattr(LLMClient, "chat", _boom)

    resp = client.post(
        "/api/portfolio/ask",
        json={"question": "Why is TSLA not in this portfolio?", "portfolio": portfolio},
    )
    assert resp.status_code == 200
    assert "TSLA" in resp.json["answer"]


def test_portfolio_ask_missing_question_is_400(client):
    resp = client.post("/api/portfolio/ask", json={"portfolio": {}})
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "question" in resp.json["error"]


def test_portfolio_ask_malformed_portfolio_is_400(client):
    resp = client.post("/api/portfolio/ask", json={"question": "why?", "portfolio": "not-a-dict"})
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "portfolio" in resp.json["error"]


# --------------------------------------------------------------------------- #
# /api/portfolio/debate
# --------------------------------------------------------------------------- #
def test_portfolio_debate_returns_transcript(client, patched):
    portfolio = build_portfolio(_CANDIDATES, as_of_date="2024-06-01", top_k=25, max_weight=_MAX_WEIGHT)

    resp = client.post(
        "/api/portfolio/debate",
        json={"ticker": "MSFT", "portfolio": portfolio, "rounds": 2, "use_llm": False},
    )
    assert resp.status_code == 200
    data = resp.json
    assert data["ticker"] == "MSFT"
    assert data["rounds"] == 2
    assert data["used_llm"] is False
    assert len(data["personas"]) == 6
    assert len(data["statements"]) == 2
    assert all(len(rnd) == 6 for rnd in data["statements"])
    assert "gamma_verdict" in data


def test_portfolio_debate_ticker_not_in_portfolio_is_400(client, patched):
    portfolio = build_portfolio(_CANDIDATES, as_of_date="2024-06-01", top_k=25, max_weight=_MAX_WEIGHT)

    resp = client.post(
        "/api/portfolio/debate",
        json={"ticker": "TSLA", "portfolio": portfolio, "rounds": 2, "use_llm": False},
    )
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "TSLA" in resp.json["error"]


def test_portfolio_debate_missing_ticker_is_400(client):
    resp = client.post("/api/portfolio/debate", json={"portfolio": {}, "rounds": 2})
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "ticker" in resp.json["error"]


def test_portfolio_debate_bad_rounds_type_is_400(client, patched):
    portfolio = build_portfolio(_CANDIDATES, as_of_date="2024-06-01", top_k=25, max_weight=_MAX_WEIGHT)

    resp = client.post(
        "/api/portfolio/debate",
        json={"ticker": "MSFT", "portfolio": portfolio, "rounds": "three"},
    )
    assert resp.status_code == 400
    assert resp.json["success"] is False
    assert "rounds" in resp.json["error"]
