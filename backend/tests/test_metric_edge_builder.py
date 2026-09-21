"""
Fase 9 (baru) — tes `metric_edge_builder.py` (edge Company-to-Company
DITURUNKAN DARI METRIK, TIDAK ADA LLM sama sekali).

Persis docs/design/fase9a_metric_based_edges.md. Semua data harga/fundamental
SINTETIS DAN DETERMINISTIK (mengikuti pola factor+idio-noise yang sama dengan
`test_portfolio_optimizer.py`), tidak ada jaringan. `seed_builder.get_stock_context`
di-patch untuk tes `build_universe_seed` (pola sama dengan `test_entity_edge_builder.py`).

`test_no_llm_calls_anywhere` menegaskan eksplisit: modul ini bebas LLM -
patch kedua entry point LLM yang ada di codebase (`app.utils.llm_client.LLMClient.chat`,
`app.utils.openai_chat_compat.create_chat_completion`) supaya raise kalau
terpanggil, jalankan seluruh pipeline, assert `mock.assert_not_called()`.
"""

import copy
import logging
import math
from contextlib import contextmanager
from typing import List
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.services import metric_edge_builder as meb
from app.services import portfolio_optimizer, seed_builder
from app.services.financial_entity_extractor import ENTITY_TYPE_COMPANY
from app.services.metric_edge_builder import (
    RELATION_FUNDAMENTALLY_SIMILAR,
    RELATION_PRICE_CORRELATED,
    RELATION_SAME_SECTOR,
    build_universe_seed,
    compute_fundamental_similarity_edges,
    compute_price_correlation_edges,
    compute_sector_edges,
)

# --------------------------------------------------------------------------- #
# Helper - harga sintetis via model faktor (pola sama dengan test_portfolio_optimizer.py)
# --------------------------------------------------------------------------- #
_N_DAYS = 320
_TDAYS = 252


def _make_dates():
    return np.busday_offset(
        np.datetime64("2024-05-31"), -np.arange(_N_DAYS)[::-1], roll="backward"
    ).astype("datetime64[D]")


def _make_factor_prices(betas, factor_names, idio_vol=0.02, seed=20240601):
    """betas: {ticker: {factor_name: beta}}. Return dari kombinasi linear faktor
    (independen satu sama lain) + noise idiosinkratik kecil -> korelasi antar
    ticker TERKONTROL lewat faktor mana yang dibagi bersama, bukan cuma beda
    skala beta di faktor yang sama (yang HAMPIR SELALU tetap ~1.0 kalau faktornya
    tunggal dan idio kecil)."""
    rng = np.random.default_rng(seed)
    dates = _make_dates()
    factors = {fn: rng.normal(0.0, 1.0 / np.sqrt(_TDAYS), _N_DAYS) for fn in factor_names}
    out = {}
    for ticker, b in betas.items():
        daily = np.zeros(_N_DAYS)
        for fn, beta in b.items():
            daily = daily + beta * factors[fn]
        if idio_vol:
            daily = daily + rng.normal(0.0, idio_vol / np.sqrt(_TDAYS), _N_DAYS)
        closes = 100.0 * np.exp(np.cumsum(daily))
        out[ticker] = [{"date": str(d), "close": round(float(c), 4)} for d, c in zip(dates, closes)]
    return out


def _patch_prices(monkeypatch, price_series):
    def _fake_get_price_data(ticker, as_of_date=None):
        t = (ticker or "").strip().upper()
        if t not in price_series:
            return {"ticker": t, "success": False, "error": "no synthetic series", "ohlcv": None}
        return {"ticker": t, "success": True, "error": None, "ohlcv": copy.deepcopy(price_series[t])}
    monkeypatch.setattr(portfolio_optimizer, "get_price_data", _fake_get_price_data)


# --------------------------------------------------------------------------- #
# Helper - fundamental sintetis
# --------------------------------------------------------------------------- #
def _fund(sector="Technology", industry="Software - Infrastructure", success=True, **overrides):
    base = {
        "success": success, "error": None,
        "sector": sector, "industry": industry,
        "pe_ratio": 25.0, "pb_ratio": 8.0, "market_cap": 5e11,
        "revenue_growth_yoy": 0.10, "profit_margin": 0.25, "roe": 0.30,
        "debt_to_equity": 0.5, "eps_growth": 0.12, "dividend_yield": 0.01,
    }
    base.update(overrides)
    return base


def _patch_fundamentals(monkeypatch, fundamentals):
    def _fake(ticker, as_of_date=None):
        t = (ticker or "").strip().upper()
        return fundamentals.get(t, {"success": False, "error": "no synthetic fundamental"})
    monkeypatch.setattr(meb, "get_fundamental_data", _fake)


class _ListLogHandler(logging.Handler):
    """`get_logger` (app/utils/logger.py) memanggil `logger.propagate = False`
    pada tiap named logger (menghindari duplikasi output console/file) - itu
    juga berarti pytest `caplog` (yang menangkap via handler di root logger)
    TIDAK PERNAH melihat record dari logger ini, bahkan setelah `propagate`
    di-flip ke True (diverifikasi langsung - interaksi caplog/propagate/root
    di setup logger custom ini tidak reliable). Solusi robust: attach handler
    SENDIRI langsung ke logger modul ini, lepas lagi setelah tes selesai."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records: list = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def _capture_module_logs():
    handler = _ListLogHandler()
    logger = logging.getLogger("mirofish.metric_edge_builder")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def _messages(handler) -> List[str]:
    return [r.getMessage() for r in handler.records]


def _edge_between(edges, a, b):
    key = tuple(sorted((a, b)))
    hits = [e for e in edges if tuple(sorted((e["ticker_a"], e["ticker_b"]))) == key]
    return hits[0] if hits else None


# --------------------------------------------------------------------------- #
# 1) price_correlated - mutual top-K (universe kecil, top-K jadi no-op)
# --------------------------------------------------------------------------- #
def test_correlation_mutual_topk_both_ranks_filled(monkeypatch):
    betas = {
        "AAA": {"F1": 0.6}, "BBB": {"F1": 0.6},
        "CCC": {}, "DDD": {}, "EEE": {},
    }
    _patch_prices(monkeypatch, _make_factor_prices(betas, ["F1"]))

    edges = compute_price_correlation_edges(list(betas), as_of_date="2024-06-01")
    edge = _edge_between(edges, "AAA", "BBB")
    assert edge is not None
    assert edge["attributes"]["correlation_value"] > 0.3
    assert edge["attributes"]["rank_a"] is not None
    assert edge["attributes"]["rank_b"] is not None
    assert edge["attributes"]["symmetric"] is True
    assert edge["relation_type"] == RELATION_PRICE_CORRELATED


# --------------------------------------------------------------------------- #
# 2) price_correlated - union: 1 sisi menominasikan, sisi lain tidak
# --------------------------------------------------------------------------- #
def test_correlation_union_one_sided_nomination(monkeypatch):
    # AAA hanya berkorelasi (sedang) dengan BBB lewat F_AB. BBB JUGA berkorelasi
    # LEBIH KUAT dengan 5 ticker X lain lewat faktor TERPISAH F_BX - jadi top-5
    # BBB terisi penuh oleh X1..X5, AAA tidak masuk top-5 BBB, TAPI BBB tetap
    # masuk top-5 AAA (satu-satunya peer AAA yang berkorelasi berarti).
    betas = {
        "AAA": {"F_AB": 0.5},
        "BBB": {"F_AB": 0.5, "F_BX": 0.6},
        "X1": {"F_BX": 0.6}, "X2": {"F_BX": 0.6}, "X3": {"F_BX": 0.6},
        "X4": {"F_BX": 0.6}, "X5": {"F_BX": 0.6},
    }
    _patch_prices(monkeypatch, _make_factor_prices(betas, ["F_AB", "F_BX"], idio_vol=0.01))

    edges = compute_price_correlation_edges(list(betas), as_of_date="2024-06-01")
    edge = _edge_between(edges, "AAA", "BBB")
    assert edge is not None, "AAA seharusnya tetap menominasikan BBB di top-5 nya sendiri"

    ticker_a, ticker_b = edge["ticker_a"], edge["ticker_b"]
    assert (ticker_a, ticker_b) == ("AAA", "BBB")  # alfabetis
    assert edge["attributes"]["rank_a"] is not None, "AAA HARUS menominasikan BBB"
    assert edge["attributes"]["rank_b"] is None, "BBB TIDAK menominasikan AAA (kalah oleh X1..X5)"


# --------------------------------------------------------------------------- #
# 3) price_correlated - korelasi negatif kuat tetap masuk top-K (ranking |value|)
# --------------------------------------------------------------------------- #
def test_correlation_negative_ranked_by_absolute_value(monkeypatch):
    # OILCO berkorelasi KUAT NEGATIF dgn SOLARCO (-0.85 lewat F1, sign terbalik),
    # dan LEMAH POSITIF dgn 6 ticker W (0.10-0.26 lewat mixing rho*F1 + independen).
    # Ranking RAW (tanpa abs) akan menaruh SEMUA W di atas SOLARCO (positif >
    # negatif) -> SOLARCO tersingkir dari top-5. Ranking by |value| yang BENAR
    # harus menempatkan SOLARCO di posisi #1 (magnitude 0.85 > semua W).
    betas = {
        "OILCO": {"F1": 1.0},
        "SOLARCO": {"F1": -0.85, "G_SOLARCO": math.sqrt(1 - 0.85 ** 2)},
    }
    rhos = [0.15, 0.18, 0.20, 0.22, 0.24, 0.26]
    for i, rho in enumerate(rhos, start=1):
        betas[f"W{i}"] = {"F1": rho, f"G_W{i}": math.sqrt(1 - rho ** 2)}

    factor_names = ["F1"] + [f"G_{k}" for k in ["SOLARCO"] + [f"W{i}" for i in range(1, 7)]]
    _patch_prices(monkeypatch, _make_factor_prices(betas, factor_names, idio_vol=0.0))

    edges = compute_price_correlation_edges(list(betas), as_of_date="2024-06-01")
    edge = _edge_between(edges, "OILCO", "SOLARCO")
    assert edge is not None, "korelasi negatif kuat HARUS tetap masuk top-K"
    assert edge["attributes"]["correlation_value"] < -0.5, (
        "correlation_value harus tersimpan dengan TANDA ASLI negatif, bukan diabsolutkan"
    )
    rank_for_oilco = (
        edge["attributes"]["rank_a"] if edge["ticker_a"] == "OILCO" else edge["attributes"]["rank_b"]
    )
    assert rank_for_oilco == 1, "SOLARCO harus rank #1 di top-5 OILCO (magnitude terbesar)"


# --------------------------------------------------------------------------- #
# 4) fundamentally_similar - formula RMS distance manual (2 ticker, semua beda ->
#    z-score selalu tepat +-1, RMS = sqrt(mean(2^2 * 9)) = 2.0, score = 1/3)
# --------------------------------------------------------------------------- #
def test_fundamental_similarity_manual_rms_formula(monkeypatch):
    fundamentals = {
        "A": _fund(
            pe_ratio=20.0, pb_ratio=6.0, market_cap=1e11, revenue_growth_yoy=0.08,
            profit_margin=0.20, roe=0.25, debt_to_equity=0.4, eps_growth=0.10, dividend_yield=0.01,
        ),
        "B": _fund(
            pe_ratio=30.0, pb_ratio=10.0, market_cap=5e11, revenue_growth_yoy=0.15,
            profit_margin=0.30, roe=0.35, debt_to_equity=0.8, eps_growth=0.20, dividend_yield=0.03,
        ),
    }
    _patch_fundamentals(monkeypatch, fundamentals)

    edges = compute_fundamental_similarity_edges(list(fundamentals), as_of_date="2024-06-01")
    edge = _edge_between(edges, "A", "B")
    assert edge is not None
    # dengan HANYA 2 ticker di populasi, z-score SETIAP metrik yang beda selalu
    # persis +-1 (z = (v-mean)/std, std=|v1-v2|/2 dengan n=2) -> RMS distance atas
    # 9 metrik = sqrt(mean([2^2]*9)) = sqrt(4) = 2.0 -> similarity_score = 1/(1+2) = 1/3
    assert edge["attributes"]["similarity_score"] == pytest.approx(1.0 / 3.0, abs=1e-6)
    assert edge["attributes"]["n_metrics_compared"] == 9
    assert set(edge["attributes"]["metrics_compared"]) == set(meb._NUMERIC_METRICS)


# --------------------------------------------------------------------------- #
# 5) fundamentally_similar - overlap < 3 -> pasangan di-skip, alasan tercatat
# --------------------------------------------------------------------------- #
def test_fundamental_pair_skipped_when_overlap_below_minimum(monkeypatch):
    fundamentals = {
        "RICH": _fund(),  # 9/9 metrik terisi
        "POOR": _fund(
            pe_ratio=None, pb_ratio=None, roe=None, debt_to_equity=None,
            revenue_growth_yoy=None, profit_margin=None, eps_growth=None,
            dividend_yield=None,  # cuma market_cap yang terisi...
            market_cap=3e11,  # ...dan BEDA dari RICH (5e11) supaya TIDAK std=0/guarded
        ),  # -> overlap RICH^POOR = tepat 1 (market_cap)
    }
    _patch_fundamentals(monkeypatch, fundamentals)

    with _capture_module_logs() as handler:
        edges = compute_fundamental_similarity_edges(list(fundamentals), as_of_date="2024-06-01")

    assert edges == []
    assert any("overlap=1" in m and "minimum 3" in m for m in _messages(handler))


# --------------------------------------------------------------------------- #
# 6) fundamentally_similar - metrik std=0 di populasi -> guard, tidak crash
# --------------------------------------------------------------------------- #
def test_fundamental_std0_metric_guarded_not_crash(monkeypatch):
    # dividend_yield SAMA PERSIS di 3 ticker (-> std=0, guard) - 4 metrik lain
    # (pe_ratio/pb_ratio/market_cap/roe) BEDA supaya overlap tetap >= 3 valid.
    fundamentals = {
        "A": _fund(dividend_yield=0.02, pe_ratio=18.0, pb_ratio=6.0, market_cap=3e11, roe=0.20),
        "B": _fund(dividend_yield=0.02, pe_ratio=22.0, pb_ratio=7.0, market_cap=4e11, roe=0.25),
        "C": _fund(dividend_yield=0.02, pe_ratio=26.0, pb_ratio=8.0, market_cap=5e11, roe=0.30),
    }

    with _capture_module_logs() as handler:
        z, std0_metrics = meb._zscore_fundamentals(fundamentals, list(fundamentals))

    assert "dividend_yield" in std0_metrics
    assert z["dividend_yield"] == {}  # di-skip TOTAL, bukan division-by-zero
    assert set(z["pe_ratio"]) == set(fundamentals)  # metrik lain tetap normal
    assert any("std=0" in m for m in _messages(handler))

    _patch_fundamentals(monkeypatch, fundamentals)
    edges = compute_fundamental_similarity_edges(list(fundamentals), as_of_date="2024-06-01")
    assert len(edges) > 0  # tidak crash, tetap hasilkan edge dari 8 metrik valid lainnya


# --------------------------------------------------------------------------- #
# 7) log10(market_cap) WAJIB sebelum z-score
# --------------------------------------------------------------------------- #
def test_market_cap_log10_applied_before_zscore():
    # 3 ticker (n=2 secara matematis TIDAK BISA membedakan log vs raw - z-score
    # dgn 2 titik selalu +-1 utk transform monoton apa pun; butuh >=3 titik).
    fundamentals = {
        "SMALL": _fund(market_cap=1e10, pe_ratio=20.0),
        "MID": _fund(market_cap=1e11, pe_ratio=22.0),
        "BIG": _fund(market_cap=1e12, pe_ratio=24.0),  # 100x MID, 10x MID lagi
    }
    tickers = list(fundamentals)
    z, std0_metrics = meb._zscore_fundamentals(fundamentals, tickers)
    assert "market_cap" not in std0_metrics

    # Expected z-score dihitung ULANG di sini dari LOG10(market_cap) secara
    # independen dari implementasi - membuktikan log10 terjadi SEBELUM z-score.
    log_caps = {t: math.log10(fundamentals[t]["market_cap"]) for t in tickers}
    mean = sum(log_caps.values()) / len(log_caps)
    var = sum((v - mean) ** 2 for v in log_caps.values()) / len(log_caps)
    std = math.sqrt(var)
    expected_log_z = {t: max(-3.0, min(3.0, (log_caps[t] - mean) / std)) for t in tickers}
    for t in tickers:
        assert z["market_cap"][t] == pytest.approx(expected_log_z[t], abs=1e-6)

    # Kontras: z-score dari market_cap MENTAH (tanpa log10) akan berbeda nyata
    # dari yang dihasilkan implementasi - membuktikan implementasi TIDAK memakai
    # nilai mentah.
    raw_caps = {t: fundamentals[t]["market_cap"] for t in tickers}
    raw_mean = sum(raw_caps.values()) / len(raw_caps)
    raw_var = sum((v - raw_mean) ** 2 for v in raw_caps.values()) / len(raw_caps)
    raw_std = math.sqrt(raw_var)
    raw_z_big = (raw_caps["BIG"] - raw_mean) / raw_std
    assert z["market_cap"]["BIG"] != pytest.approx(raw_z_big, abs=1e-3)


# --------------------------------------------------------------------------- #
# 8) same_sector - grup besar (> top-K) dibatasi, TIDAK full pairwise
# --------------------------------------------------------------------------- #
def test_sector_edges_capped_for_large_group(monkeypatch):
    tickers = [f"SEC{i}" for i in range(8)]
    betas = {t: {"F1": 0.25 + 0.05 * i} for i, t in enumerate(tickers)}
    _patch_prices(monkeypatch, _make_factor_prices(betas, ["F1"], idio_vol=0.05))
    _patch_fundamentals(
        monkeypatch, {t: _fund(sector="Materials", industry="Widgets") for t in tickers}
    )

    matrix = meb._price_correlation_matrix(tickers, as_of_date="2024-06-01")
    edges = compute_sector_edges(tickers, "2024-06-01", matrix["pair_values"])

    full_pairwise = len(tickers) * (len(tickers) - 1) // 2
    assert full_pairwise == 28
    assert 0 < len(edges) <= 5 * len(tickers)  # bound atas K*N (bagian 3 dokumen)
    assert len(edges) < full_pairwise, "cap top-K harus benar-benar mengurangi dari full pairwise"
    for e in edges:
        assert e["attributes"]["shared_sector"] is True
        assert e["attributes"]["shared_sub_industry"] is True
        assert e["relation_type"] == RELATION_SAME_SECTOR


# --------------------------------------------------------------------------- #
# 9) same_sector - grup kecil (<= top-K) -> top-K jadi no-op, semua terhubung
# --------------------------------------------------------------------------- #
def test_sector_edges_small_group_fully_connected(monkeypatch):
    tickers = ["S1", "S2", "S3", "S4"]  # 4 <= K=5
    betas = {t: {"F1": 0.2 + 0.1 * i} for i, t in enumerate(tickers)}
    _patch_prices(monkeypatch, _make_factor_prices(betas, ["F1"], idio_vol=0.05))
    _patch_fundamentals(
        monkeypatch, {t: _fund(sector="Materials", industry="Widgets") for t in tickers}
    )

    matrix = meb._price_correlation_matrix(tickers, as_of_date="2024-06-01")
    edges = compute_sector_edges(tickers, "2024-06-01", matrix["pair_values"])

    full_pairwise = len(tickers) * (len(tickers) - 1) // 2
    pairs_found = {tuple(sorted((e["ticker_a"], e["ticker_b"]))) for e in edges}
    assert len(pairs_found) == full_pairwise == 6


# --------------------------------------------------------------------------- #
# 10) 1 pasangan lolos ketiga kriteria -> 3 edge TERPISAH, bukan 1 campuran
# --------------------------------------------------------------------------- #
def test_three_separate_edges_when_pair_qualifies_for_all(monkeypatch):
    tickers = ["SIM1", "SIM2", "F1T", "F2T", "F3T"]
    betas = {
        "SIM1": {"F": 0.6}, "SIM2": {"F": 0.6},
        "F1T": {}, "F2T": {}, "F3T": {},
    }
    _patch_prices(monkeypatch, _make_factor_prices(betas, ["F"], idio_vol=0.02))
    # 5 metrik (pe_ratio/pb_ratio/market_cap/roe/debt_to_equity) BEDA per ticker
    # supaya tidak ada yang std=0/guarded (overlap tiap pasangan = 5 >= minimum 3);
    # 4 metrik lain dibiarkan default (identik, guarded away, tidak masalah).
    fundamentals = {
        "SIM1": _fund(sector="Tech", industry="Same-Industry",
                       pe_ratio=20.0, pb_ratio=7.0, market_cap=4e11, roe=0.30, debt_to_equity=0.4),
        "SIM2": _fund(sector="Tech", industry="Same-Industry",
                       pe_ratio=21.0, pb_ratio=7.5, market_cap=4.5e11, roe=0.31, debt_to_equity=0.45),
        "F1T": _fund(sector="Other", industry="Other-Industry",
                      pe_ratio=50.0, pb_ratio=2.0, market_cap=1e10, roe=0.05, debt_to_equity=1.2),
        "F2T": _fund(sector="Other", industry="Other-Industry",
                      pe_ratio=55.0, pb_ratio=2.5, market_cap=1.5e10, roe=0.04, debt_to_equity=1.3),
        "F3T": _fund(sector="Other", industry="Other-Industry",
                      pe_ratio=60.0, pb_ratio=3.0, market_cap=2e10, roe=0.03, debt_to_equity=1.4),
    }
    _patch_fundamentals(monkeypatch, fundamentals)

    corr_matrix = meb._price_correlation_matrix(tickers, as_of_date="2024-06-01")
    price_edges = meb._edges_from_correlation_matrix(corr_matrix, as_of_date="2024-06-01")
    fund_edges = compute_fundamental_similarity_edges(tickers, as_of_date="2024-06-01")
    sector_edges = compute_sector_edges(tickers, "2024-06-01", corr_matrix["pair_values"])

    price_hit = _edge_between(price_edges, "SIM1", "SIM2")
    fund_hit = _edge_between(fund_edges, "SIM1", "SIM2")
    sector_hit = _edge_between(sector_edges, "SIM1", "SIM2")
    assert price_hit and fund_hit and sector_hit

    relation_types = {price_hit["relation_type"], fund_hit["relation_type"], sector_hit["relation_type"]}
    assert relation_types == {RELATION_PRICE_CORRELATED, RELATION_FUNDAMENTALLY_SIMILAR, RELATION_SAME_SECTOR}
    assert price_hit is not fund_hit is not sector_hit  # 3 objek edge BERBEDA

    # tidak ada field campuran nyasar lintas tipe
    assert "similarity_score" not in price_hit["attributes"]
    assert "correlation_value" not in fund_hit["attributes"]
    assert "similarity_score" not in sector_hit["attributes"]


# --------------------------------------------------------------------------- #
# Helper bersama untuk tes 11/12 (build_universe_seed end-to-end)
# --------------------------------------------------------------------------- #
def _stock_context(ticker, company_name, sector, industry):
    return {
        "ticker": ticker, "company_name": company_name, "as_of_date": "2024-06-01",
        "success": True,
        "price": {
            "ticker": ticker, "success": True, "error": None, "period": "1y",
            "as_of_date": "2024-06-01", "latest_close": 100.0, "latest_date": "2024-05-31",
            "stats": {"52w_high": 110.0, "52w_low": 80.0},
            "technical_indicators": {
                "price_vs_sma50": 3.0, "price_vs_sma200": 4.0, "rsi_14": 55.0,
                "macd_signal": {"macd": 1.0, "signal": 0.9, "histogram": 0.1},
                "bollinger_position": 0.5, "volume_vs_avg": 1.1, "change_1d_pct": 0.2,
            },
        },
        "fundamental": {
            "ticker": ticker, "success": True, "error": None, "as_of_date": "2024-06-01",
            "is_point_in_time": False, "warning": "nilai terkini, bukan snapshot historis",
            "company_name": company_name,
            "pe_ratio": 25.0, "pb_ratio": 8.0, "market_cap": 5e11,
            "sector": sector, "industry": industry,
            "revenue_growth_yoy": 0.10, "profit_margin": 0.25, "roe": 0.30,
            "debt_to_equity": 0.5, "eps_growth": 0.12, "dividend_yield": 0.01,
            "skipped_fields": {},
        },
    }


def _build_universe_fixture(monkeypatch):
    tickers = ["AAPL", "ORCL"]
    contexts = {
        "AAPL": _stock_context("AAPL", "Apple Inc.", "Technology", "Same-Industry"),
        "ORCL": _stock_context("ORCL", "Oracle Corp.", "Technology", "Same-Industry"),
    }
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(contexts[ticker.strip().upper()]),
    )
    betas = {"AAPL": {"F": 0.6}, "ORCL": {"F": 0.6}}
    _patch_prices(monkeypatch, _make_factor_prices(betas, ["F"], idio_vol=0.02))
    # >=3 metrik BEDA antara AAPL/ORCL (dengan cuma 2 ticker, metrik APA PUN yang
    # identik otomatis std=0/guarded - lihat test_fundamental_std0_metric_guarded)
    fundamentals = {
        "AAPL": _fund(sector="Technology", industry="Same-Industry",
                       pe_ratio=20.0, pb_ratio=7.0, market_cap=4e11, roe=0.30, debt_to_equity=0.4),
        "ORCL": _fund(sector="Technology", industry="Same-Industry",
                       pe_ratio=21.0, pb_ratio=7.5, market_cap=4.5e11, roe=0.31, debt_to_equity=0.45),
    }
    _patch_fundamentals(monkeypatch, fundamentals)
    return tickers


_METRIC_EDGE_NAMES = {RELATION_PRICE_CORRELATED, RELATION_FUNDAMENTALLY_SIMILAR, RELATION_SAME_SECTOR}


# --------------------------------------------------------------------------- #
# 11) outgoing/incoming tersimpan di KEDUA node, attributes identik
# --------------------------------------------------------------------------- #
def test_build_universe_seed_edges_symmetric_outgoing_incoming(monkeypatch):
    tickers = _build_universe_fixture(monkeypatch)
    seed = build_universe_seed(tickers, as_of_date="2024-06-01")

    companies = {n.attributes["ticker"]: n for n in seed.entities if n.get_entity_type() == ENTITY_TYPE_COMPANY}
    aapl, orcl = companies["AAPL"], companies["ORCL"]

    outgoing_from_aapl = [
        e for e in aapl.related_edges
        if e["direction"] == "outgoing" and e["edge_name"] in _METRIC_EDGE_NAMES
    ]
    incoming_to_orcl = [
        e for e in orcl.related_edges
        if e["direction"] == "incoming" and e["edge_name"] in _METRIC_EDGE_NAMES
    ]
    assert len(outgoing_from_aapl) == 3  # ketiga jenis lolos (fixture didesain begitu)
    assert len(incoming_to_orcl) == 3

    for out_edge in outgoing_from_aapl:
        match = next(e for e in incoming_to_orcl if e["edge_name"] == out_edge["edge_name"])
        assert match["fact"] == out_edge["fact"]
        assert match["attributes"] == out_edge["attributes"]
        assert match["source_node_uuid"] == aapl.uuid
        assert out_edge["target_node_uuid"] == orcl.uuid

    # related_nodes dua arah konsisten (konvensi Fase 2c)
    assert any(rn["uuid"] == orcl.uuid for rn in aapl.related_nodes)
    assert any(rn["uuid"] == aapl.uuid for rn in orcl.related_nodes)


# --------------------------------------------------------------------------- #
# 12) bintang Fase 2 tiap ticker tetap UTUH setelah edge metrik di-append
# --------------------------------------------------------------------------- #
def test_build_universe_seed_preserves_phase2_star(monkeypatch):
    tickers = _build_universe_fixture(monkeypatch)
    seed = build_universe_seed(tickers, as_of_date="2024-06-01")

    companies = {n.attributes["ticker"]: n for n in seed.entities if n.get_entity_type() == ENTITY_TYPE_COMPANY}
    for ticker, company in companies.items():
        star_edges = [e for e in company.related_edges if e["edge_name"] in ("has_sector", "has_metric", "has_signal")]
        star_edge_names = {e["edge_name"] for e in star_edges}
        assert star_edge_names == {"has_sector", "has_metric", "has_signal"}, (
            f"bintang Fase 2c {ticker} harus tetap utuh setelah edge metrik ditambahkan"
        )
        # Company node tetap punya entitas non-Company terkait di seed gabungan
        non_company_types = {
            n.get_entity_type() for n in seed.entities
            if n.get_entity_type() != ENTITY_TYPE_COMPANY
        }
        assert {"Sector", "ValuationMetric", "FundamentalMetric", "TechnicalSignal"} <= non_company_types

    # edge metrik hanya APPEND, bukan REPLACE - company punya has_* DAN price_correlated dkk sekaligus
    aapl = companies["AAPL"]
    all_edge_names = {e["edge_name"] for e in aapl.related_edges}
    assert _METRIC_EDGE_NAMES <= all_edge_names
    assert {"has_sector", "has_metric", "has_signal"} <= all_edge_names


# --------------------------------------------------------------------------- #
# 13) TIDAK ADA LLM - bukti eksplisit
# --------------------------------------------------------------------------- #
def test_no_llm_calls_anywhere(monkeypatch):
    """
    Patch di titik PALING RENDAH yang tidak bisa dihindari jalur manapun:
    `openai.OpenAI.__init__` (constructor client) DAN
    `openai.resources.chat.completions.Completions.create` (method panggilan
    completion pada instance manapun). SENGAJA BUKAN patch ke
    `app.utils.openai_chat_compat.create_chat_completion` atau
    `app.utils.llm_client.LLMClient.chat` — keduanya adalah fungsi WRAPPER
    yang di-bind SECARA LOKAL oleh modul lain lewat `from X import Y` (mis.
    `oasis_profile_generator.py` sudah bind `create_chat_completion` ke
    namespace-nya sendiri SAAT IMPORT, sebelum test ini jalan) - patch di
    lokasi asal wrapper TIDAK mencegat panggilan lewat reference lokal yang
    sudah ter-bind itu ("patch where used, not where defined"). Konstruktor
    `openai.OpenAI` dan method `Completions.create` TIDAK bisa dihindari
    dengan cara ini - SEMUA jalur (langsung maupun lewat wrapper/reference
    lokal manapun) pada akhirnya harus lewat kelas `openai.OpenAI` yang sama.
    """
    tickers = _build_universe_fixture(monkeypatch)

    with patch(
        "openai.OpenAI.__init__",
        new=MagicMock(side_effect=AssertionError("openai.OpenAI() TIDAK BOLEH di-construct di Fase 9 (baru)")),
    ) as mock_openai_init, patch(
        "openai.resources.chat.completions.Completions.create",
        new=MagicMock(side_effect=AssertionError("chat.completions.create TIDAK BOLEH terpanggil di Fase 9 (baru)")),
    ) as mock_completions_create:
        seed = build_universe_seed(tickers, as_of_date="2024-06-01")
        compute_price_correlation_edges(tickers, as_of_date="2024-06-01")
        compute_fundamental_similarity_edges(tickers, as_of_date="2024-06-01")
        matrix = meb._price_correlation_matrix(tickers, as_of_date="2024-06-01")
        compute_sector_edges(tickers, "2024-06-01", matrix["pair_values"])

    mock_openai_init.assert_not_called()
    mock_completions_create.assert_not_called()
    assert seed.total_count > 0  # pipeline tetap jalan penuh tanpa LLM
