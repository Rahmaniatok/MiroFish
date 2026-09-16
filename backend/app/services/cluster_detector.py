"""
Cluster detection —— 取代原本 "Generated Entity Types/Relation Types" 的本体生成流程。
从 CorrelationEdge 构建 networkx 图，跑 Louvain 社区发现，
每个 cluster 只调用一次 LLM 生成 label + description（不逐 ticker 调用）。

只读 StockNode / CorrelationEdge，只写 StockCluster + StockNode.cluster_id。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Optional

import networkx as nx
from sqlmodel import select

from ..db import get_session
from ..models.universe_graph import CorrelationEdge, CorrelationMethod, StockCluster, StockNode
from ..utils.llm_client import LLMClient, LLMResponseError
from ..utils.logger import get_logger

logger = get_logger('mirofish.cluster_detector')

_MAX_LLM_PARALLEL = 5
_MAX_MEMBERS_IN_PROMPT = 40  # 防止超大 cluster 把 prompt 撑爆


def _build_correlation_graph(correlation_method: str) -> nx.Graph:
    """
    只用正相关边构建图 —— Louvain/modularity 把边权当"吸引力"看待，
    负相关（反向变动）的两只股票不应该被拉进同一个 cluster。
    """
    method_enum = CorrelationMethod(correlation_method)
    with get_session() as session:
        edges = session.exec(
            select(CorrelationEdge).where(
                CorrelationEdge.method == method_enum,
                CorrelationEdge.correlation_weight > 0,
            )
        ).all()

    graph = nx.Graph()
    for edge in edges:
        graph.add_edge(edge.source_ticker, edge.target_ticker, weight=edge.correlation_weight)
    return graph


def _fallback_label(tickers_sectors: list[tuple[str, str]]) -> dict[str, str]:
    sector_counts: dict[str, int] = {}
    for _, sector in tickers_sectors:
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
    top_sector = max(sector_counts, key=sector_counts.get) if sector_counts else "Mixed"
    return {
        "label": f"{top_sector} cluster ({len(tickers_sectors)} stocks)",
        "description": (
            f"A correlation-based cluster of {len(tickers_sectors)} stocks, "
            f"predominantly in the {top_sector} sector. LLM description "
            f"generation failed for this cluster; this is an automatic fallback."
        ),
    }


def _label_cluster_with_llm(client: LLMClient, tickers_sectors: list[tuple[str, str]]) -> dict[str, str]:
    shown = tickers_sectors[:_MAX_MEMBERS_IN_PROMPT]
    listing = ", ".join(f"{t} ({s})" for t, s in shown)
    if len(tickers_sectors) > len(shown):
        listing += f", and {len(tickers_sectors) - len(shown)} more"

    messages = [
        {
            "role": "system",
            "content": (
                "You are a financial analyst. You are given a cluster of stocks "
                "that move together, found via correlation-based community "
                "detection. Respond ONLY with JSON of the form "
                '{"label": "...", "description": "..."}. '
                "The label is a short thematic phrase (3-6 words). "
                "The description is 2-3 sentences summarizing the cluster's "
                "common theme (sector overlap, business model similarity, or "
                "shared macro driver), in a style like: 'mega-cap technology "
                "cluster encompassing cloud software, cybersecurity, and "
                "enterprise SaaS providers.'"
            ),
        },
        {
            "role": "user",
            "content": f"Cluster members ({len(tickers_sectors)} stocks): {listing}",
        },
    ]

    try:
        result = client.chat_json(messages, temperature=0.3, max_tokens=400)
    except LLMResponseError as error:
        logger.warning(f"LLM cluster labeling failed, using fallback label: {error}")
        return _fallback_label(tickers_sectors)

    label = str(result.get("label", "")).strip()
    description = str(result.get("description", "")).strip()
    if not label or not description:
        return _fallback_label(tickers_sectors)
    return {"label": label, "description": description}


def detect_clusters(method: str = "louvain", correlation_method: str = "pearson") -> dict[str, Any]:
    """
    读取指定 correlation_method 的正相关 CorrelationEdge -> networkx 图 ->
    Louvain 社区发现 -> 每个 cluster(>=2 成员) 调用一次 LLM 生成 label/description
    -> upsert StockCluster + 回填 StockNode.cluster_id（幂等：先清空同一
    method(编码了 correlation_method) 下的旧 cluster 再重建）。
    """
    if method != "louvain":
        raise ValueError(f"Unsupported clustering method: {method}")
    if correlation_method not in (CorrelationMethod.PEARSON, CorrelationMethod.SPEARMAN):
        raise ValueError(f"Unknown correlation_method: {correlation_method}")

    graph = _build_correlation_graph(correlation_method)
    if graph.number_of_nodes() == 0:
        raise ValueError(
            f"No positive-weight CorrelationEdge rows found for "
            f"correlation_method={correlation_method}; build correlation edges "
            f"first via POST /api/graph/correlation/build"
        )

    communities = nx.community.louvain_communities(graph, weight='weight', seed=42)
    communities = [c for c in communities if len(c) >= 2]  # 孤立节点不算 cluster
    communities.sort(key=len, reverse=True)

    logger.info(f"Louvain 检测到 {len(communities)} 个 cluster（correlation_method={correlation_method}）")

    all_tickers = {t for community in communities for t in community}
    with get_session() as session:
        nodes = session.exec(
            select(StockNode).where(StockNode.ticker.in_(all_tickers))
        ).all() if all_tickers else []
    sector_by_ticker = {n.ticker: n.sector for n in nodes}

    cluster_members: list[list[tuple[str, str]]] = [
        sorted((t, sector_by_ticker.get(t, "Unknown")) for t in community)
        for community in communities
    ]

    # 每个 cluster 只调用一次 LLM，并发上限 5，避免打爆 rate limit
    llm_client = LLMClient()
    labels: list[Optional[dict[str, str]]] = [None] * len(cluster_members)
    with ThreadPoolExecutor(max_workers=_MAX_LLM_PARALLEL) as pool:
        futures = {
            pool.submit(_label_cluster_with_llm, llm_client, members): idx
            for idx, members in enumerate(cluster_members)
        }
        for future in as_completed(futures):
            idx = futures[future]
            labels[idx] = future.result()

    # StockCluster.method 没有单独的 correlation_method 列（Prompt 1 定的 schema），
    # 编码进字符串里区分，重复运行同一 (method, correlation_method) 时能正确幂等替换
    stored_method = f"{method}_{correlation_method}"
    now = datetime.now(timezone.utc)

    with get_session() as session:
        old_clusters = session.exec(
            select(StockCluster).where(StockCluster.method == stored_method)
        ).all()
        old_cluster_ids = [c.cluster_id for c in old_clusters]
        if old_cluster_ids:
            old_members = session.exec(
                select(StockNode).where(StockNode.cluster_id.in_(old_cluster_ids))
            ).all()
            for node in old_members:
                node.cluster_id = None
                session.add(node)
            session.commit()
            for cluster in old_clusters:
                session.delete(cluster)
            session.commit()

        new_clusters: list[StockCluster] = []
        for members, label_info in zip(cluster_members, labels):
            cluster = StockCluster(
                label=label_info["label"],
                description=label_info["description"],
                method=stored_method,
                created_at=now,
            )
            session.add(cluster)
            session.flush()  # 拿到自增 cluster_id，先不 commit
            new_clusters.append(cluster)

            tickers = [t for t, _ in members]
            member_nodes = session.exec(
                select(StockNode).where(StockNode.ticker.in_(tickers))
            ).all()
            for node in member_nodes:
                node.cluster_id = cluster.cluster_id
                session.add(node)

        session.commit()
        for cluster in new_clusters:
            session.refresh(cluster)

    # 统计：曾经有 CorrelationEdge 的 StockNode 中，多少现在拿到了 cluster_id
    method_enum = CorrelationMethod(correlation_method)
    with get_session() as session:
        edge_pairs = session.exec(
            select(CorrelationEdge.source_ticker, CorrelationEdge.target_ticker)
            .where(CorrelationEdge.method == method_enum)
        ).all()
        tickers_with_edges = {t for pair in edge_pairs for t in pair}

        all_nodes = session.exec(select(StockNode)).all()
        nodes_with_edges = [n for n in all_nodes if n.ticker in tickers_with_edges]
        nodes_with_edges_and_cluster = [n for n in nodes_with_edges if n.cluster_id is not None]

    pct_null = (
        100.0 * (len(nodes_with_edges) - len(nodes_with_edges_and_cluster)) / len(nodes_with_edges)
        if nodes_with_edges else 0.0
    )

    clusters_out = [
        {
            "cluster_id": cluster.cluster_id,
            "label": cluster.label,
            "description": cluster.description,
            "member_count": len(members),
            "sample_members": [t for t, _ in members[:10]],
        }
        for cluster, members in zip(new_clusters, cluster_members)
    ]
    clusters_out.sort(key=lambda c: c["member_count"], reverse=True)

    return {
        "method": method,
        "correlation_method": correlation_method,
        "cluster_count": len(new_clusters),
        "clusters": clusters_out,
        "stock_nodes_with_correlation_edges": len(nodes_with_edges),
        "stock_nodes_with_cluster_id": len(nodes_with_edges_and_cluster),
        "stock_nodes_null_cluster_pct": round(pct_null, 2),
    }
