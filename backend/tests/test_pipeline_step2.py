"""
News pipeline step 2 — windowed Finnhub fetch, compaction, resumable job.
Offline: Finnhub HTTP and get_company_news are stubbed.
"""

import time
from datetime import date, datetime, timedelta, timezone

import pytest

from app import create_app
from app.data_layer import news_data
from app.news_pipeline import news_compactor as nc
from app.news_pipeline import run_store, step2_news


AS_OF = (date.today() - timedelta(days=60)).isoformat()


def _epoch(day: date, hour=12):
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc).timestamp())


# ------------------------------------------------------------ fetcher ----

def test_fetch_splits_capped_windows_and_guards_leakage(monkeypatch):
    as_of = date.fromisoformat(AS_OF)
    requested = []

    def fake_request(ticker, start, end):
        requested.append((start, end))
        span = (end - start).days + 1
        # a "busy" ticker: 60 articles/day -> any window > 3 days hits the cap
        per_day = 60
        items = []
        for i in range(span):
            d = start + timedelta(days=i)
            items += [{"id": d.toordinal() * 100 + k, "headline": f"h{k}", "url": "u",
                       "datetime": _epoch(d), "summary": "s"} for k in range(per_day)]
        items.append({"id": 1, "headline": "future", "url": "u", "datetime": _epoch(as_of + timedelta(days=3))})
        return items[:news_data.SPLIT_THRESHOLD + 45]  # Finnhub-style cap

    monkeypatch.setattr(news_data, "_request_window", fake_request)
    out = news_data.fetch_company_news("aapl", AS_OF)

    days = {a["published_at"][:10] for a in out["articles"]}
    assert len(days) == 91                       # every day of the window covered
    assert max(days) <= AS_OF                    # leakage guard dropped the future item
    assert len(requested) > 13 and min((e - s).days + 1 for s, e in requested) <= 3  # 7-day windows were split
    assert out["truncated_days"] == []
    assert out["calls"] == len(requested)


def test_fetch_stops_between_calls(monkeypatch):
    monkeypatch.setattr(news_data, "_request_window", lambda *a: [])
    with pytest.raises(news_data.NewsFetchStopped):
        news_data.fetch_company_news("XOM", AS_OF, should_stop=lambda: True)


# ---------------------------------------------------------- compactor ----

def test_name_variants():
    assert nc.name_variants("ExxonMobil")[:3] == ["ExxonMobil", "Exxon Mobil", "Exxon"]
    assert nc.name_variants("Eli Lilly and Company") == ["Eli Lilly", "Lilly"]
    assert nc.name_variants("NextEra Energy") == ["NextEra Energy", "NextEra"]
    assert "Home" not in nc.name_variants("The Home Depot")


def _raw(ticker, articles):
    start = date.fromisoformat(AS_OF) - timedelta(days=90)
    return {"ticker": ticker, "window_start": start.isoformat(), "window_end": AS_OF, "articles": articles}


def _art(i, headline, day_offset, summary=""):
    d = date.fromisoformat(AS_OF) - timedelta(days=day_offset)
    return {"article_id": i, "headline": headline, "summary": summary, "publisher": "X",
            "url": f"u{i}", "published_at": f"{d.isoformat()}T10:00:00Z"}


def test_select_decisions_and_week_coverage():
    arts = [
        _art(1, "Exxon beats Q1 earnings estimates", 5),
        _art(2, "Oil prices slide as OPEC meets", 6),                   # not about XOM
        _art(3, "Vanguard Group LLC raises its stake in XOM shares", 7),  # 13F noise
        _art(4, "Exxon beats Q1 earnings estimates", 8),                # duplicate
        _art(5, "XOM faces lawsuit over climate disclosures", 80),       # old week, must survive
        _art(6, "Exxon announces dividend increase", 9),
    ]
    decided = {r["article_id"]: r for r in nc.select_for_ticker(_raw("XOM", arts), "ExxonMobil", cap=2)}
    assert decided[2]["decision"] == nc.DROP_IRRELEVANT
    assert decided[3]["decision"] == nc.DROP_NOISE
    assert decided[4]["decision"] == nc.DROP_DUPLICATE
    assert decided[5]["decision"] == nc.KEPT        # week coverage beats raw score
    assert decided[1]["decision"] == nc.KEPT
    assert decided[6]["decision"] == nc.DROP_CAP
    assert "EARNINGS" in decided[1]["tags"] and "LEGAL" in decided[5]["tags"]


def test_ambiguous_ticker_needs_explicit_form():
    arts = [_art(1, "Now is the time to buy stocks", 3), _art(2, "ServiceNow (NOW) raises guidance", 4)]
    decided = {r["article_id"]: r for r in nc.select_for_ticker(_raw("NOW", arts), "ServiceNow", cap=5)}
    assert decided[1]["decision"] == nc.DROP_IRRELEVANT
    assert decided[2]["decision"] == nc.KEPT


def test_render_merges_shared_story():
    universe = {"ticker_universe": ["XOM", "CVX"], "sectors": ["Energy"], "market_cap_tiers": ["mega"],
                "as_of_date": AS_OF,
                "universe": [{"ticker": "XOM", "company_name": "ExxonMobil", "gics_sector": "Energy", "market_cap_tier": "mega"},
                             {"ticker": "CVX", "company_name": "Chevron Corporation", "gics_sector": "Energy", "market_cap_tier": "mega"}]}
    shared = "Chevron vs ExxonMobil: which dividend is safer"
    sel = {
        "XOM": nc.select_for_ticker(_raw("XOM", [_art(1, shared, 3)]), "ExxonMobil", 5),
        "CVX": nc.select_for_ticker(_raw("CVX", [_art(2, shared, 3), _art(3, "Chevron closes Hess deal", 4)]), "Chevron Corporation", 5),
    }
    text = nc.render_txt(universe, sel, 5)
    assert text.count(shared) == 1
    assert f"| XOM,CVX | {shared}" in text
    assert "=== CVX — Chevron Corporation (Energy, mega cap) ===" in text


# ---------------------------------------------------------------- job ----

UNIVERSE = {
    "as_of_date": AS_OF, "sectors": ["Energy"], "market_cap_tiers": ["mega"],
    "ticker_universe": ["XOM", "CVX"],
    "universe": [
        {"ticker": "XOM", "company_name": "ExxonMobil", "gics_sector": "Energy", "market_cap": 5e11, "market_cap_tier": "mega"},
        {"ticker": "CVX", "company_name": "Chevron Corporation", "gics_sector": "Energy", "market_cap": 3e11, "market_cap_tier": "mega"},
    ],
}


@pytest.fixture
def run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "PIPELINE_RUNS_DIR", str(tmp_path / "runs"))
    manifest = run_store.create_run("t", {"as_of_date": AS_OF})
    run_store.write_artifact(manifest["run_id"], "01_universe.json", UNIVERSE)
    run_store.update_step(manifest["run_id"], "universe", run_store.STEP_COMPLETED)
    return manifest["run_id"]


def _fake_news(fetched):
    def fake(ticker, as_of_date, on_call=None, should_stop=None):
        if should_stop and should_stop():
            raise news_data.NewsFetchStopped(ticker)
        fetched.append(ticker)
        if on_call:
            on_call({"start": "a", "end": "b", "count": 1, "split": False})
        name = {"XOM": "Exxon", "CVX": "Chevron"}[ticker]
        return {**_raw(ticker, [_art(hash(ticker) % 1000, f"{name} beats earnings", 3)]),
                "calls": 1, "truncated_days": [], "from_cache": False}
    return fake


def _wait(rid, done=("completed", "failed", "paused")):
    for _ in range(200):
        st = step2_news.status(rid)
        if st["status"] in done:
            return st
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_job_fetches_compacts_and_completes(run_id, monkeypatch):
    fetched = []
    monkeypatch.setattr(step2_news, "get_company_news", _fake_news(fetched))
    client = create_app().test_client()
    assert client.post(f"/api/pipeline/runs/{run_id}/news/start").status_code == 200
    st = _wait(run_id)
    assert st["status"] == "completed" and fetched == ["XOM", "CVX"]
    assert run_store.load_run(run_id)["current_step"] == 3
    txt = client.get(f"/api/pipeline/runs/{run_id}/news/txt").json["data"]
    assert "Exxon beats earnings" in txt["text"] and "Chevron beats earnings" in txt["text"]
    arts = client.get(f"/api/pipeline/runs/{run_id}/news/articles?ticker=XOM&view=kept").json["data"]
    assert [a["decision"] for a in arts] == ["kept"]

    # rebuild with a different cap, no refetch
    r = client.post(f"/api/pipeline/runs/{run_id}/news/compact", json={"cap": 5}).json["data"]
    assert r["cap"] == 5 and fetched == ["XOM", "CVX"]
    assert run_store.load_run(run_id)["steps"]["news"]["summary"]["cap"] == 5


def test_job_resume_skips_fetched_tickers(run_id, monkeypatch):
    fetched = []
    fake = _fake_news(fetched)
    run_store.write_artifact(run_id, step2_news._raw_name("XOM"), fake("XOM", AS_OF))
    fetched.clear()
    monkeypatch.setattr(step2_news, "get_company_news", fake)
    step2_news.start(run_id)
    assert _wait(run_id)["status"] == "completed"
    assert fetched == ["CVX"]
    events = [l["event"] for l in run_store.read_log(run_id)]
    assert "news_resume" in events and events[-1] == "step_completed"


def test_pause_then_interrupted_state(run_id, monkeypatch):
    monkeypatch.setattr(step2_news, "get_company_news", _fake_news([]))
    job = step2_news._Job(run_id, 2)
    job.stop.set()
    run_store.update_step(run_id, "news", run_store.STEP_RUNNING)
    step2_news._worker(job, UNIVERSE)
    assert run_store.load_run(run_id)["steps"]["news"]["status"] == "paused"

    # manifest says running but no live job (backend restart) -> interrupted
    run_store.update_step(run_id, "news", run_store.STEP_RUNNING)
    assert step2_news.status(run_id)["status"] == "interrupted"
