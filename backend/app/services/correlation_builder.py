"""
Correlation Graph builder —— 取代原本 /api/graph/build 的 GraphRAG 文本图谱构建流程。
基于历史价格数据计算股票两两相关性，写入 CorrelationEdge。

只读 StockNode（取 ticker 列表），只写 CorrelationEdge —— 不修改 StockNode。
不含任何 LLM 调用。
"""

from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlmodel import select

from ..data_layer.market_data import fetch_price_history
from ..db import get_session
from ..models.universe_graph import CorrelationEdge, CorrelationMethod, StockNode
from ..utils.logger import get_logger

logger = get_logger('mirofish.correlation_builder')

# 相关性边的入库阈值 —— 只有 |correlation_weight| 超过这个值才存成 CorrelationEdge。
# 放在这里作为可调常量，不要散落在计算逻辑里写死。
CORRELATION_EDGE_THRESHOLD = 0.5


def build_correlation_edges(
    method: str,
    calc_type: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """
    读取全部 StockNode -> 拉历史价格 -> （可选）转 daily return -> 相关性矩阵
    -> 删除同 method 的旧 CorrelationEdge -> 写入新的（仅 |weight| > 阈值 的股票对）。

    method: "pearson" | "spearman"
    calc_type: "return" | "price"
    """
    if method not in (CorrelationMethod.PEARSON, CorrelationMethod.SPEARMAN):
        raise ValueError(f"Unknown method: {method}")
    if calc_type not in ("return", "price"):
        raise ValueError(f"Unknown type: {calc_type}")

    with get_session() as session:
        tickers = session.exec(select(StockNode.ticker)).all()

    if len(tickers) < 2:
        raise ValueError("Need at least 2 StockNode rows to compute correlations")

    logger.info(f"Fetching price history for {len(tickers)} tickers ({start_date} ~ {end_date})")
    prices = fetch_price_history(tickers, start_date, end_date)
    tickers_with_data = list(prices.columns)
    tickers_dropped_no_data = sorted(set(tickers) - set(tickers_with_data))

    if calc_type == "return":
        series = prices.pct_change().dropna(how="all")
    else:
        series = prices

    corr_matrix = series.corr(method=method)

    method_enum = CorrelationMethod(method)
    now = datetime.now(timezone.utc)

    new_edges: list[CorrelationEdge] = []
    cols = corr_matrix.columns.tolist()
    for i, source in enumerate(cols):
        for target in cols[i + 1:]:
            weight = corr_matrix.loc[source, target]
            if pd.isna(weight):
                continue
            if abs(weight) <= CORRELATION_EDGE_THRESHOLD:
                continue
            # 无序对，统一按字典序存 source/target，保证多次运行结果一致（幂等）
            lo, hi = sorted((source, target))
            new_edges.append(CorrelationEdge(
                source_ticker=lo,
                target_ticker=hi,
                correlation_weight=float(weight),
                method=method_enum,
                computed_at=now,
            ))

    with get_session() as session:
        old_edges = session.exec(
            select(CorrelationEdge).where(CorrelationEdge.method == method_enum)
        ).all()
        deleted_count = len(old_edges)
        for edge in old_edges:
            session.delete(edge)
        session.commit()

        for edge in new_edges:
            session.add(edge)
        session.commit()
        for edge in new_edges:
            session.refresh(edge)

    logger.info(
        f"Correlation build done: deleted {deleted_count} old edges (method={method}), "
        f"created {len(new_edges)} new edges (threshold={CORRELATION_EDGE_THRESHOLD})"
    )

    top_edges = sorted(new_edges, key=lambda e: abs(e.correlation_weight), reverse=True)[:10]

    return {
        "method": method,
        "type": calc_type,
        "start_date": start_date,
        "end_date": end_date,
        "tickers_considered": len(tickers),
        "tickers_with_data": len(tickers_with_data),
        "tickers_dropped_no_data": tickers_dropped_no_data,
        "edges_deleted": deleted_count,
        "edges_created": len(new_edges),
        "correlation_edge_threshold": CORRELATION_EDGE_THRESHOLD,
        "top_edges": [
            {
                "source_ticker": e.source_ticker,
                "target_ticker": e.target_ticker,
                "correlation_weight": e.correlation_weight,
            }
            for e in top_edges
        ],
    }
