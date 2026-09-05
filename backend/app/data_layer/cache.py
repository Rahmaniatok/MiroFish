"""
市场数据缓存层
用 sqlite3 缓存 market_data.py 抓取到的价格/基本面数据，避免对 yfinance
的重复请求。不做 ORM 封装，MVP 阶段保持简单直接。

缓存键为 (ticker, as_of_date, data_type):
- as_of_date 为 None 时代表"实时数据"，命中缓存后还需检查 TTL
  （LIVE_DATA_TTL_MINUTES 分钟内视为新鲜，否则返回 None 让调用方重新抓取）
- as_of_date 有值时代表某个历史快照，历史数据不会变化，只要命中即直接返回，
  不检查过期时间

注意(SQLite NULL 语义): SQL 标准里 NULL 与 NULL 不相等，因此
UNIQUE(ticker, as_of_date, data_type) 这个约束对 as_of_date 为 NULL 的多行
"实时数据" 并不能天然去重/触发 ON CONFLICT。这里改用显式的
"先 DELETE 匹配行、再 INSERT" 来实现 upsert，并用 SQLite 的 `IS` 运算符做
NULL-safe 比较（`col IS ?` 在参数为 None 时等价于 `col IS NULL`，参数非空时
等价于 `col = ?`），从而让 as_of_date 为 None 和有值的两种情况用同一套查询
逻辑正确处理。
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..utils.logger import get_logger

logger = get_logger('mirofish.data_layer.cache')

DB_PATH = os.path.join(os.path.dirname(__file__), 'market_cache.db')

# 实时数据("as_of_date"为None)的缓存有效期
LIVE_DATA_TTL_MINUTES = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_data_cache (
    ticker TEXT NOT NULL,
    as_of_date TEXT,
    data_type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (ticker, as_of_date, data_type)
);
"""


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_SCHEMA)
    return conn


def get_cached(ticker: str, data_type: str, as_of_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    查询缓存

    Args:
        ticker: 股票代码
        data_type: "price" 或 "fundamental"
        as_of_date: None 表示查询实时数据缓存(受TTL限制)；传入具体日期
                     (ISO格式字符串)则查询该历史快照(命中即返回，永不过期)

    Returns:
        命中且有效时返回缓存的数据字典，否则返回 None
    """
    ticker = (ticker or "").strip().upper()

    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT data_json, fetched_at FROM market_data_cache "
            "WHERE ticker = ? AND data_type = ? AND as_of_date IS ?",
            (ticker, data_type, as_of_date),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data_json, fetched_at = row

    if as_of_date is not None:
        # 历史快照数据不会变化，命中即可直接返回
        return json.loads(data_json)

    # 实时数据需要检查 TTL
    fetched_dt = datetime.fromisoformat(fetched_at)
    age = datetime.now(timezone.utc) - fetched_dt
    if age > timedelta(minutes=LIVE_DATA_TTL_MINUTES):
        logger.debug(f"缓存已过期: {ticker}/{data_type} (距上次抓取 {age})")
        return None

    return json.loads(data_json)


def set_cache(ticker: str, data_type: str, data: Dict[str, Any], as_of_date: Optional[str] = None) -> None:
    """
    写入/更新缓存(upsert)

    Args:
        ticker: 股票代码
        data_type: "price" 或 "fundamental"
        data: 要缓存的数据字典(通常是 fetch_price_data/fetch_fundamental_data 的返回值)
        as_of_date: None 表示这是实时数据；传入具体日期表示这是该日期的历史快照
    """
    ticker = (ticker or "").strip().upper()
    fetched_at = datetime.now(timezone.utc).isoformat()
    data_json = json.dumps(data, ensure_ascii=False)

    conn = _get_connection()
    try:
        with conn:
            # 见模块顶部注释: as_of_date 为 NULL 时 UNIQUE 约束不会自动去重，
            # 所以先删除同 key 的旧行，再插入新行，手动实现 upsert 语义
            conn.execute(
                "DELETE FROM market_data_cache WHERE ticker = ? AND data_type = ? AND as_of_date IS ?",
                (ticker, data_type, as_of_date),
            )
            conn.execute(
                "INSERT INTO market_data_cache "
                "(ticker, as_of_date, data_type, data_json, fetched_at) VALUES (?, ?, ?, ?, ?)",
                (ticker, as_of_date, data_type, data_json, fetched_at),
            )
    finally:
        conn.close()
