"""
Universe & Correlation Graph 数据模型
StockNode / CorrelationEdge / StockCluster —— S&P 500 股票关联图谱的 schema。

本阶段仅定义数据模型，不含筛选/相关性计算逻辑（后续阶段实现）。
旧的 entity/relation 图谱（Zep Cloud-backed，见 services/zep_entity_reader.py）
保持不变，仅供参考，不受本模块影响。
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CorrelationMethod(str, Enum):
    """相关性计算方法"""
    PEARSON = "pearson"
    SPEARMAN = "spearman"


class StockCluster(SQLModel, table=True):
    """股票聚类（如 Louvain 社区发现的输出）"""
    __tablename__ = "stock_clusters"

    cluster_id: Optional[int] = Field(default=None, primary_key=True)
    label: str
    description: Optional[str] = None
    method: str = Field(default="louvain")
    created_at: datetime = Field(default_factory=_utcnow)


class StockNode(SQLModel, table=True):
    """图谱中的一个股票节点"""
    __tablename__ = "stock_nodes"

    ticker: str = Field(primary_key=True)
    name: str
    sector: str
    market_cap: float
    cluster_id: Optional[int] = Field(default=None, foreign_key="stock_clusters.cluster_id")
    last_updated: datetime = Field(default_factory=_utcnow)


class CorrelationEdge(SQLModel, table=True):
    """两只股票之间的相关性边"""
    __tablename__ = "correlation_edges"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_ticker: str = Field(foreign_key="stock_nodes.ticker")
    target_ticker: str = Field(foreign_key="stock_nodes.ticker")
    correlation_weight: float
    method: CorrelationMethod
    computed_at: datetime = Field(default_factory=_utcnow)
