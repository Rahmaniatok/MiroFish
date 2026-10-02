"""
Sharia (AAOIFI SS-21) filter — data-layer screens, verdict summary, step-1
toggle and step-8 Sharia benchmarks. Offline (yfinance .info stubbed).
"""

import time
from datetime import date, timedelta

import pandas as pd
import pytest

from app import create_app
from app.data_layer import market_data, universe as uni
from app.news_pipeline import run_store, step1_universe, step7_consensus, step8_performance as s8


def _info(**kw):
    base = {"sector": "Technology", "industry": "Software - Infrastructure",
            "marketCap": 1000, "totalDebt": 100, "totalCash": 100, "sharesOutstanding": 100}
    base.update(kw)
    return base


class _FakeTicker:
    """yfinance.Ticker stand-in: .info, a one-quarter balance sheet, 24 monthly closes of 10."""

    def __init__(self, info, bs=None):
        self.info = info
        bs = bs or {"Total Debt": 100.0, "Cash Cash Equivalents And Short Term Investments": 100.0,
                    "Accounts Receivable": 100.0}
        self.quarterly_balance_sheet = pd.DataFrame({pd.Timestamp("2026-03-31"): pd.Series(bs)})

    def history(self, **kw):
        return pd.DataFrame({"Close": [10.0] * 24})


@pytest.fixture
def fake_yf(monkeypatch):
    infos = {}   # ticker -> info, or (info, balance-sheet dict)
    def make(t):
        v = infos[t]
        return _FakeTicker(*v) if isinstance(v, tuple) else _FakeTicker(v)
    monkeypatch.setattr(market_data.yf, "Ticker", make)
    return infos


def test_thresholds_are_aaoifi_30_percent(fake_yf):
    fake_yf.update({
        "OK": _info(totalDebt=299, totalCash=299),
        "DEBT": _info(totalDebt=300),           # 30.0% is NOT < 30%
        "CASH": _info(totalCash=310),
        "BANK": _info(sector="Financial Services", industry="Banks - Diversified"),
        "NOCASH": _info(totalCash=None),
    })
    got = {t: market_data.fetch_sharia_compliance_data(t, "2026-06-29") for t in fake_yf}
    assert got["OK"]["overall_compliant"] is True
    assert got["DEBT"]["debt_to_market_cap_threshold"] == 0.30
    assert got["DEBT"]["passes_debt_screen"] is False and got["DEBT"]["overall_compliant"] is False
    assert got["CASH"]["passes_cash_screen"] is False and got["CASH"]["overall_compliant"] is False
    assert got["CASH"]["cash_screen_is_approximation"] is True
    assert got["BANK"]["excluded_category"] == "conventional_finance"
    assert got["BANK"]["overall_compliant"] is False
    assert got["NOCASH"]["overall_compliant"] is None
    assert "cash_to_market_cap" in got["NOCASH"]["unavailable_checks"]
    assert "interest_bearing_investments_ratio" not in got["OK"]["unavailable_checks"]


def test_failed_screen_beats_missing_ratio(fake_yf):
    fake_yf["X"] = _info(totalDebt=500, totalCash=None)
    assert market_data.fetch_sharia_compliance_data("X")["overall_compliant"] is False


def test_sharia_summary_reasons(fake_yf):
    fake_yf.update({"A": _info(totalDebt=330, totalCash=10), "B": _info(totalCash=None)})
    a = uni.sharia_summary({"sharia_compliance": market_data.fetch_sharia_compliance_data("A")})["aaoifi"]
    assert a["verdict"] == "non_compliant" and a["reasons"] == ["debt/market cap 33.0% ≥ 30%"]
    b = uni.sharia_summary({"sharia_compliance": market_data.fetch_sharia_compliance_data("B")})["aaoifi"]
    assert b["verdict"] == "unknown" and "cash/market cap not available" in b["reasons"]
    assert uni.sharia_summary({})["djim"]["verdict"] == "unknown"


# avg mcap in the fake = 100 shares x 10 = 1000, so a balance-sheet value of 330 = 33%
def test_djim_ratios_use_24m_average_and_33_percent(fake_yf):
    fake_yf.update({
        "OK": (_info(), {"Total Debt": 320.0, "Cash Cash Equivalents And Short Term Investments": 320.0,
                         "Accounts Receivable": 320.0}),
        "AR": (_info(), {"Total Debt": 10.0, "Cash Cash Equivalents And Short Term Investments": 10.0,
                         "Accounts Receivable": 330.0}),
    })
    ok = market_data.fetch_sharia_compliance_data("OK", "2026-06-29")["djim"]
    assert ok["avg_market_cap_24m"] == 1000 and ok["debt_to_avg_mcap"] == 0.32
    assert ok["balance_sheet_date"] == "2026-03-31" and ok["overall_compliant"] is True
    ar = market_data.fetch_sharia_compliance_data("AR", "2026-06-29")["djim"]
    assert ar["passes_receivables_screen"] is False and ar["overall_compliant"] is False


def test_djim_balance_sheet_must_not_postdate_as_of(fake_yf):
    fake_yf["X"] = _info()
    d = market_data.fetch_sharia_compliance_data("X", "2026-01-15")["djim"]   # only a 2026-03-31 column exists
    assert d["balance_sheet_date"] is None and d["overall_compliant"] is None


def test_djim_business_exclusions_differ_from_aaoifi(fake_yf):
    fake_yf.update({"NFLX": _info(industry="Entertainment"), "MAR": _info(industry="Lodging"), "HRL": _info(industry="Packaged Foods")})
    for t, cat in (("NFLX", "entertainment"), ("MAR", "hotels"), ("HRL", "pork")):
        d = market_data.fetch_sharia_compliance_data(t)
        assert d["overall_compliant"] is True                      # AAOIFI list does not cover these
        assert d["djim"]["excluded_category"] == cat and d["djim"]["overall_compliant"] is False


def test_intersection_and_single_standard():
    summ = {"aaoifi": {"verdict": "non_compliant", "reasons": ["debt/market cap 31.5% ≥ 30%"]},
            "djim": {"verdict": "compliant", "reasons": []}}
    assert uni.sharia_combined(summ, ["djim"])["verdict"] == "compliant"
    both = uni.sharia_combined(summ, ["aaoifi", "djim"])
    assert both["verdict"] == "non_compliant" and both["reasons"] == ["AAOIFI: debt/market cap 31.5% ≥ 30%"]
    assert both["by_standard"] == {"aaoifi": "non_compliant", "djim": "compliant"}
    unk = {"aaoifi": {"verdict": "compliant", "reasons": []}, "djim": {"verdict": "unknown", "reasons": ["x"]}}
    assert uni.sharia_combined(unk, ["aaoifi", "djim"])["verdict"] == "unknown"


# ------------------------------------------------------------ step 1 ----

def _row(t, cap, tier, aaoifi, djim, reasons=()):
    std = lambda v: {"verdict": v, "reasons": [] if v == "compliant" else list(reasons), "category": None}  # noqa: E731
    return {"ticker": t, "company_name": t, "gics_sector": "Information Technology", "market_cap": cap,
            "market_cap_tier": tier, "sharia": {"aaoifi": std(aaoifi), "djim": std(djim)}}


ROWS = [_row("NVDA", 4e12, "mega", "compliant", "compliant"),
        _row("IBM", 2.5e11, "mega", "non_compliant", "compliant", ["debt/market cap 31.0% ≥ 30%"]),
        _row("NFLX", 4e11, "mega", "compliant", "non_compliant", ["entertainment"]),
        _row("QCOM", 1.8e11, "large", "compliant", "compliant"),
        _row("FISV", 5e10, "large", "unknown", "unknown", ["debt/market cap not available"])]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "PIPELINE_RUNS_DIR", str(tmp_path / "runs"))

    def fake_screen(sectors, tiers, as_of, progress_callback=None):
        for i, r in enumerate(ROWS, 1):
            progress_callback({"processed": i, "in_scope": 5, "ticker": r["ticker"], "outcome": "passed",
                               "entry": r, "reason": None})
        return {"passed": list(ROWS), "skipped": [], "total": 503}
    monkeypatch.setattr(step1_universe, "_run_screen", fake_screen)
    return create_app().test_client()


def _screen(client):
    as_of = (date.today() - timedelta(days=60)).isoformat()
    tid = client.post('/api/pipeline/universe/screen', json={
        "sectors": ["Information Technology"], "market_cap_tiers": ["mega", "large"], "as_of_date": as_of}).json["data"]["task_id"]
    for _ in range(100):
        if client.get(f'/api/pipeline/universe/screen/{tid}').json["data"]["status"] == "completed":
            return tid
        time.sleep(0.02)


def test_options_expose_both_standards(client):
    opts = client.get('/api/pipeline/universe/options').json["data"]["sharia"]["standards"]
    assert any("< 30%" in r for r in opts["aaoifi"]["rules"])
    assert any("24-month average" in r for r in opts["djim"]["rules"])


def _lock(client, **body):
    rid = client.post('/api/pipeline/runs', json={"task_id": _screen(client), **body}).json["data"]["run_id"]
    return rid, run_store.read_artifact(rid, "01_universe.json")


def test_lock_aaoifi_only(client):
    rid, u = _lock(client, sharia_standards=["aaoifi"])
    assert u["ticker_universe"] == ["NVDA", "NFLX", "QCOM"]
    assert {x["ticker"]: x["verdict"] for x in u["sharia"]["excluded"]} == {"IBM": "non_compliant", "FISV": "unknown"}
    assert u["sharia"]["excluded"][0]["reasons"] == ["AAOIFI: debt/market cap 31.0% ≥ 30%"]
    run = run_store.load_run(rid)
    assert run["config"]["sharia_standards"] == ["aaoifi"] and run["config"]["sharia_filter"] is True
    assert "Sharia filter (AAOIFI) removed 2" in run_store.read_log(rid)[-2]["message"]


def test_lock_djim_only(client):
    _, u = _lock(client, sharia_standards=["djim"])
    assert u["ticker_universe"] == ["NVDA", "IBM", "QCOM"]


def test_lock_intersection(client):
    rid, u = _lock(client, sharia_standards=["djim", "aaoifi"])
    assert u["ticker_universe"] == ["NVDA", "QCOM"]
    assert u["sharia"]["standards"] == ["aaoifi", "djim"] and u["sharia"]["combine"] == "intersection"
    assert "∩" in u["sharia"]["standard"]
    assert run_store.load_run(rid)["steps"]["universe"]["summary"]["sharia_excluded_count"] == 3


def test_lock_legacy_flag_means_aaoifi(client):
    _, u = _lock(client, sharia_filter=True)
    assert u["sharia"]["standards"] == ["aaoifi"]


def test_lock_without_toggle_keeps_everything(client):
    _, u = _lock(client)
    assert u["ticker_universe"] == ["NVDA", "NFLX", "IBM", "QCOM", "FISV"] and u["sharia"] is None


def test_unknown_standard_rejected(client):
    r = client.post('/api/pipeline/runs', json={"task_id": _screen(client), "sharia_standards": ["msci"]})
    assert r.status_code == 400 and "Unknown Sharia standard" in r.json["error"]


# ------------------------------------------------------------ step 8 ----

def test_sharia_runs_get_sharia_benchmarks(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "PIPELINE_RUNS_DIR", str(tmp_path / "runs"))
    from tests.test_pipeline_step8 import RJ, HC, _fake_fetch
    rid = run_store.create_run("s", {"as_of_date": "2026-03-31"})["run_id"]
    base = {"as_of_date": "2026-03-31", "sectors": ["Health Care"], "market_cap_tiers": ["mega"],
            "ticker_universe": HC, "universe": []}
    run_store.write_artifact(rid, "01_universe.json", {**base, "sharia": {"excluded": []}})
    run_store.write_artifact(rid, "06_report.json", RJ)
    for s in ("universe", "news", "seed", "prompt", "simulation", "report_json"):
        run_store.update_step(rid, s, run_store.STEP_COMPLETED)
    step7_consensus.build(rid, 2)
    s8.build(rid, fetch=_fake_fetch())
    keys = [p["key"] for p in run_store.read_artifact(rid, s8.ARTIFACT)["portfolios"]]
    assert "SPUS" in keys and "HLAL" in keys

    run_store.write_artifact(rid, "01_universe.json", {**base, "sharia": None})
    s8.build(rid, fetch=_fake_fetch())
    keys = [p["key"] for p in run_store.read_artifact(rid, s8.ARTIFACT)["portfolios"]]
    assert "SPUS" not in keys
