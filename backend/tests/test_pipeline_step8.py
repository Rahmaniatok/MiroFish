"""News pipeline step 8 — performance metrics and report (offline, synthetic prices)."""

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app import create_app
from app.news_pipeline import performance_metrics as pm
from app.news_pipeline import run_store, step7_consensus, step8_performance as s8
from tests.test_pipeline_step3 import run_id  # noqa: F401


def _prices(cols, n=60, start="2026-04-01"):
    idx = pd.bdate_range(start, periods=n)
    rng = np.random.default_rng(7)
    data = {c: 100 * np.cumprod(1 + rng.normal(0.001 * (i + 1), 0.01 * (i + 1), n)) for i, c in enumerate(cols)}
    return pd.DataFrame(data, index=idx)


def test_buy_and_hold_value_and_drawdown():
    p = pd.DataFrame({"A": [100, 110, 99, 121], "B": [50, 50, 50, 50]},
                     index=pd.bdate_range("2026-04-01", periods=4))
    v = pm.portfolio_value(p, {"A": 1, "B": 1})
    assert list(v.round(4)) == [1.0, 1.05, 0.995, 1.105]
    assert round(pm.drawdown(v).min(), 6) == round(0.995 / 1.05 - 1, 6)


def test_metrics_match_hand_computation():
    p = _prices(["A", "B"])
    w = {"A": 3.0, "B": 1.0}
    v = pm.portfolio_value(p, w)
    m = pm.metrics(v, p, w, rf_annual=0.04)
    r = v.pct_change().dropna()
    rf_d = 1.04 ** (1 / 252) - 1
    vol = r.std(ddof=1) * math.sqrt(252)
    assert m["volatility"] == pytest.approx(vol, rel=1e-5)
    assert m["sharpe"] == pytest.approx((r - rf_d).mean() * 252 / vol, rel=1e-5)
    down = math.sqrt((np.minimum(r - rf_d, 0) ** 2).mean()) * math.sqrt(252)
    assert m["sortino"] == pytest.approx((r - rf_d).mean() * 252 / down, rel=1e-5)
    assert m["calmar"] == pytest.approx(m["annualized_return"] / abs(m["max_drawdown"]), rel=1e-4)
    assert m["effective_assets"] == pytest.approx(1 / (0.75 ** 2 + 0.25 ** 2))
    assert m["diversification_ratio"] >= 1.0


def test_single_asset_has_no_diversification():
    p = _prices(["A"])
    m = pm.metrics(pm.portfolio_value(p, {"A": 1}), p, {"A": 1}, 0.0)
    assert m["diversification_ratio"] == pytest.approx(1.0)
    assert m["effective_assets"] == 1.0


def test_horizon_and_holdings():
    p = _prices(["A", "B"])
    v = pm.portfolio_value(p, {"A": 1, "B": 1})
    h = pm.value_at_offset(v, 28)
    assert h["date"] >= (p.index[0] + pd.Timedelta(days=28)).strftime("%Y-%m-%d")
    assert pm.value_at_offset(v, 400) is None
    hold = pm.holdings(p, {"A": 1, "B": 1})
    assert sum(x["contribution"] for x in hold) == pytest.approx(v.iloc[-1] - 1, abs=1e-5)


# ---------------------------------------------------------------- build ----

HC = ["LLY", "UNH", "MDT", "SYK"]
RJ = {"personas": [
    {"persona": "Value Investor", "tickers": ["LLY", "UNH"]},
    {"persona": "Growth Investor", "tickers": ["UNH", "MDT"]},
    {"persona": "Momentum Trader", "tickers": ["UNH"]},
    {"persona": "Contrarian/Skeptic", "tickers": ["SYK"]},
    {"persona": "Macro Strategist", "tickers": []},
    {"persona": "Technical Analyst", "tickers": ["MDT"]},
    {"persona": "ESG/Sustainability Investor", "tickers": ["LLY"]},
    {"persona": "Risk Manager/Quant", "tickers": ["SYK"]},
]}
AS_OF = date(2026, 3, 31)


def _fake_fetch(missing=()):
    def fetch(tickers, start, end):
        cols = [t for t in tickers if t not in missing and t != "^IRX"]
        df = _prices(cols, n=70, start="2026-03-25")
        out = {t: {d.strftime("%Y-%m-%d"): float(v) for d, v in df[t].items()} for t in cols}
        if "^IRX" not in missing:
            out["^IRX"] = {d.strftime("%Y-%m-%d"): 4.0 for d in df.index}
        return out
    return fetch


@pytest.fixture
def ready(run_id):  # noqa: F811
    run_store.write_artifact(run_id, "01_universe.json", {"as_of_date": AS_OF.isoformat(), "sectors": ["Health Care"],
                                                          "market_cap_tiers": ["mega"], "ticker_universe": HC, "universe": []})
    run_store.write_artifact(run_id, "06_report.json", RJ)
    for s in ("seed", "prompt", "simulation", "report_json"):
        run_store.update_step(run_id, s, run_store.STEP_COMPLETED)
    step7_consensus.build(run_id, 2)   # UNH 3 votes, LLY/MDT/SYK 2 -> tie at 2 -> 4 tickers
    return run_id


def test_build_portfolios_weights_and_entry(ready):
    s8.build(ready, fetch=_fake_fetch())
    r = run_store.read_artifact(ready, s8.ARTIFACT)
    assert r["base_date"] > AS_OF.isoformat()                       # entry strictly after as_of
    keys = [p["key"] for p in r["portfolios"]]
    assert keys[0] == "consensus" and "SPY" in keys and "XLV" in keys and keys[-1] == "universe_ew"
    cons = r["portfolios"][0]
    assert cons["weights"] == {"UNH": pytest.approx(3 / 9), "LLY": pytest.approx(2 / 9),
                               "MDT": pytest.approx(2 / 9), "SYK": pytest.approx(2 / 9)}
    assert "persona:macrostrategist" not in keys                      # empty persona skipped
    assert "Macro Strategist: no priced tickers — skipped" in r["warnings"]
    assert len(cons["series"]) == r["trading_days"] == len(r["dates"])
    assert cons["series"][0] == 0.0
    assert r["risk_free_rate"] == pytest.approx(0.04)
    assert r["correlation"]["tickers"][:4] == ["UNH", "LLY", "MDT", "SYK"]
    assert run_store.load_run(ready)["status"] == "completed"


def test_build_drops_unpriced_tickers_and_flags_stale(ready):
    s8.build(ready, fetch=_fake_fetch(missing=("SYK", "^IRX")))
    r = run_store.read_artifact(ready, s8.ARTIFACT)
    assert "SYK" not in r["portfolios"][0]["weights"]
    assert any("SYK" in w for w in r["warnings"]) and any("^IRX" in w for w in r["warnings"])
    assert s8.get(ready)["stale"] is False
    step7_consensus.build(ready, 1)
    assert s8.get(ready)["stale"] is True


def test_build_needs_prices_after_as_of(ready):
    def only_before(tickers, start, end):
        return {t: {"2026-03-30": 100.0, "2026-03-31": 101.0} for t in tickers}
    with pytest.raises(ValueError, match="Not enough trading days"):
        s8.build(ready, fetch=only_before)
    assert run_store.load_run(ready)["steps"]["performance"]["status"] == "failed"


def test_api_requires_consensus(run_id):  # noqa: F811
    r = create_app().test_client().post(f"/api/pipeline/runs/{run_id}/performance")
    assert r.status_code == 400 and "05" in r.json["error"]
