"""
Phase 5e — tes `build_returns_matrix` / `optimize_portfolio` / `build_portfolio`.

Pakai set 9 ticker yang sama dengan test Phase 5b (`test_consensus_screener`),
yang sudah menunjukkan JPM & GS (conventional finance) tersaring keluar sebagai
non-compliant. Di sini kita jalankan `build_portfolio()` end-to-end:

  screen_and_rank (5b, LLM-free)  ->  optimize_portfolio (skfolio MeanRisk)

Offline: `seed_builder.get_stock_context` di-patch dengan fixture Phase 5b;
`portfolio_optimizer.get_price_data` di-patch dengan seri harga sintetis
deterministik (GBM + faktor pasar bersama supaya ada korelasi realistis).
Tidak ada jaringan.

Yang diverifikasi:
  - bobot akhir jumlah ~= 1.0, tidak ada yang > max_weight
  - JPM / GS tidak pernah sampai ke tahap optimasi (bukan di holdings, bukan di weights)
  - >= 1 dari 7 kandidat compliant dapat bobot ~0 (optimizer menilainya tidak
    efisien — ini sehat, bukan bug)
  - guard infeasibility (n_assets * max_weight < 1) melempar error yang jelas
  - consensus score ikut di output untuk transparansi TAPI tidak masuk math optimasi
"""

import copy

import numpy as np
import pytest

from app.services import portfolio_optimizer
from app.services import seed_builder
from app.services.portfolio_optimizer import (
    build_portfolio,
    build_returns_matrix,
    optimize_portfolio,
)
from tests.test_consensus_screener import _CONTEXTS as _CONSENSUS_CONTEXTS


_CANDIDATES = ["AAPL", "MSFT", "NVDA", "JPM", "GS", "KO", "XOM", "WMT", "PFE"]
_NON_COMPLIANT = {"JPM", "GS"}
_MAX_WEIGHT = 0.20

# Per-ticker (annual drift, annual vol, beta to a common market factor).
# PFE is deliberately dominated: negative drift + high idiosyncratic vol, so a
# max-Sharpe optimizer should give it ~0 weight (expected, not a bug).
_PROFILE = {
    "AAPL": (0.22, 0.26, 1.05),
    "MSFT": (0.25, 0.24, 1.00),
    "NVDA": (0.40, 0.45, 1.35),
    "KO":   (0.08, 0.15, 0.55),
    "XOM":  (0.10, 0.28, 0.80),
    "WMT":  (0.12, 0.18, 0.60),
    "PFE":  (-0.12, 0.34, 0.75),   # <- should be zeroed
    # JPM / GS never reach the optimizer (filtered by compliance), but give them
    # series anyway so a bug that let them through would still have data.
    "JPM":  (0.18, 0.30, 1.10),
    "GS":   (0.16, 0.32, 1.15),
}

_N_DAYS = 320
_TDAYS = 252


def _make_price_series(seed: int = 20240601):
    """Deterministic daily close series per ticker, ending 2024-05-31."""
    rng = np.random.default_rng(seed)
    dates = np.busday_offset(
        np.datetime64("2024-05-31"), -np.arange(_N_DAYS)[::-1], roll="backward"
    ).astype("datetime64[D]")
    market = rng.normal(0.06 / _TDAYS, 0.11 / np.sqrt(_TDAYS), _N_DAYS)  # common factor

    out = {}
    for ticker, (mu, vol, beta) in _PROFILE.items():
        idio_vol = np.sqrt(max(vol**2 - (beta * 0.11) ** 2, (0.05 * vol) ** 2)) / np.sqrt(_TDAYS)
        drift = mu / _TDAYS - 0.5 * (vol / np.sqrt(_TDAYS)) ** 2
        daily = drift + beta * market + rng.normal(0.0, idio_vol, _N_DAYS)
        closes = 100.0 * np.exp(np.cumsum(daily))
        out[ticker] = [
            {"date": str(d), "close": round(float(c), 4)}
            for d, c in zip(dates, closes)
        ]
    return out


_PRICE_OHLCV = _make_price_series()


@pytest.fixture
def patched(monkeypatch):
    # Phase 5b screen: offline seed graphs.
    monkeypatch.setattr(
        seed_builder, "get_stock_context",
        lambda ticker, as_of_date=None: copy.deepcopy(_CONSENSUS_CONTEXTS[ticker.strip().upper()]),
    )

    # Phase 5e prices: synthetic close series (default lookback "1y" -> get_price_data).
    def _fake_get_price_data(ticker, as_of_date=None):
        t = (ticker or "").strip().upper()
        if t not in _PRICE_OHLCV:
            return {"ticker": t, "success": False, "error": "no synthetic series", "ohlcv": None}
        return {"ticker": t, "success": True, "error": None, "ohlcv": copy.deepcopy(_PRICE_OHLCV[t])}

    monkeypatch.setattr(portfolio_optimizer, "get_price_data", _fake_get_price_data)
    # also trip-wire fetch_price_data so a non-1y lookback in a test is obvious
    monkeypatch.setattr(
        portfolio_optimizer, "fetch_price_data",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetch_price_data hit unexpectedly")),
    )
    return None


# --------------------------------------------------------------------------- #
# build_returns_matrix
# --------------------------------------------------------------------------- #
def test_build_returns_matrix_shape_and_alignment(patched):
    df = build_returns_matrix(["AAPL", "MSFT", "NVDA", "KO"], as_of_date="2024-06-01")
    assert list(df.columns) == ["AAPL", "MSFT", "NVDA", "KO"]
    assert df.notna().all().all()                  # no NaN gaps
    assert len(df) == _N_DAYS - 1                   # N closes -> N-1 returns
    assert df.index.is_monotonic_increasing
    # daily returns, so values are small
    assert df.abs().to_numpy().max() < 0.5


def test_build_returns_matrix_drops_short_history(patched, monkeypatch, caplog):
    short = {"date": "2024-05-31", "close": 100.0}
    base = _fake_series_for("AAPL")
    monkeypatch.setattr(
        portfolio_optimizer, "get_price_data",
        lambda ticker, as_of_date=None: (
            {"ticker": ticker, "success": True, "error": None, "ohlcv": [short] * 10}
            if ticker.upper() == "WMT"
            else {"ticker": ticker, "success": True, "error": None, "ohlcv": base}
        ),
    )
    rep = portfolio_optimizer._build_returns_with_report(
        ["AAPL", "WMT"], as_of_date="2024-06-01", lookback="1y",
    )
    assert "WMT" not in rep["returns"].columns
    assert [d["ticker"] for d in rep["dropped"]] == ["WMT"]
    assert "insufficient history" in rep["dropped"][0]["reason"]


def _fake_series_for(ticker):
    return copy.deepcopy(_PRICE_OHLCV[ticker])


# --------------------------------------------------------------------------- #
# optimize_portfolio — the infeasibility guard
# --------------------------------------------------------------------------- #
def test_optimize_portfolio_infeasible_cap_raises_clear_error(patched):
    with pytest.raises(ValueError) as ei:
        optimize_portfolio(["AAPL", "MSFT", "KO"], as_of_date="2024-06-01", max_weight=0.20)
    msg = str(ei.value)
    assert "3 compliant tickers" in msg
    assert "max_weight=0.2" in msg
    assert "at least 5" in msg
    assert "0.60 < 1.0" in msg
    assert "Reduce max_weight or provide more candidate tickers" in msg


def test_optimize_portfolio_respects_bounds_and_zeroes_inefficient(patched):
    seven = ["AAPL", "MSFT", "NVDA", "KO", "XOM", "WMT", "PFE"]
    out = optimize_portfolio(seven, as_of_date="2024-06-01", max_weight=_MAX_WEIGHT)

    w = out["weights"]
    assert abs(sum(w.values()) - 1.0) < 1e-4
    assert all(0 <= v <= _MAX_WEIGHT + 1e-6 for v in w.values())
    assert len(w) < len(seven)                       # at least one asset zeroed
    assert out["n_assets_optimized"] == 7
    assert out["objective"] == "MAXIMIZE_RATIO"
    for k in ("expected_return", "volatility", "sharpe_ratio"):
        assert isinstance(out[k], float)
    assert out["volatility"] > 0


# --------------------------------------------------------------------------- #
# build_portfolio — end-to-end headline test
# --------------------------------------------------------------------------- #
def test_build_portfolio_end_to_end(patched, capsys):
    result = build_portfolio(_CANDIDATES, as_of_date="2024-06-01", top_k=25, max_weight=_MAX_WEIGHT)

    weights = result["weights"]
    holdings = result["holdings"]
    p = result["portfolio"]

    with capsys.disabled():
        print("\n\n" + "=" * 94)
        print(f"Phase 5e — build_portfolio({len(_CANDIDATES)} candidates) @ 2024-06-01")
        print(f"  candidates          : {_CANDIDATES}")
        print(f"  screened+ranked (5b): {[h['ticker'] for h in holdings]}   "
              f"(JPM/GS excluded as non-compliant, never reach optimization)")
        print("=" * 94)
        print(f"\n  {'TICKER':<7} {'WEIGHT':>9}  {'CONSENSUS':>10}  {'RANK':>4}  "
              f"{'COMPLIANT':>9}  {'IN-OPT':>7}")
        print("  " + "-" * 60)
        for h in holdings:
            print(f"  {h['ticker']:<7} {h['weight'] * 100:>8.2f}%  {h['consensus_score']:>+10.3f}  "
                  f"{h['consensus_rank']:>4}  {str(h['passes_compliance']):>9}  "
                  f"{('yes' if h['in_optimization'] else 'no'):>7}")
        print("  " + "-" * 60)
        print(f"  {'SUM':<7} {sum(weights.values()) * 100:>8.2f}%")
        print(f"\n  portfolio expected return : {p['expected_return'] * 100:+.2f}%   (annualised)")
        print(f"  portfolio volatility      : {p['volatility'] * 100:+.2f}%   (annualised)")
        print(f"  portfolio Sharpe ratio    : {p['sharpe_ratio']:.3f}   "
              f"(rf={p['risk_free_rate']:.2%}, {p['objective']})")
        zeroed = [h["ticker"] for h in holdings if h["weight"] == 0.0]
        print(f"\n  compliant candidates given ~0 weight (inefficient, expected): {zeroed}")
        print()

    ranked_tickers = [h["ticker"] for h in holdings]

    # (1) JPM / GS never reach this stage
    for t in _NON_COMPLIANT:
        assert t not in ranked_tickers
        assert t not in weights
    assert set(ranked_tickers) == set(_CANDIDATES) - _NON_COMPLIANT
    assert len(holdings) == 7

    # (2) valid long-only fully-invested portfolio, per-asset cap respected
    assert abs(sum(weights.values()) - 1.0) < 1e-4
    assert all(0 < v <= _MAX_WEIGHT + 1e-6 for v in weights.values())

    # (3) at least one compliant candidate is zeroed by the optimizer (healthy)
    zeroed = [h["ticker"] for h in holdings if h["weight"] == 0.0]
    assert len(zeroed) >= 1
    assert len(weights) == 7 - len(zeroed)

    # (4) every holding row carries its compliance status + consensus score
    for h in holdings:
        assert h["passes_compliance"] is True
        assert h["compliance_verdict"] == "compliant"
        assert isinstance(h["consensus_score"], float)
    # holdings are in consensus-rank order (1..7), descending consensus score
    assert [h["consensus_rank"] for h in holdings] == list(range(1, 8))
    cs = [h["consensus_score"] for h in holdings]
    assert cs == sorted(cs, reverse=True)

    # (5) portfolio-level metrics are finite and sane
    assert np.isfinite([p["expected_return"], p["volatility"], p["sharpe_ratio"]]).all()
    assert p["volatility"] > 0
    assert p["risk_free_rate"] == 0.0             # default, flagged configurable

    # (6) Option A: consensus scores are reported but NOT the optimization input.
    # Prove it: the optimizer's weight ordering is NOT the consensus ordering
    # (if it were fed the scores, top consensus would ~ top weight).
    weight_order = [h["ticker"] for h in sorted(holdings, key=lambda x: -x["weight"])]
    consensus_order = [h["ticker"] for h in holdings]
    assert weight_order != consensus_order


def test_build_portfolio_consensus_not_in_optimization_math(patched, monkeypatch):
    """Same screened tickers, but perturb the consensus scores wildly -> the
    optimized weights must be byte-identical (scores don't touch the math)."""
    base = build_portfolio(_CANDIDATES, as_of_date="2024-06-01", max_weight=_MAX_WEIGHT)

    real_screen = portfolio_optimizer.screen_and_rank

    def _shuffled_scores(*args, **kwargs):
        rows = real_screen(*args, **kwargs)
        for i, r in enumerate(rows):
            r = dict(r)
            r["consensus_score"] = (-1.0) ** i * 0.99   # nonsense, but still compliant
            rows[i] = r
        return rows

    monkeypatch.setattr(portfolio_optimizer, "screen_and_rank", _shuffled_scores)
    perturbed = build_portfolio(_CANDIDATES, as_of_date="2024-06-01", max_weight=_MAX_WEIGHT)

    assert perturbed["weights"] == base["weights"]
    assert perturbed["portfolio"]["sharpe_ratio"] == base["portfolio"]["sharpe_ratio"]
