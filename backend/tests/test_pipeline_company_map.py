"""
Company map — correlation backbone (MST), strong ties, Louvain clusters. Offline (synthetic prices).
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.news_pipeline import company_map as CM
from app.news_pipeline import run_store, step1_universe

SECTORS = {"AAA": ("Energy", "Oil"), "BBB": ("Energy", "Oil"), "CCC": ("Energy", "Oil"),
           "XXX": ("Information Technology", "Software"), "YYY": ("Information Technology", "Software"),
           "ZZZ": ("Information Technology", "Semis")}


def _meta(tickers=SECTORS):
    return {t: {"ticker": t, "company_name": t, "gics_sector": s, "gics_sub_industry": i,
                "market_cap": 1e10, "market_cap_tier": "large"} for t, (s, i) in tickers.items()}


def _returns(n=250, seed=1):
    rng = np.random.default_rng(seed)
    oil, tech = rng.normal(0, 0.01, n), rng.normal(0, 0.01, n)
    cols = {}
    for t in ("AAA", "BBB", "CCC"):
        cols[t] = oil + rng.normal(0, 0.004, n)
    for t in ("XXX", "YYY", "ZZZ"):
        cols[t] = tech + rng.normal(0, 0.004, n)
    return pd.DataFrame(cols)


def test_mst_links_every_company_once_and_clusters_follow_comovement():
    m = CM.compute_map(_returns(), _meta())
    mst = [e for e in m["edges"] if e["mst"]]
    assert len(mst) == 5                                       # spanning tree of 6 nodes
    clusters = {c["id"]: set(c["members"]) for c in m["clusters"]}
    assert sorted(map(sorted, clusters.values())) == [["AAA", "BBB", "CCC"], ["XXX", "YYY", "ZZZ"]]
    oil = next(c for c in m["clusters"] if "AAA" in c["members"])
    assert oil["name"] == "Oil" and oil["sectors"] == ["Energy"] and oil["avg_rho"] > 0.7
    tech = next(c for c in m["clusters"] if "XXX" in c["members"])
    assert tech["name"] == "Software and Semis"
    node = next(n for n in m["nodes"] if n["id"] == "AAA")
    assert node["top_peers"][0]["ticker"] in {"BBB", "CCC"} and node["cluster"] == oil["id"]
    # strong non-tree ties inside each block, none across the two uncorrelated blocks
    assert all(e["rho"] >= CM.STRONG_RHO for e in m["edges"] if not e["mst"])
    cross = [e for e in m["edges"] if not e["mst"] and (e["source"] in "AAA BBB CCC") != (e["target"] in "AAA BBB CCC")]
    assert cross == []


def test_short_history_becomes_an_isolated_node():
    r = _returns()
    r.loc[: len(r) - 50, "ZZZ"] = np.nan                       # only ~50 days
    m = CM.compute_map(r, _meta())
    assert m["no_history"] == ["ZZZ"]
    assert next(n for n in m["nodes"] if n["id"] == "ZZZ")["cluster"] is None
    assert all("ZZZ" not in (e["source"], e["target"]) for e in m["edges"])


def test_build_map_never_uses_prices_after_as_of(monkeypatch):
    days = pd.bdate_range("2025-01-01", "2026-06-30")
    rng = np.random.default_rng(3)
    walk = {t: dict(zip((d.date().isoformat() for d in days), 100 * np.cumprod(1 + rng.normal(0, 0.01, len(days)))))
            for t in SECTORS}
    seen = {}

    def fake(tickers, start, end, adjusted=True):
        seen["end"] = end
        return {t: walk[t] for t in tickers}        # deliberately returns data past `end`
    monkeypatch.setattr(CM, "fetch_close_history", fake)
    m = CM.build_map([dict(v) for v in _meta().values()], "2026-03-31")
    assert seen["end"] == date(2026, 3, 31)
    assert m["window"]["end"] <= "2026-03-31"
    assert len(m["nodes"]) == 6


def test_lock_writes_company_map_and_run_endpoint(tmp_path, monkeypatch):
    from app import create_app
    rows = [{"ticker": t, "company_name": t, "gics_sector": s, "gics_sub_industry": i, "market_cap": 5e10,
             "market_cap_tier": "large"} for t, (s, i) in SECTORS.items()]

    def fake_screen(sectors, tiers, as_of_date, progress_callback=None):
        for k, r in enumerate(rows, 1):
            progress_callback({"processed": k, "in_scope": len(rows), "ticker": r["ticker"], "outcome": "passed",
                               "entry": r, "reason": None})
        return {"passed": rows, "skipped": [], "total": 503}
    monkeypatch.setattr(run_store, "PIPELINE_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(step1_universe, "_run_screen", fake_screen)
    ret = _returns(300)
    idx = pd.bdate_range(end=date.today() - timedelta(days=60), periods=301)
    prices = (1 + ret).cumprod() * 100
    monkeypatch.setattr(CM, "fetch_close_history", lambda tickers, start, end, adjusted=True: {
        t: {d.date().isoformat(): float(v) for d, v in zip(idx[1:], prices[t])} for t in tickers})
    client = create_app().test_client()
    as_of = (date.today() - timedelta(days=60)).isoformat()
    task_id = client.post('/api/pipeline/universe/screen', json={
        "sectors": ["Energy", "Information Technology"], "market_cap_tiers": ["large"], "as_of_date": as_of}).json["data"]["task_id"]
    import time
    for _ in range(100):
        if client.get(f'/api/pipeline/universe/screen/{task_id}').json["data"]["status"] == "completed":
            break
        time.sleep(0.02)
    prev = client.post('/api/pipeline/universe/map', json={"task_id": task_id, "tickers": ["AAA", "BBB", "XXX"]})
    assert prev.status_code == 200 and {n["id"] for n in prev.json["data"]["nodes"]} == {"AAA", "BBB", "XXX"}
    r = client.post('/api/pipeline/runs', json={"task_id": task_id, "name": "m", "market_cap_tiers": ["large"]})
    assert r.status_code == 201
    run_id = r.json["data"]["run_id"]
    assert r.json["data"]["steps"]["universe"]["summary"]["company_map"]["clusters"] == 2
    m = client.get(f'/api/pipeline/runs/{run_id}/company-map').json["data"]
    assert len(m["nodes"]) == 6 and len(m["clusters"]) == 2
    assert client.get('/api/pipeline/runs/nope/company-map').status_code == 404
