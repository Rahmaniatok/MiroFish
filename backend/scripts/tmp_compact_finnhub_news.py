"""Temporary script: shrink finnhub_news_2026Q1.txt into a compact file small
enough for Zep graph ingestion.

Steps:
  1. Parse the raw dump written by tmp_fetch_finnhub_news.py.
  2. Keep only articles whose headline/summary actually mention the ticker or
     company name (drops e.g. Novo Nordisk stories filed under LLY).
  3. Drop duplicate headlines (same story is often filed under several tickers).
  4. Keep at most MAX_PER_TICKER articles per ticker, spread evenly over the quarter.
  5. Write one compact line per article: date | ticker | headline — summary.

Usage (from repo root):
    python backend/scripts/tmp_compact_finnhub_news.py
"""
import re
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
IN_PATH = HERE / "finnhub_news_2026Q1.txt"
OUT_PATH = HERE / "finnhub_news_2026Q1_compact.txt"

MAX_PER_TICKER = 30
MAX_SUMMARY_CHARS = 280

# Name variants used to check that an article is really about the company.
COMPANY_NAMES = {
    "LLY": ["Eli Lilly", "Lilly", "Mounjaro", "Zepbound", "orforglipron"],
    "JNJ": ["Johnson & Johnson", "J&J", "Johnson and Johnson"],
    "MRK": ["Merck", "Keytruda"],
    "PFE": ["Pfizer"],
    "ABBV": ["AbbVie", "Skyrizi", "Rinvoq", "Humira"],
    "BMY": ["Bristol Myers", "Bristol-Myers", "BMS"],
    "AMGN": ["Amgen"],
    "GILD": ["Gilead"],
    "VRTX": ["Vertex"],
    "REGN": ["Regeneron", "Dupixent"],
    "ABT": ["Abbott"],
    "MDT": ["Medtronic"],
    "SYK": ["Stryker"],
    "BSX": ["Boston Scientific"],
    "ISRG": ["Intuitive Surgical", "Intuitive", "da Vinci"],
    "BDX": ["Becton", "BD "],
    "UNH": ["UnitedHealth", "Optum", "United Health"],
    "CVS": ["CVS", "Aetna"],
    "CI": ["Cigna", "Evernorth", "Express Scripts"],
    "ELV": ["Elevance", "Anthem"],
    "HUM": ["Humana"],
    "TMO": ["Thermo Fisher"],
    "DHR": ["Danaher"],
    "IDXX": ["IDEXX", "Idexx"],
}

SECTION_RE = re.compile(r"^([A-Z]+) \(\d+ articles\)$")
ITEM_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) \d{2}:\d{2} UTC\] (.*)$")


def parse(path: Path) -> dict[str, list[dict]]:
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    ticker = None
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = SECTION_RE.match(line)
        if m:
            ticker = m.group(1)
            continue
        m = ITEM_RE.match(line)
        if m and ticker:
            current = {"date": m.group(1), "headline": m.group(2).strip(), "summary": ""}
            by_ticker[ticker].append(current)
        elif line.startswith("Summary: ") and current is not None:
            current["summary"] = line[len("Summary: "):].strip()
    return by_ticker


def is_relevant(ticker: str, item: dict) -> bool:
    text = f"{item['headline']} {item['summary']}"
    if re.search(rf"\b{ticker}\b", text):
        return True
    return any(name.lower() in text.lower() for name in COMPANY_NAMES[ticker])


def normalize(headline: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", headline.lower()).strip()


def spread(items: list[dict], k: int) -> list[dict]:
    """Pick k items evenly spaced in time (items are already date-sorted)."""
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def main() -> None:
    by_ticker = parse(IN_PATH)
    seen_headlines: set[str] = set()
    lines: list[str] = []
    stats = []
    for ticker, items in by_ticker.items():
        relevant = []
        for item in items:
            key = normalize(item["headline"])
            if not key or key in seen_headlines or not is_relevant(ticker, item):
                continue
            seen_headlines.add(key)
            relevant.append(item)
        picked = spread(relevant, MAX_PER_TICKER)
        stats.append((ticker, len(items), len(relevant), len(picked)))
        for item in picked:
            summary = item["summary"]
            if len(summary) > MAX_SUMMARY_CHARS:
                summary = summary[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
            line = f"{item['date']} | {ticker} | {item['headline']}"
            if summary:
                line += f" — {summary}"
            lines.append(line)

    header = (
        "Healthcare stock news, Q1 2026 (2026-01-01 .. 2026-03-31), source: Finnhub.\n"
        "Format: date | ticker | headline — summary\n\n"
    )
    OUT_PATH.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")

    for ticker, raw, rel, picked in stats:
        print(f"{ticker:5} raw={raw:5} relevant={rel:5} kept={picked}")
    size = OUT_PATH.stat().st_size
    print(f"Done: {len(lines)} articles, {size:,} bytes -> {OUT_PATH}")


if __name__ == "__main__":
    main()
