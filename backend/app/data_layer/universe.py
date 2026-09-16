"""
Universe screening —— S&P 500 股票筛选（取代原本的 /api/graph/ontology/generate 文档分析流程）

漏斗顺序（先便宜后昂贵）：
    1. 从静态文件 sp500_constituents.json 读取 503 只成分股（零 API 调用）
    2. 按 sector 预过滤（同样零 API 调用，用静态文件里的 GICS sector）
    3. 只对通过 sector 预过滤的 ticker 调用 yfinance 获取实时 market_cap（+权威 sector/name）
    4. 按 min_market_cap 做最终过滤
    5. Upsert 进 StockNode 表

本模块不含任何 LLM 调用。
"""

import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Optional

import yfinance as yf
from sqlmodel import select

from ..db import get_session
from ..models.universe_graph import StockNode
from ..utils.logger import get_logger

logger = get_logger('mirofish.universe')

CONSTITUENTS_PATH = os.path.join(os.path.dirname(__file__), 'sp500_constituents.json')

_MAX_FETCH_WORKERS = 8
_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.0

_constituents_cache: Optional[list[dict]] = None


def load_sp500_constituents() -> list[dict]:
    """加载静态 S&P 500 成分股列表（ticker/name/sector），进程内缓存一次"""
    global _constituents_cache
    if _constituents_cache is None:
        with open(CONSTITUENTS_PATH, 'r', encoding='utf-8') as f:
            _constituents_cache = json.load(f)
    return _constituents_cache


def _sector_matches(requested_sectors: Optional[list[str]], actual_sector: str) -> bool:
    """大小写不敏感的子串匹配，兼容 yfinance ('Technology') 与
    GICS ('Information Technology') 两套不同的行业命名"""
    if not requested_sectors:
        return True
    actual_lower = (actual_sector or "").lower()
    return any(req.strip().lower() in actual_lower for req in requested_sectors if req.strip())


def _fetch_ticker_snapshot(ticker: str) -> Optional[dict[str, Any]]:
    """带退避重试地获取单只股票的 sector/market_cap/name；
    失败或数据不完整（退市/无数据）时返回 None，由调用方跳过并记录"""
    last_error: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            info = yf.Ticker(ticker).info
            market_cap = info.get('marketCap')
            sector = info.get('sector')
            name = info.get('longName') or info.get('shortName')
            if market_cap is None or sector is None or not name:
                # 数据不完整（退市/无覆盖），不算异常，不重试
                logger.warning(f"数据不完整，跳过 {ticker}: sector={sector}, market_cap={market_cap}, name={name}")
                return None
            return {
                'ticker': ticker,
                'name': name,
                'sector': sector,
                'market_cap': float(market_cap),
            }
        except Exception as error:  # yfinance/网络异常，指数退避重试
            last_error = error
            if attempt < _MAX_RETRIES - 1:
                delay = _BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(f"获取 {ticker} 失败（第 {attempt + 1} 次），{delay:.1f}s 后重试: {error}")
                time.sleep(delay)

    logger.error(f"获取 {ticker} 最终失败，跳过: {last_error}")
    return None


def screen_universe(
    sectors: Optional[list[str]] = None,
    min_market_cap: Optional[float] = None,
) -> dict[str, Any]:
    """
    执行完整筛选漏斗，返回：
        {
            "candidates_prefiltered": int,  # sector 预过滤后、发起 yfinance 请求前的数量
            "fetched_ok": int,              # yfinance 成功返回的数量
            "fetch_failed": int,            # yfinance 请求失败/数据不完整而跳过的数量
            "passed_market_cap": list[dict],  # 最终通过 min_market_cap 过滤的候选
        }
    """
    constituents = load_sp500_constituents()

    prefiltered = [c for c in constituents if _sector_matches(sectors, c['sector'])]
    logger.info(
        f"Sector 预过滤: {len(constituents)} -> {len(prefiltered)} "
        f"(sectors={sectors or '全部'})"
    )

    snapshots: list[dict[str, Any]] = []
    fetch_failed = 0
    with ThreadPoolExecutor(max_workers=_MAX_FETCH_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_ticker_snapshot, c['ticker']): c['ticker']
            for c in prefiltered
        }
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                fetch_failed += 1
            else:
                snapshots.append(result)

    if min_market_cap is not None:
        passed = [s for s in snapshots if s['market_cap'] >= min_market_cap]
    else:
        passed = snapshots

    logger.info(
        f"yfinance 获取成功: {len(snapshots)}, 失败/跳过: {fetch_failed}, "
        f"min_market_cap 过滤后: {len(passed)}"
    )

    return {
        "candidates_prefiltered": len(prefiltered),
        "fetched_ok": len(snapshots),
        "fetch_failed": fetch_failed,
        "passed_market_cap": passed,
    }


def upsert_stock_nodes(candidates: list[dict[str, Any]]) -> list[StockNode]:
    """按 ticker upsert StockNode（存在则更新，不存在则插入）"""
    now = datetime.now(timezone.utc)
    upserted: list[StockNode] = []

    with get_session() as session:
        for candidate in candidates:
            existing = session.get(StockNode, candidate['ticker'])
            if existing:
                existing.name = candidate['name']
                existing.sector = candidate['sector']
                existing.market_cap = candidate['market_cap']
                existing.last_updated = now
                session.add(existing)
                upserted.append(existing)
            else:
                node = StockNode(
                    ticker=candidate['ticker'],
                    name=candidate['name'],
                    sector=candidate['sector'],
                    market_cap=candidate['market_cap'],
                    last_updated=now,
                )
                session.add(node)
                upserted.append(node)
        session.commit()
        for node in upserted:
            session.refresh(node)

    return upserted


def screen_and_upsert_universe(
    sectors: Optional[list[str]] = None,
    min_market_cap: Optional[float] = None,
) -> dict[str, Any]:
    """screen_universe() + upsert_stock_nodes()，供 API 层直接调用"""
    result = screen_universe(sectors=sectors, min_market_cap=min_market_cap)
    upserted_nodes = upsert_stock_nodes(result["passed_market_cap"])

    return {
        "candidates_prefiltered": result["candidates_prefiltered"],
        "fetched_ok": result["fetched_ok"],
        "fetch_failed": result["fetch_failed"],
        "upserted_count": len(upserted_nodes),
        "stock_nodes": [
            {
                "ticker": n.ticker,
                "name": n.name,
                "sector": n.sector,
                "market_cap": n.market_cap,
                "cluster_id": n.cluster_id,
                "last_updated": n.last_updated.isoformat(),
            }
            for n in upserted_nodes
        ],
    }


if __name__ == "__main__":
    demo = screen_and_upsert_universe(sectors=["Technology"])
    print(f"Upserted {demo['upserted_count']} nodes")
    for row in demo["stock_nodes"][:3]:
        print(row)
