"""
Step 1 fundamental filter — ratio engine, industry benchmark, ranking, API.
Offline: statements / prices are synthetic, _run_screen is stubbed.
"""

import time
from datetime import date, timedelta

import pytest

from app import create_app
from app.news_pipeline import fundamentals as F
from app.news_pipeline import run_store, step1_universe

AS_OF = "2026-09-30"
Q = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]   # all published by AS_OF - 45d
FY = ["2025-12-31", "2024-12-31"]


def _q(values):
    return dict(zip(Q, values))


def _statements(rev_q=100.0, ni_q=10.0, eps_q=1.0, equity=400.0, debt=100.0, cash=50.0, shares=40.0,
                prev_rev=80.0, prev_eps=0.5, fy_rev=380.0, fy_prev_rev=300.0, fy_eps=3.6, fy_prev_eps=3.0):
    return {
        "quarterly_income": {
            "Total Revenue": _q([rev_q] * 4 + [prev_rev]),
            "Gross Profit": _q([rev_q * 0.6] * 5),
            "Operating Income": _q([rev_q * 0.2] * 5),
            "EBIT": _q([rev_q * 0.2] * 5),
            "EBITDA": _q([rev_q * 0.25] * 5),
            "Net Income Common Stockholders": _q([ni_q] * 5),
            "Diluted EPS": _q([eps_q] * 4 + [prev_eps]),
            "Interest Expense": _q([2.0] * 5),
            "Tax Rate For Calcs": _q([0.2] * 5),
        },
        "quarterly_balance": {
            "Stockholders Equity": _q([equity] * 5), "Total Debt": _q([debt] * 5),
            "Cash Cash Equivalents And Short Term Investments": _q([cash] * 5),
            "Total Assets": _q([1000.0] * 5), "Current Assets": _q([300.0] * 5),
            "Current Liabilities": _q([200.0] * 5), "Invested Capital": _q([500.0] * 5),
            "Ordinary Shares Number": _q([shares] * 5), "Tangible Book Value": _q([300.0] * 5),
        },
        "quarterly_cashflow": {
            "Free Cash Flow": _q([8.0] * 5), "Cash Dividends Paid": _q([-2.0] * 5),
            "Depreciation And Amortization": _q([5.0] * 5),
        },
        "annual_income": {
            "Total Revenue": dict(zip(FY, [fy_rev, fy_prev_rev])),
            "Net Income Common Stockholders": dict(zip(FY, [36.0, 30.0])),
            "Diluted EPS": dict(zip(FY, [fy_eps, fy_prev_eps])),
            "EBITDA": dict(zip(FY, [90.0, 80.0])),
        },
        "annual_balance": {
            "Stockholders Equity": dict(zip(FY, [380.0, 350.0])),
            "Total Debt": dict(zip(FY, [90.0, 90.0])),
            "Ordinary Shares Number": dict(zip(FY, [40.0, 40.0])),
        },
        "annual_cashflow": {"Free Cash Flow": dict(zip(FY, [30.0, 25.0]))},
    }


# ------------------------------------------------------------ ratio engine --

def test_ttm_ratios_and_quarter_yoy_growth():
    r = F.compute_ticker(_statements(), price=50.0, as_of=AS_OF)
    v = r["ttm"]["values"]
    assert r["periods"]["ttm"]["quarters"] == Q[:4]
    assert r["market_cap"] == 2000.0
    assert v["pe"] == pytest.approx(50 / 4)                  # 4 quarters of EPS 1.0
    assert v["pb"] == pytest.approx(2000 / 400)
    assert v["ps"] == pytest.approx(2000 / 400)
    assert v["ev_ebitda"] == pytest.approx((2000 + 100 - 50) / 100)
    assert v["div_yield"] == pytest.approx(8 / 2000)
    assert v["roe"] == pytest.approx(40 / 400)
    assert v["roic"] == pytest.approx(80 * 0.8 / 500)
    assert v["current_ratio"] == pytest.approx(1.5)
    # TTM growth = latest quarter vs the same quarter one year earlier
    assert r["periods"]["ttm"]["growth_vs"] == "2025-06-30"
    assert v["rev_growth"] == pytest.approx(100 / 80 - 1)
    assert v["eps_growth"] == pytest.approx(1.0)
    assert v["peg"] == pytest.approx(12.5 / 100)


def test_fy_basis_uses_fiscal_year_and_its_growth():
    r = F.compute_ticker(_statements(), price=50.0, as_of=AS_OF)
    v = r["fy"]["values"]
    assert r["periods"]["fy"] == {"year_end": "2025-12-31", "growth_vs": "2024-12-31"}
    assert v["pe"] == pytest.approx(50 / 3.6)
    assert v["rev_growth"] == pytest.approx(380 / 300 - 1)
    assert v["eps_growth"] == pytest.approx(0.2)


def test_periods_published_after_as_of_are_not_used():
    # as_of 2026-07-15: the 2026-06-30 quarter is not published yet (45-day lag)
    r = F.compute_ticker(_statements(), price=50.0, as_of="2026-07-15")
    assert r["periods"]["ttm"]["quarters"] == Q[1:5]
    # as_of 2026-03-01: only 3 quarters before the cutoff -> no TTM, FY still there
    r = F.compute_ticker(_statements(), price=50.0, as_of="2026-03-01")
    assert r["ttm"] is None and "TTM unavailable" in r["notes"][0]
    assert r["fy"]["values"]["pe"] is not None
    # FY 2025 is only usable 75 days after year end
    r = F.compute_ticker(_statements(), price=50.0, as_of="2026-02-15")
    assert r["periods"]["fy"]["year_end"] == "2024-12-31"


def test_not_meaningful_values_are_flagged_not_returned():
    r = F.compute_ticker(_statements(eps_q=-1.0, equity=-10.0, prev_eps=-0.5), price=50.0, as_of=AS_OF)
    v, flags = r["ttm"]["values"], r["ttm"]["flags"]
    for m in ("pe", "pb", "roe", "de", "eps_growth", "peg"):
        assert v[m] is None and flags[m] == "nm", m


# --------------------------------------------------------------- benchmark --

def test_benchmark_stats_and_outliers():
    pairs = [(f"T{i}", float(i)) for i in range(1, 10)] + [("BIG", 500.0)]
    s = F.benchmark_stats(pairs, nm_count=2)
    assert s["outliers"] == ["BIG"]
    assert (s["n"], s["nm"], s["min"], s["max"], s["median"], s["mean"]) == (9, 2, 1.0, 9.0, 5.0, 5.0)
    # fewer than 8 peers: nothing is treated as an outlier
    s = F.benchmark_stats([("A", 1.0), ("B", 2.0), ("C", 100.0)])
    assert s["outliers"] == [] and s["max"] == 100.0 and s["median"] == 2.0


def test_rank_is_direction_adjusted():
    s = F.benchmark_stats([("A", 10.0), ("B", 20.0), ("C", 30.0)])
    low = F.rank_in_group(10.0, "A", s, "lower")       # cheapest P/E = best
    assert low == {"pct": 100.0, "side": "better", "extreme": "min", "outlier": False}
    high = F.rank_in_group(10.0, "A", s, "higher")     # lowest ROE = worst
    assert high["pct"] == 0.0 and high["side"] == "worse"
    assert F.rank_in_group(20.0, "B", s, "higher")["side"] == "equal"
    assert F.rank_in_group(None, "A", s, "higher") is None


def _entries():
    return [
        {"ticker": "AAA", "company_name": "A", "gics_sector": "Industrials", "gics_sub_industry": "Machinery",
         "market_cap_tier": "large"},
        {"ticker": "BBB", "company_name": "B", "gics_sector": "Industrials", "gics_sub_industry": "Machinery",
         "market_cap_tier": "mid"},
        {"ticker": "CCC", "company_name": "C", "gics_sector": "Industrials", "gics_sub_industry": "Airlines",
         "market_cap_tier": "large"},
    ]


def test_build_benchmarks_groups_and_evaluate():
    per = {
        "AAA": F.compute_ticker(_statements(eps_q=2.0, rev_q=120.0, equity=800.0), 50.0, AS_OF),   # cheaper, faster
        "BBB": F.compute_ticker(_statements(), 50.0, AS_OF),
        "CCC": F.compute_ticker(_statements(), 50.0, AS_OF),
    }
    bench, ranks = F.build_benchmarks(_entries(), per)
    mach = bench["ttm"]["sub_industry"]["Machinery"]
    assert mach["peers"] == 2 and mach["small"] is True
    assert mach["metrics"]["pe"]["n"] == 2
    assert set(bench["ttm"]["sector"]) == {"Industrials"}
    assert ranks["ttm"]["sub_industry"]["AAA"]["pe"]["side"] == "better"
    result = {"tickers": {e["ticker"]: {"sector": e["gics_sector"]} for e in _entries()}, "ranks": ranks}
    ev = F.evaluate(result, ["AAA", "BBB"], "value", "ttm", "sub_industry", 0.5)
    assert ev["AAA"]["passes"] and not ev["BBB"]["passes"]
    assert ev["AAA"]["n"] == len(F.SECTOR_PROFILES["Industrials"]["value"])
    # switching metrics off changes the denominator
    ev = F.evaluate(result, ["AAA"], "value", "ttm", "sub_industry", 1.0,
                    disabled={"Industrials": [m for m in F.SECTOR_PROFILES["Industrials"]["value"] if m != "pe"]})
    assert ev["AAA"]["metrics"] == ["pe"] and ev["AAA"]["passes"]


def test_every_profile_metric_exists():
    for sector, p in F.SECTOR_PROFILES.items():
        for lens in ("value", "growth"):
            assert p[lens] and all(m in F.METRICS for m in p[lens]), sector
    assert set(F.SECTOR_PROFILES) == set(step1_universe.GICS_SECTORS)


# --------------------------------------------------------------------- API --

_ROWS = [dict(e, market_cap=m) for e, m in zip(_entries(), (50e9, 5e9, 30e9))]


def _fake_run_screen(sectors, tiers, as_of_date, progress_callback=None):
    passed = [r for r in _ROWS if r["market_cap_tier"] in tiers]
    for i, r in enumerate(_ROWS, 1):
        progress_callback({"processed": i, "in_scope": 3, "ticker": r["ticker"],
                           "outcome": "passed" if r in passed else "filtered", "entry": r, "reason": None})
    return {"passed": passed, "skipped": [], "total": 503}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "PIPELINE_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(step1_universe, "_run_screen", _fake_run_screen)
    stmts = {"AAA": _statements(eps_q=2.0, rev_q=120.0, equity=800.0), "BBB": _statements(), "CCC": _statements()}
    monkeypatch.setattr(F, "get_statements",
                        lambda t: {"success": True, "statements": stmts[t]} if t in stmts
                        else {"success": False, "error": "none"})
    monkeypatch.setattr(F, "get_price_data",
                        lambda t, as_of_date=None: {"success": True, "latest_close": 50.0,
                                                    "latest_date": as_of_date})
    return create_app().test_client()


def _wait(client, url):
    for _ in range(200):
        task = client.get(url).json["data"]
        if task["status"] in ("completed", "failed"):
            return task
        time.sleep(0.02)
    raise AssertionError("task never finished")


def test_api_fundamentals_and_lock_records_them(client):
    as_of = (date.today() - timedelta(days=60)).isoformat()
    r = client.post('/api/pipeline/universe/screen',
                    json={"sectors": ["Industrials"], "market_cap_tiers": ["large", "mid"], "as_of_date": as_of})
    screen_id = r.json["data"]["task_id"]
    assert _wait(client, f'/api/pipeline/universe/screen/{screen_id}')["status"] == "completed"

    r = client.post('/api/pipeline/universe/fundamentals', json={"screen_task_id": screen_id})
    assert r.status_code == 202
    fund_id = r.json["data"]["task_id"]
    # a second request for the same screen joins the same task
    assert client.post('/api/pipeline/universe/fundamentals',
                       json={"screen_task_id": screen_id}).json["data"]["task_id"] == fund_id
    task = _wait(client, f'/api/pipeline/universe/fundamentals/{fund_id}')
    assert task["status"] == "completed", task.get("error")
    res = task["result"]
    # the benchmark covers every screened company, including the mid-cap outside the chosen tier
    assert set(res["tickers"]) == {"AAA", "BBB", "CCC"}
    assert res["benchmarks"]["ttm"]["sub_industry"]["Machinery"]["peers"] == 2

    r = client.post('/api/pipeline/runs', json={
        "task_id": screen_id, "name": "fund", "market_cap_tiers": ["large", "mid"],
        "excluded_tickers": ["BBB"],
        "fundamentals": {"task_id": fund_id, "lens": "value", "basis": "ttm", "level": "sub_industry",
                         "threshold": 0.5}})
    assert r.status_code == 201, r.json
    run_id = r.json["data"]["run_id"]
    art = run_store.read_artifact(run_id, step1_universe.ARTIFACT)
    assert art["ticker_universe"] == ["AAA", "CCC"]
    fund = art["fundamentals"]
    assert fund["lens"] == "value" and "AAA" in fund["passing"]
    assert set(fund["evaluation"]) == {"AAA", "CCC"}
    assert "Machinery" in fund["group_stats"]
    assert r.json["data"]["config"]["fundamentals"]["lens"] == "value"


def test_api_fundamentals_unknown_screen(client):
    r = client.post('/api/pipeline/universe/fundamentals', json={"screen_task_id": "nope"})
    assert r.status_code == 404
    assert client.get('/api/pipeline/universe/fundamentals/nope').status_code == 404


def test_nearly_empty_period_column_is_ignored():
    st = _statements()
    st["quarterly_income"]["Diluted EPS"]["2026-09-30"] = 1.0   # yfinance stub column, 1 row only
    r = F.compute_ticker(st, 50.0, "2026-12-31")
    assert r["periods"]["ttm"]["quarters"] == Q[:4]
    assert r["ttm"]["values"]["ps"] is not None


def test_score_counts_nm_as_zero_and_needs_half_coverage():
    ranks = {"ttm": {"sub_industry": {
        "AAA": {"pe": {"pct": 100.0, "side": "better"}},
        "BBB": {"pe": {"pct": 80.0, "side": "better"}, "ev_ebitda": {"pct": 60.0, "side": "better"},
                "fcf_yield": {"pct": 40.0, "side": "worse"}}}}}
    tickers = {"AAA": {"sector": "Industrials", "ttm": {"flags": {}}},
               "BBB": {"sector": "Industrials", "ttm": {"flags": {"pb": "nm"}}}}
    ev = F.evaluate({"tickers": tickers, "ranks": ranks}, ["AAA", "BBB"], "value", "ttm", "sub_industry", 0.5)
    assert ev["AAA"]["score"] is None                       # 1 of 7 ratios -> no score
    assert ev["BBB"]["coverage"] == 4 and ev["BBB"]["score"] == pytest.approx((80 + 60 + 40 + 0) / 4)
