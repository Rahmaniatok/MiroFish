"""
选股域(universe)筛选模块 — Phase 1d

在任何 LLM / Agent 模拟启动之前，先用两个"便宜"的维度把 S&P 500 全域
(~500 只)收窄成一个候选池：

  1. GICS 行业(gics_sector)   —— 直接取自 S&P 500 成分股名单(维基百科)
  2. 市值档位(market_cap_tier) —— 由 get_stock_context 里的 fundamental.market_cap 分档

本模块严格属于"数据层筛选"，不调用任何 LLM，也不 import graph_building /
persona / simulation 相关代码。价格/基本面的抓取逻辑完全复用 Phase 1 的
market_data.get_stock_context，本文件不重复实现任何 yfinance 抓取。

数据来源与缓存
--------------
get_sp500_constituents() 抓取维基百科 "List of S&P 500 companies" 成分表，
用 Phase 1b 的 SQLite 缓存(cache.py 里的同一个 market_cache.db)缓存，key 为
"sp500_constituents"，TTL 7 天(成分变动很少)。抓取失败时回退到随包分发的
静态 CSV(sp500_constituents.csv)。

关于 as_of_date
---------------
screen_universe 会把 as_of_date 透传给 get_stock_context：
  - 价格部分不会有未来数据泄漏(见 market_data.fetch_price_data)。
  - 但市值(market_cap)来自 fundamental，而 fundamental 目前**不是**
    point-in-time 的(见 market_data 模块顶部的大段说明)。因此在历史
    回测语义下，这里的市值分档实际用的是"当前市值"，存在 lookahead
    bias，等 Phase 8 接入真正的历史基本面源后才算严谨。
  - 同理，S&P 500 成分名单也只有"当前"这一份快照，历史某天的真实成分
    可能不同。
"""

import csv
import io
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from ..utils.logger import get_logger
from .cache import DB_PATH, _get_connection
from .market_data import get_stock_context

logger = get_logger('mirofish.data_layer.universe')

WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_WIKI_TABLE_ID = "constituents"
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (research; MiroFish data layer / universe.py)"}
_HTTP_TIMEOUT = 20

# Phase 1b 缓存约定：复用 market_cache.db 同一张表，用一个"伪 ticker"当 key
CONSTITUENTS_CACHE_KEY = "sp500_constituents"
_CONSTITUENTS_DATA_TYPE = "constituents"
CONSTITUENTS_CACHE_TTL_DAYS = 7

# 随包分发的静态兜底名单(与本文件同目录)
_FALLBACK_CSV_PATH = os.path.join(os.path.dirname(__file__), "sp500_constituents.csv")

# 市值档位阈值(美元)
_MEGA_CAP_MIN = 200e9   # > 200B      -> mega
_LARGE_CAP_MIN = 10e9   # 10B .. 200B -> large
_MID_CAP_MIN = 2e9      # 2B .. 10B   -> mid
#                         < 2B        -> small
MARKET_CAP_TIERS = ("mega", "large", "mid", "small")

# 逐个 ticker 之间的礼貌性间隔，降低被 yfinance 限流的概率
_PER_TICKER_SLEEP_SEC = 0.2
# 遇到疑似限流错误时的重试设置
_RATE_LIMIT_MAX_RETRIES = 4
_RATE_LIMIT_INITIAL_BACKOFF_SEC = 5.0
_RATE_LIMIT_BACKOFF_FACTOR = 2.0
_RATE_LIMIT_HINTS = ("too many requests", "rate limit", "429", "try again later")


# ---------------------------------------------------------------------------
# 1) S&P 500 成分名单
# ---------------------------------------------------------------------------
def _normalize_ticker(symbol: str) -> str:
    """维基百科里用 'BRK.B'，yfinance 用 'BRK-B'。"""
    return (symbol or "").strip().upper().replace(".", "-")


def _scrape_sp500_from_wikipedia() -> List[Dict[str, str]]:
    resp = requests.get(WIKI_SP500_URL, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    table = pd.read_html(io.StringIO(resp.text), attrs={"id": _WIKI_TABLE_ID})[0]

    required = {"Symbol", "Security", "GICS Sector"}
    missing = required - set(table.columns)
    if missing:
        raise ValueError(f"维基百科成分表缺少预期列: {missing}(实际列: {list(table.columns)})")

    rows: List[Dict[str, str]] = []
    for rec in table.to_dict("records"):
        rows.append({
            "ticker": _normalize_ticker(str(rec["Symbol"])),
            "company_name": str(rec["Security"]).strip(),
            "gics_sector": str(rec["GICS Sector"]).strip(),
        })
    if len(rows) < 400:
        raise ValueError(f"维基百科成分表只解析出 {len(rows)} 行，明显不完整，判定为抓取失败")
    return rows


def _load_sp500_from_fallback_csv() -> List[Dict[str, str]]:
    with open(_FALLBACK_CSV_PATH, newline="", encoding="utf-8") as f:
        rows = [
            {
                "ticker": _normalize_ticker(r["ticker"]),
                "company_name": r["company_name"].strip(),
                "gics_sector": r["gics_sector"].strip(),
            }
            for r in csv.DictReader(f)
        ]
    logger.warning(f"使用随包静态兜底名单 {_FALLBACK_CSV_PATH}(共 {len(rows)} 只)")
    return rows


def _read_constituents_cache() -> Optional[List[Dict[str, str]]]:
    """复用 Phase 1b 的 market_cache.db，但用 7 天 TTL(而不是 cache.py 的 30 分钟)。"""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT data_json, fetched_at FROM market_data_cache "
            "WHERE ticker = ? AND data_type = ? AND as_of_date IS NULL",
            (CONSTITUENTS_CACHE_KEY, _CONSTITUENTS_DATA_TYPE),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data_json, fetched_at = row
    age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
    if age.total_seconds() > CONSTITUENTS_CACHE_TTL_DAYS * 86400:
        logger.info(f"S&P 500 成分名单缓存已过期(距上次抓取 {age}),将重新抓取")
        return None
    return json.loads(data_json)


def _write_constituents_cache(rows: List[Dict[str, str]]) -> None:
    fetched_at = datetime.now(timezone.utc).isoformat()
    data_json = json.dumps(rows, ensure_ascii=False)
    conn = _get_connection()
    try:
        with conn:
            # 见 cache.py 顶部说明：as_of_date 为 NULL 时 UNIQUE 约束不去重，
            # 所以先 DELETE 再 INSERT 手动实现 upsert
            conn.execute(
                "DELETE FROM market_data_cache WHERE ticker = ? AND data_type = ? AND as_of_date IS NULL",
                (CONSTITUENTS_CACHE_KEY, _CONSTITUENTS_DATA_TYPE),
            )
            conn.execute(
                "INSERT INTO market_data_cache "
                "(ticker, as_of_date, data_type, data_json, fetched_at) VALUES (?, NULL, ?, ?, ?)",
                (CONSTITUENTS_CACHE_KEY, _CONSTITUENTS_DATA_TYPE, data_json, fetched_at),
            )
    except sqlite3.Error as e:
        logger.warning(f"写入成分名单缓存失败(忽略,不影响本次结果): {e}")
    finally:
        conn.close()


def get_sp500_constituents(force_refresh: bool = False) -> List[Dict[str, str]]:
    """
    返回当前 S&P 500 成分股列表，每项为
    {"ticker": str, "company_name": str, "gics_sector": str}。

    优先命中 7 天 TTL 的 SQLite 缓存；未命中则抓取维基百科成分表并写回缓存；
    抓取失败时回退到随包静态 CSV(此时不写缓存，以便下次重试)。

    Args:
        force_refresh: True 时跳过缓存直接重新抓取。
    """
    if not force_refresh:
        cached = _read_constituents_cache()
        if cached is not None:
            logger.info(f"Cache hit: S&P 500 成分名单(共 {len(cached)} 只)")
            return cached

    logger.info("Cache miss，从维基百科抓取 S&P 500 成分名单...")
    try:
        rows = _scrape_sp500_from_wikipedia()
        _write_constituents_cache(rows)
        logger.info(f"已抓取并缓存 S&P 500 成分名单(共 {len(rows)} 只)")
        return rows
    except Exception as e:
        logger.warning(f"抓取维基百科成分表失败: {e}")
        return _load_sp500_from_fallback_csv()


# ---------------------------------------------------------------------------
# 2) 市值分档
# ---------------------------------------------------------------------------
def classify_market_cap(market_cap: float) -> str:
    """
    把市值(美元)映射为档位字符串，取值范围见 MARKET_CAP_TIERS：

        mega  : > $200B
        large : $10B – $200B
        mid   : $2B – $10B
        small : < $2B

    market_cap 为 None / 非数值 / <= 0 时抛 ValueError（调用方应先自行过滤）。
    """
    if market_cap is None:
        raise ValueError("market_cap 为 None，无法分档")
    try:
        cap = float(market_cap)
    except (TypeError, ValueError):
        raise ValueError(f"market_cap 不是数值: {market_cap!r}")
    if cap != cap or cap <= 0:  # NaN 或非正
        raise ValueError(f"market_cap 非法(NaN 或 <= 0): {market_cap!r}")

    if cap > _MEGA_CAP_MIN:
        return "mega"
    if cap >= _LARGE_CAP_MIN:
        return "large"
    if cap >= _MID_CAP_MIN:
        return "mid"
    return "small"


# ---------------------------------------------------------------------------
# 3) 全域筛选
# ---------------------------------------------------------------------------
def _looks_like_rate_limit(message: str) -> bool:
    msg = (message or "").lower()
    return any(hint in msg for hint in _RATE_LIMIT_HINTS)


def _context_error_message(ctx: Dict[str, Any]) -> str:
    """从 get_stock_context 的结果里拼出人类可读的失败原因。"""
    parts = []
    price_err = (ctx.get("price") or {}).get("error")
    fund_err = (ctx.get("fundamental") or {}).get("error")
    if price_err:
        parts.append(f"price: {price_err}")
    if fund_err:
        parts.append(f"fundamental: {fund_err}")
    return " | ".join(parts) if parts else "get_stock_context 返回 success=False(无具体 error 字段)"


def _get_stock_context_with_retry(ticker: str, as_of_date: Optional[str]) -> Dict[str, Any]:
    """
    调用 Phase 1 的 get_stock_context；若失败且错误信息像限流，则指数退避重试。
    get_stock_context 本身不抛异常(内部已捕获)，所以这里也兜一层 try/except。
    """
    backoff = _RATE_LIMIT_INITIAL_BACKOFF_SEC
    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            ctx = get_stock_context(ticker, as_of_date=as_of_date)
        except Exception as e:  # noqa: BLE001 — 单个 ticker 不能拖垮整轮
            ctx = {"ticker": ticker, "success": False,
                   "price": {"error": f"异常: {e}"}, "fundamental": {"error": None}}

        if ctx.get("success"):
            return ctx

        err = _context_error_message(ctx)
        if attempt < _RATE_LIMIT_MAX_RETRIES and _looks_like_rate_limit(err):
            logger.warning(
                f"{ticker}: 疑似被限流({err})，{backoff:.0f}s 后重试 "
                f"({attempt + 1}/{_RATE_LIMIT_MAX_RETRIES})"
            )
            time.sleep(backoff)
            backoff *= _RATE_LIMIT_BACKOFF_FACTOR
            continue
        return ctx  # 非限流失败(或重试用尽)：交给调用方按 skip 处理

    return ctx


def _run_screen(
    sectors: Optional[List[str]],
    market_cap_tiers: Optional[List[str]],
    as_of_date: Optional[str],
) -> Dict[str, Any]:
    """
    筛选主循环。返回 {"passed": [...], "skipped": [...], "total": int}。
    screen_universe 是它的薄封装(只取 passed)；CLI 额外需要 skipped/total。
    """
    if market_cap_tiers is not None:
        unknown = set(market_cap_tiers) - set(MARKET_CAP_TIERS)
        if unknown:
            raise ValueError(f"未知的市值档位: {unknown}，合法取值: {MARKET_CAP_TIERS}")

    sector_filter = set(sectors) if sectors is not None else None
    tier_filter = set(market_cap_tiers) if market_cap_tiers is not None else None

    constituents = get_sp500_constituents()
    total = len(constituents)
    logger.info(
        f"开始筛选 S&P 500 全域({total} 只) | sectors={sectors or '不限'} "
        f"| market_cap_tiers={market_cap_tiers or '不限'} | as_of_date={as_of_date or 'live'}"
    )

    passed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for idx, row in enumerate(constituents, start=1):
        ticker = row["ticker"]
        logger.info(f"Processing {idx}/{total}: {ticker}")

        # 行业不匹配 —— 在抓数据之前就短路，省掉 get_stock_context 调用
        if sector_filter is not None and row["gics_sector"] not in sector_filter:
            continue

        ctx = _get_stock_context_with_retry(ticker, as_of_date)
        if not ctx or not ctx.get("success"):
            reason = _context_error_message(ctx) if ctx else "get_stock_context 返回 None"
            logger.warning(f"跳过 {ticker}: {reason}")
            skipped.append({"ticker": ticker, "reason": reason})
            continue

        market_cap = (ctx.get("fundamental") or {}).get("market_cap")
        try:
            tier = classify_market_cap(market_cap)
        except ValueError as e:
            reason = f"市值不可用: {e}"
            logger.warning(f"跳过 {ticker}: {reason}")
            skipped.append({"ticker": ticker, "reason": reason})
            continue

        if tier_filter is not None and tier not in tier_filter:
            continue

        passed.append({
            "ticker": ticker,
            "company_name": row["company_name"],
            "gics_sector": row["gics_sector"],
            "market_cap": float(market_cap),
            "market_cap_tier": tier,
        })

        if _PER_TICKER_SLEEP_SEC:
            time.sleep(_PER_TICKER_SLEEP_SEC)

    logger.info(
        f"筛选完成: 处理 {total} 只，跳过/失败 {len(skipped)} 只，通过 {len(passed)} 只"
    )
    for s in skipped:
        logger.debug(f"  skipped {s['ticker']}: {s['reason']}")

    passed.sort(key=lambda d: d["market_cap"], reverse=True)
    return {"passed": passed, "skipped": skipped, "total": total}


def screen_universe(
    sectors: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
    as_of_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    用 GICS 行业 + 市值档位筛选 S&P 500 全域。

    Args:
        sectors: None = 不限行业(全部通过)；否则只保留 gics_sector 在此列表中的
            ticker。行业名用 GICS 口径(维基百科成分表那一列)，例如
            "Information Technology" / "Health Care"。
        market_cap_tiers: None = 不限市值；否则只保留 classify_market_cap 结果
            在此列表中的 ticker，例如 ["mega", "large"]。
        as_of_date: 透传给 get_stock_context。None = live。注意 fundamental
            (市值)目前不是 point-in-time 的(见模块顶部说明)。

    Returns:
        通过筛选的条目列表(按市值降序)，每项:
        {
            "ticker": str, "company_name": str, "gics_sector": str,
            "market_cap": float, "market_cap_tier": str,
        }
        (返回的是可检视的结构，不是裸 ticker 列表。失败/跳过的 ticker 只记日志。)
    """
    return _run_screen(sectors, market_cap_tiers, as_of_date)["passed"]


def _fmt_market_cap(cap: float) -> str:
    if cap >= 1e12:
        return f"${cap / 1e12:.2f}T"
    if cap >= 1e9:
        return f"${cap / 1e9:.1f}B"
    if cap >= 1e6:
        return f"${cap / 1e6:.1f}M"
    return f"${cap:.0f}"


if __name__ == "__main__":
    EXAMPLE_SECTORS = ["Information Technology", "Health Care"]
    EXAMPLE_TIERS = ["mega", "large"]

    print("=" * 78)
    print("Phase 1d — universe screening 示例")
    print(f"  sectors          = {EXAMPLE_SECTORS}")
    print(f"  market_cap_tiers = {EXAMPLE_TIERS}")
    print(f"  as_of_date       = live(当前)")
    print("=" * 78)

    t0 = time.monotonic()
    result = _run_screen(EXAMPLE_SECTORS, EXAMPLE_TIERS, None)
    elapsed = time.monotonic() - t0
    passed, skipped, total = result["passed"], result["skipped"], result["total"]

    print()
    print("-" * 78)
    print(f"Total tickers processed : {total}")
    print(f"Total skipped / failed  : {len(skipped)}")
    print(f"Total passed the filter : {len(passed)}")
    print(f"Elapsed                 : {elapsed:.1f}s")
    print("-" * 78)

    if skipped:
        print("\nSkipped / failed (ticker -> reason):")
        for s in skipped:
            print(f"  {s['ticker']:<8} {s['reason']}")

    print("\nFiltered universe:")
    print(f"  {'TICKER':<8} {'SECTOR':<24} {'TIER':<7} {'MARKET CAP':>12}")
    print(f"  {'-' * 6:<8} {'-' * 22:<24} {'-' * 5:<7} {'-' * 12:>12}")
    for entry in passed:
        print(
            f"  {entry['ticker']:<8} {entry['gics_sector']:<24} "
            f"{entry['market_cap_tier']:<7} {_fmt_market_cap(entry['market_cap']):>12}"
        )
    print()
