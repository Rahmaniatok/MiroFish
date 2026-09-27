"""Temporary script: fetch Finnhub company news for a fixed healthcare ticker list
(2026-01-01 .. 2026-03-31) and dump everything into one .txt file.

Usage (from repo root):
    python backend/scripts/tmp_fetch_finnhub_news.py
"""
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

API_KEY = os.getenv("FINNHUB_API_KEY")
if not API_KEY:
    sys.exit("FINNHUB_API_KEY not found in .env")

TICKERS = [
    "LLY", "JNJ", "MRK", "PFE", "ABBV", "BMY", "AMGN", "GILD", "VRTX", "REGN",
    "ABT", "MDT", "SYK", "BSX", "ISRG", "BDX", "UNH", "CVS", "CI", "ELV",
    "HUM", "TMO", "DHR", "IDXX",
]
START = date(2026, 1, 1)
END = date(2026, 3, 31)
# Finnhub caps results per request, so query in weekly windows to avoid truncation.
CHUNK_DAYS = 7
# Free tier: 60 calls/minute.
SLEEP_SEC = 1.1

OUT_PATH = Path(__file__).resolve().parent / "finnhub_news_2026Q1.txt"
URL = "https://finnhub.io/api/v1/company-news"


def fetch_window(symbol: str, start: date, end: date) -> list[dict]:
    params = {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat(), "token": API_KEY}
    for attempt in range(5):
        resp = requests.get(URL, params=params, timeout=30)
        if resp.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json() or []
    raise RuntimeError(f"Rate limited too many times for {symbol} {start}..{end}")


def fetch_symbol(symbol: str) -> list[dict]:
    seen: dict = {}
    cur = START
    while cur <= END:
        win_end = min(cur + timedelta(days=CHUNK_DAYS - 1), END)
        for item in fetch_window(symbol, cur, win_end):
            key = item.get("id") or item.get("url")
            seen[key] = item
        time.sleep(SLEEP_SEC)
        cur = win_end + timedelta(days=1)
    return sorted(seen.values(), key=lambda x: x.get("datetime", 0))


def fmt_item(item: dict) -> str:
    ts = datetime.fromtimestamp(item.get("datetime", 0), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"[{ts}] {item.get('headline', '').strip()}\n"
        f"Source: {item.get('source', '')} | Category: {item.get('category', '')} | Related: {item.get('related', '')}\n"
        f"Summary: {(item.get('summary') or '').strip()}\n"
        f"URL: {item.get('url', '')}\n"
    )


def main() -> None:
    total = 0
    with OUT_PATH.open("w", encoding="utf-8") as f:
        f.write(f"Finnhub company news {START} .. {END}\n")
        f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Tickers: {', '.join(TICKERS)}\n\n")
        for symbol in TICKERS:
            items = fetch_symbol(symbol)
            total += len(items)
            print(f"{symbol}: {len(items)} articles", flush=True)
            f.write("=" * 80 + f"\n{symbol} ({len(items)} articles)\n" + "=" * 80 + "\n\n")
            for item in items:
                f.write(fmt_item(item) + "\n")
            f.flush()
    print(f"Done: {total} articles -> {OUT_PATH}")


if __name__ == "__main__":
    main()
