"""
Company map — the network of the companies being analysed, built BEFORE any news.

  nodes     one per company (sector, industry, market cap)
  bridges   from the correlation of daily returns over the year ending on as_of
            (no data after as_of is used):
              * the minimum spanning tree of the correlation distance
                d = sqrt(2 (1 - rho)) — the classic "market backbone": every
                company is linked to the peer it moves most like, no cycles;
              * every other pair with rho >= STRONG_RHO (the UI can raise the bar)
  clusters  Louvain communities on the backbone + strong ties, weighted by rho;
            named after their most common GICS sub-industries, so a cluster can
            mix sectors (banks + airlines when they trade together).

compute_map is pure (returns DataFrame in, dict out); build_map downloads the prices.
"""

import math
import threading
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd

from ..data_layer.price_history import fetch_close_history
from ..utils.logger import get_logger

logger = get_logger('mirofish.news_pipeline.company_map')

LOOKBACK_DAYS = 365
MIN_OVERLAP = 120            # trading days two tickers must share to get a correlation
STRONG_RHO = 0.4             # extra (non-tree) edges sent to the UI; it filters further
MAX_EXTRA_EDGES = 1500
CLUSTER_RHO = 0.3            # ties used for community detection
MAX_TICKERS = 400
RANDOM_SEED = 42


def _cluster_name(industries: List[str]) -> str:
    counts: Dict[str, int] = {}
    for ind in industries:
        counts[ind or "Unclassified"] = counts.get(ind or "Unclassified", 0) + 1
    top = [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    if len(top) == 1:
        return top[0]
    return ", ".join(top[:-1]) + " and " + top[-1]


def compute_map(returns: pd.DataFrame, meta: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    returns: daily returns, one column per ticker. meta: ticker -> {company_name, gics_sector,
    gics_sub_industry, market_cap, market_cap_tier}. Tickers without enough history become
    isolated nodes (listed in `no_history`).
    """
    usable = [t for t in returns.columns if returns[t].notna().sum() >= MIN_OVERLAP]
    corr = returns[usable].corr(min_periods=MIN_OVERLAP) if usable else pd.DataFrame()
    tickers = list(meta)

    g = nx.Graph()
    g.add_nodes_from(usable)
    pairs: List[Tuple[str, str, float]] = []
    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            rho = corr.at[a, b]
            if rho == rho:          # not NaN
                pairs.append((a, b, float(rho)))
                g.add_edge(a, b, rho=float(rho), dist=math.sqrt(max(0.0, 2 * (1 - float(rho)))))

    mst = nx.minimum_spanning_tree(g, weight="dist") if g.number_of_edges() else nx.Graph()
    mst_keys = {tuple(sorted((a, b))) for a, b in mst.edges()}

    edges = [{"source": a, "target": b, "rho": round(d["rho"], 4), "mst": True}
             for a, b, d in mst.edges(data=True)]
    extra = sorted((p for p in pairs if p[2] >= STRONG_RHO and tuple(sorted(p[:2])) not in mst_keys),
                   key=lambda p: -p[2])[:MAX_EXTRA_EDGES]
    edges += [{"source": a, "target": b, "rho": round(r, 4), "mst": False} for a, b, r in extra]

    # communities on the backbone + ties above CLUSTER_RHO (weights = rho, floored for MST links)
    cg = nx.Graph()
    cg.add_nodes_from(usable)
    for a, b, d in mst.edges(data=True):
        cg.add_edge(a, b, weight=max(d["rho"], 0.05))
    for a, b, r in pairs:
        if r >= CLUSTER_RHO:
            cg.add_edge(a, b, weight=r)
    communities = (nx.community.louvain_communities(cg, weight="weight", seed=RANDOM_SEED)
                   if cg.number_of_nodes() else [])
    communities = sorted((sorted(c) for c in communities), key=lambda c: (-len(c), c[0]))

    cluster_of: Dict[str, int] = {}
    clusters = []
    for cid, members in enumerate(communities):
        for t in members:
            cluster_of[t] = cid
        inner = [corr.at[a, b] for i, a in enumerate(members) for b in members[i + 1:]
                 if corr.at[a, b] == corr.at[a, b]]
        clusters.append({
            "id": cid,
            "name": _cluster_name([meta[t].get("gics_sub_industry") for t in members]),
            "size": len(members),
            "sectors": sorted({meta[t].get("gics_sector") for t in members}),
            "avg_rho": round(float(np.mean(inner)), 3) if inner else None,
            "members": members,
        })

    nodes = []
    for t in tickers:
        m = meta[t]
        top = []
        if t in usable:
            peers = corr[t].drop(t).dropna().sort_values(ascending=False)
            top = [{"ticker": p, "rho": round(float(r), 3)} for p, r in peers.head(3).items()]
        nodes.append({
            "id": t, "company_name": m.get("company_name"), "sector": m.get("gics_sector"),
            "industry": m.get("gics_sub_industry"), "market_cap": m.get("market_cap"),
            "tier": m.get("market_cap_tier"), "cluster": cluster_of.get(t),
            "degree": mst.degree(t) if t in mst else 0, "top_peers": top,
        })
    return {
        "nodes": nodes,
        "edges": edges,
        "clusters": clusters,
        "no_history": sorted(set(tickers) - set(usable)),
        "params": {"lookback_days": LOOKBACK_DAYS, "min_overlap": MIN_OVERLAP, "strong_rho": STRONG_RHO,
                   "cluster_rho": CLUSTER_RHO, "method": "MST of sqrt(2(1-rho)) + strong ties; Louvain clusters"},
    }


# closes per (as_of, ticker), so rebuilding the map as the list changes does not re-download
_PRICE_CACHE: Dict[Tuple[str, str], Dict[str, float]] = {}
_CACHE_LOCK = threading.Lock()


def _closes(tickers: List[str], as_of: str) -> Dict[str, Dict[str, float]]:
    with _CACHE_LOCK:
        missing = [t for t in tickers if (as_of, t) not in _PRICE_CACHE]
    if missing:
        end = date.fromisoformat(as_of)
        fetched = fetch_close_history(missing, end - timedelta(days=LOOKBACK_DAYS), end, adjusted=True)
        with _CACHE_LOCK:
            for t in missing:
                _PRICE_CACHE[(as_of, t)] = fetched.get(t, {})
    with _CACHE_LOCK:
        return {t: _PRICE_CACHE[(as_of, t)] for t in tickers}


def build_map(entries: List[Dict[str, Any]], as_of: str) -> Dict[str, Any]:
    """entries = universe rows (ticker, company_name, gics_sector, gics_sub_industry, market_cap, ...)."""
    meta = {e["ticker"]: e for e in entries}
    tickers = list(meta)
    if not tickers:
        raise ValueError("No companies to map")
    if len(tickers) > MAX_TICKERS:
        raise ValueError(f"At most {MAX_TICKERS} companies on one map")
    closes = _closes(tickers, as_of)
    frame = pd.DataFrame({t: pd.Series(c, dtype=float) for t, c in closes.items() if c})
    if not frame.empty:
        frame.index = pd.to_datetime(frame.index)
        frame = frame.sort_index()
        frame = frame[frame.index <= pd.Timestamp(as_of)]          # never past as_of
    returns = frame.pct_change(fill_method=None).iloc[1:] if not frame.empty else pd.DataFrame()
    result = compute_map(returns, meta)
    result["as_of_date"] = as_of
    result["window"] = ({"start": frame.index[0].date().isoformat(), "end": frame.index[-1].date().isoformat(),
                         "days": int(len(returns))} if not frame.empty else None)
    return result


def build_map_safe(entries: List[Dict[str, Any]], as_of: str) -> Optional[Dict[str, Any]]:
    """For lock time: a failed map must never block locking the universe."""
    try:
        return build_map(entries, as_of)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"company map failed: {e}")
        return None
