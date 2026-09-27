"""
News data layer — Finnhub Company News, windowed so nothing is truncated.

Rewritten from konsesus-lllm's news_data.py (Fase 8c). Same article shape and
the same leakage guard, but that version fetched the whole 90-day window in
ONE request — verified 2026-09-27 that Finnhub silently caps a response at
~245-250 articles (newest first): AAPL for 2026-03-31..06-29 came back as 246
articles covering only the last 4 days, vs 3181 when fetched per week. Even a
7-day window hits the cap for mega caps, so windows that come back near the
cap are split in half and refetched, down to 1 day.

Free-tier limits (verified in Fase 8a): ~360 days of history counted back
from the day of the fetch, 60 requests/minute per key. Calls go through one
process-wide rate limiter.
"""

import html
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from ..utils.logger import get_logger
from .cache import get_cached, set_cache

logger = get_logger('mirofish.data_layer.news_data')

_ENV_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env')
load_dotenv(_ENV_PATH if os.path.exists(_ENV_PATH) else None, override=False)

FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
NEWS_WINDOW_DAYS = 90
FINNHUB_LIMIT_DAYS = 360

INITIAL_WINDOW_DAYS = 7
# Finnhub's per-response cap is ~245-250 and not exact (KMI 90d -> 245), so a
# window at/above this is treated as truncated and split.
SPLIT_THRESHOLD = 200
CALL_INTERVAL_SEC = 1.05          # 60 calls/minute free tier
_RETRY_BACKOFF_SEC = (10, 20, 40, 60)

_CACHE_TYPE = "news_windowed"
_SCHEMA_VERSION = 1


class NewsFetchError(RuntimeError):
    pass


class NewsFetchStopped(RuntimeError):
    """Raised when should_stop() turns true between calls (pause)."""


class _RateLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = time.monotonic() + self.interval


_limiter = _RateLimiter(CALL_INTERVAL_SEC)


def _api_key() -> str:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        raise NewsFetchError("FINNHUB_API_KEY is not set in .env")
    return key


def _request_window(ticker: str, start: date, end: date) -> List[Dict[str, Any]]:
    for attempt in range(len(_RETRY_BACKOFF_SEC) + 1):
        _limiter.wait()
        try:
            resp = requests.get(
                FINNHUB_NEWS_URL,
                params={"symbol": ticker, "from": start.isoformat(), "to": end.isoformat(),
                        "token": _api_key()},
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            if attempt < len(_RETRY_BACKOFF_SEC):
                time.sleep(_RETRY_BACKOFF_SEC[attempt])
                continue
            raise NewsFetchError(f"Finnhub request failed: {e}")
        if resp.status_code == 429 and attempt < len(_RETRY_BACKOFF_SEC):
            logger.warning(f"Finnhub 429 for {ticker}, backing off {_RETRY_BACKOFF_SEC[attempt]}s")
            time.sleep(_RETRY_BACKOFF_SEC[attempt])
            continue
        if resp.status_code != 200:
            raise NewsFetchError(f"Finnhub returned {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not isinstance(data, list):
            raise NewsFetchError(f"Unexpected Finnhub response: {str(data)[:200]}")
        return data
    raise NewsFetchError("Finnhub rate limit: too many retries")


_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = _TAG_RE.sub(" ", html.unescape(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _normalize(item: Dict[str, Any], ticker: str) -> Optional[Dict[str, Any]]:
    article_id, headline = item.get("id"), _clean_text(item.get("headline"))
    url, epoch = item.get("url"), item.get("datetime")
    if article_id is None or not headline or not url or not epoch:
        return None
    published = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    return {
        "article_id": int(article_id),
        "headline": headline,
        "summary": _clean_text(item.get("summary")),
        "publisher": _clean_text(item.get("source")),
        "category": _clean_text(item.get("category")),
        "url": url,
        "published_at": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "related_ticker": ticker,  # Finnhub's `related` just echoes the query
    }


def news_window(as_of_date: str) -> Tuple[date, date]:
    end = date.fromisoformat(as_of_date)
    return end - timedelta(days=NEWS_WINDOW_DAYS), end


def fetch_company_news(
    ticker: str,
    as_of_date: str,
    on_call: Optional[Callable[[Dict[str, Any]], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    All Finnhub company news for [as_of - 90d, as_of], no truncation.

    on_call(info) fires after each HTTP call with {start, end, count, split}.
    should_stop() is checked before each call; when true -> NewsFetchStopped
    (nothing partial is returned, the ticker is simply refetched on resume).
    """
    ticker = ticker.strip().upper()
    window_start, window_end = news_window(as_of_date)
    days_back = (datetime.now(timezone.utc).date() - window_end).days
    if days_back > FINNHUB_LIMIT_DAYS:
        raise NewsFetchError(f"as_of {as_of_date} is {days_back} days ago, beyond Finnhub's ~{FINNHUB_LIMIT_DAYS}")

    # stack of windows, oldest first so the log reads chronologically
    pending: List[Tuple[date, date]] = []
    cur = window_start
    while cur <= window_end:
        end = min(cur + timedelta(days=INITIAL_WINDOW_DAYS - 1), window_end)
        pending.append((cur, end))
        cur = end + timedelta(days=1)
    pending.reverse()

    by_id: Dict[int, Dict[str, Any]] = {}
    calls, truncated_days = 0, []
    while pending:
        if should_stop and should_stop():
            raise NewsFetchStopped(ticker)
        start, end = pending.pop()
        raw = _request_window(ticker, start, end)
        calls += 1
        span = (end - start).days + 1
        split = len(raw) >= SPLIT_THRESHOLD and span > 1
        if on_call:
            on_call({"start": start.isoformat(), "end": end.isoformat(), "count": len(raw), "split": split})
        if split:
            mid = start + timedelta(days=span // 2 - 1)
            pending.append((mid + timedelta(days=1), end))
            pending.append((start, mid))
            continue
        if len(raw) >= SPLIT_THRESHOLD:
            truncated_days.append(start.isoformat())
        for item in raw:
            art = _normalize(item, ticker)
            if art is None:
                continue
            # LEAKAGE GUARD: never trust `to` alone — drop anything after as_of
            day = date.fromisoformat(art["published_at"][:10])
            if day > window_end or day < window_start:
                continue
            by_id[art["article_id"]] = art

    articles = sorted(by_id.values(), key=lambda a: a["published_at"], reverse=True)
    return {
        "schema_version": _SCHEMA_VERSION,
        "ticker": ticker,
        "as_of_date": as_of_date,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "calls": calls,
        "truncated_days": truncated_days,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "articles": articles,
    }


def get_company_news(ticker: str, as_of_date: str, **kwargs: Any) -> Dict[str, Any]:
    """Cached fetch_company_news (historical as_of rows never expire, see cache.py)."""
    ticker = ticker.strip().upper()
    cached = get_cached(ticker, _CACHE_TYPE, as_of_date)
    if cached is not None and cached.get("schema_version") == _SCHEMA_VERSION:
        cached = dict(cached, from_cache=True)
        return cached
    result = fetch_company_news(ticker, as_of_date, **kwargs)
    set_cache(ticker, _CACHE_TYPE, result, as_of_date)
    return dict(result, from_cache=False)
