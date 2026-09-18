"""
SCRATCH investigation script for Phase 8a (news data source comparison).
Not production code. Safe to delete after the investigation report is written.

Usage:
  cd backend && ../.venv-or-whatever/bin/python ../scripts/investigate_news.py yfinance
  cd backend && python ../scripts/investigate_news.py finnhub   # requires FINNHUB_API_KEY in .env
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

TICKERS = ["AAPL", "MSFT", "NVDA", "TSLA", "KO", "PFE", "AVGO"]  # mix large-cap + smaller/less-hyped

OUT_DIR = os.path.join(os.path.dirname(__file__), "_news_investigation_output")
os.makedirs(OUT_DIR, exist_ok=True)


def dump(name, obj):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    print(f"  wrote {path}")


def investigate_yfinance():
    import yfinance as yf

    print("=" * 70)
    print("YFINANCE INVESTIGATION")
    print("=" * 70)

    for ticker in TICKERS:
        print(f"\n--- {ticker} ---")
        t = yf.Ticker(ticker)
        try:
            news = t.get_news(count=50, tab="news")
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        print(f"  count returned: {len(news)}")
        if news:
            dates = []
            for item in news:
                content = item.get("content", {})
                pub = content.get("pubDate")
                dates.append(pub)
            dates_sorted = sorted(d for d in dates if d)
            if dates_sorted:
                print(f"  oldest pubDate in result: {dates_sorted[0]}")
                print(f"  newest pubDate in result: {dates_sorted[-1]}")
            fields = sorted(news[0].get("content", {}).keys())
            print(f"  top-level content fields: {fields}")
            dump(f"yfinance_{ticker}.json", news)
        time.sleep(1)  # be polite, watch for rate limiting


def investigate_finnhub():
    import requests
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        print("FINNHUB_API_KEY not set in .env — skipping Finnhub investigation.")
        print("Sign up free at https://finnhub.io/register, then add:")
        print("  FINNHUB_API_KEY=your_key_here")
        print("to your local .env (do not commit it).")
        return

    print("=" * 70)
    print("FINNHUB INVESTIGATION")
    print("=" * 70)

    today = datetime.now(timezone.utc).date()
    recent_from = today - timedelta(days=30)
    old_from = today - timedelta(days=365)
    old_to = old_from + timedelta(days=7)

    date_ranges = [
        ("recent_30d", recent_from, today),
        ("year_old_window", old_from, old_to),
    ]

    for ticker in TICKERS:
        print(f"\n--- {ticker} ---")
        for label, d_from, d_to in date_ranges:
            url = "https://finnhub.io/api/v1/company-news"
            params = {
                "symbol": ticker,
                "from": d_from.isoformat(),
                "to": d_to.isoformat(),
                "token": api_key,
            }
            try:
                resp = requests.get(url, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  [{label}] ERROR: {e}")
                continue
            print(f"  [{label}] {d_from}..{d_to} -> {len(data)} articles, status={resp.status_code}")
            if data:
                sample_fields = sorted(data[0].keys())
                print(f"    fields: {sample_fields}")
                dump(f"finnhub_{ticker}_{label}.json", data)
            # rate limit observation
            print(f"    headers of interest: "
                  f"X-Ratelimit-Limit={resp.headers.get('X-Ratelimit-Limit')} "
                  f"X-Ratelimit-Remaining={resp.headers.get('X-Ratelimit-Remaining')}")
            time.sleep(1.1)  # finnhub free tier ~60/min


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("yfinance", "both"):
        investigate_yfinance()
    if which in ("finnhub", "both"):
        investigate_finnhub()
