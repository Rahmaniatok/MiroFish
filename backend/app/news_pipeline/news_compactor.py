"""
Rule-based news compaction (no LLM) — turns thousands of raw Finnhub
articles into a small txt_berita that MiroFish can ingest as a reality seed.

Generalises backend/scripts/tmp_compact_finnhub_news.py (hand-written name
lists for 24 healthcare tickers) to any ticker_universe:

  1. relevance  — the headline/summary must actually mention the ticker or
                  the company (name variants derived from the S&P 500 name);
                  Finnhub files plenty of peer/market stories under a symbol
  2. noise      — drop low-information patterns (13F position filings,
                  "stock outperforms competitors on the day", listicles…)
  3. dedupe     — same headline filed twice under one ticker
  4. score      — importance by event type (earnings, FDA, M&A, analyst
                  action, legal, capital return, leadership…) + small bonuses
  5. select     — at most `cap` per ticker: first the best article of every
                  week (so the whole 90 days is covered), then fill by score
  6. render     — one self-contained line per article (date | ticker |
                  headline — summary) grouped per ticker; stories picked for
                  several tickers are written once with all tickers.

Every raw article gets a decision (kept / dropped + reason) so the UI can
show exactly why something is or isn't in the txt.
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

MAX_SUMMARY_CHARS = 280
# ~750 articles ≈ 250 KB of txt, the size of the healthcare trial run that
# built a healthy graph (24 tickers x 30 -> 723 lines, 191 KB, 441 entities)
TARGET_TOTAL_ARTICLES = 750
MIN_CAP, MAX_CAP = 8, 30
DIVERSITY_PENALTY = 0.25   # score cost per already-kept article sharing a topic tag

KEPT = "kept"
DROP_IRRELEVANT = "not_about_company"
DROP_NOISE = "low_value"
DROP_DUPLICATE = "duplicate"
DROP_CAP = "over_cap"

DROP_LABELS = {
    DROP_IRRELEVANT: "Not about the company",
    DROP_NOISE: "Low-value item",
    DROP_DUPLICATE: "Duplicate headline",
    DROP_CAP: "Below the per-ticker cap",
}

# (tag, weight, pattern) — first match per tag counts once
EVENT_RULES: List[Tuple[str, float, re.Pattern]] = [
    ("EARNINGS", 3.0, re.compile(r"\b(earnings|quarterly results|q[1-4] (results|revenue|sales|profit)|eps|revenue|guidance|outlook|forecast|beats?|miss(es|ed)?|profit warning)\b", re.I)),
    ("M&A", 3.0, re.compile(r"\b(acquir\w*|acquisition|merger|merge|buyout|takeover|deal to buy|spin[- ]?off|divest\w*)\b", re.I)),
    ("REGULATORY", 3.0, re.compile(r"\b(fda|approv\w*|clearance|phase (1|2|3|i{1,3})|trial|ema|regulator\w*|antitrust|ftc|doj|sec (probe|charges|filing))\b", re.I)),
    ("LEGAL", 2.5, re.compile(r"\b(lawsuit|sued|sues|settle\w*|probe|investigation|recall|fine[ds]?|penalt\w*|indict\w*|class action)\b", re.I)),
    ("ANALYST", 2.0, re.compile(r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiat\w* coverage|rating|overweight|underweight|outperform rating|buy rating|sell rating)\b", re.I)),
    ("CAPITAL", 2.0, re.compile(r"\b(dividend|buyback|repurchase|share offering|debt offering|bond sale|capital return)\b", re.I)),
    ("LEADERSHIP", 2.0, re.compile(r"\b(ceo|cfo|chief executive|resign\w*|steps? down|appoint\w*|layoffs?|job cuts|restructur\w*)\b", re.I)),
    ("STRATEGY", 1.5, re.compile(r"\b(partnership|contract|launch\w*|expan\w*|invest\w* \$|plant|factory|capex|ai\b|data center|pricing)\b", re.I)),
    ("MACRO", 1.0, re.compile(r"\b(tariff\w*|sanction\w*|interest rate|fed\b|inflation|recession|oil price|opec)\b", re.I)),
]

NOISE_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\b(shares|stake|position|holdings?) (in|of) .+ (sold|bought|acquired|trimmed|raised|lowered|boosted|increased|decreased)\b",
        r"\b(sells|buys|acquires|trims|raises|lowers|boosts|increases|decreases) (its )?(stake|position|holdings?|shares)\b",
        r"\b(llc|advisors|management|capital|partners|wealth|securities|trust)\b.{0,40}\b(stake|position|holdings?|shares)\b",
        r"outperforms competitors|underperforms competitors|stock (rises|falls|sinks|rallies|climbs|drops) .*(on the day|monday|tuesday|wednesday|thursday|friday)",
        r"^is the market (bullish|bearish)",
        r"\bunusual options activity\b|\boptions? (trading|activity) (for|in)\b|\bshort interest\b",
        r"^(top|best) \d+\b|\bstocks? to (watch|buy)\b|\bshould you buy\b|\bbuy the dip\b",
        r"\bpremarket (movers|mover)\b|\bmarket (wrap|open|close)\b|\bmovers\b",
        r"^\$?[A-Z]{1,5} (stock|shares)? ?(price|quote)$",
    )
]

_NAME_SUFFIXES = re.compile(
    r"[,.]?\s*\b(incorporated|inc|corp|corporation|company|co|plc|ltd|limited|holdings?|group|"
    r"s\.?a|n\.?v|ag|l\.?p|trust|the)\b\.?", re.I)
_GENERIC_FIRST_WORDS = {
    "american", "general", "first", "united", "international", "global", "southern", "public",
    "national", "digital", "applied", "advanced", "energy", "texas", "western", "eastern",
    "northern", "consolidated", "universal", "federal", "republic", "principal", "state",
    "capital", "cardinal", "regency", "entergy", "edison", "dominion", "church", "live",
    "match", "block", "target", "ball", "best", "news", "fox", "gap", "the",
    "home", "health", "bank", "financial", "services", "systems", "technologies", "technology",
    "communications", "resources", "industries", "power", "electric", "water", "gas", "oil",
    "data", "software", "properties", "realty", "foods", "brands", "entertainment", "insurance",
    "life", "pharmaceuticals", "therapeutics", "medical", "laboratories", "labs", "solutions",
    "networks", "america", "new", "one", "south", "north", "west", "east", "central", "pacific",
    "atlantic", "mutual", "motors", "airlines", "semiconductor", "devices", "instruments",
    "platforms", "partners", "holdings", "group", "chemical", "chemicals", "materials", "tech",
}
# tickers that are also common words: only count "$ON", "(ON)", "NYSE: ON" forms
_AMBIGUOUS_TICKERS = {
    "A", "ALL", "ARE", "BALL", "BEN", "BK", "C", "CAT", "D", "DAY", "DG", "EL", "F", "FAST",
    "HAS", "HD", "IT", "J", "K", "KEY", "L", "LOW", "MA", "MO", "NOW", "O", "ON", "PEG", "PH",
    "PM", "SO", "T", "TT", "V", "WELL", "WAT", "ES", "ED", "EW", "GE", "GL", "IP", "LH", "MS",
    "NI", "PKG", "RL", "SNA", "TAP", "TEL", "WM", "AI", "CE", "DE", "HON", "ICE", "IR",
}


def auto_cap(n_tickers: int) -> int:
    if n_tickers <= 0:
        return MAX_CAP
    return max(MIN_CAP, min(MAX_CAP, TARGET_TOTAL_ARTICLES // n_tickers))


def name_variants(company_name: str) -> List[str]:
    """'Eli Lilly and Company' -> ['Eli Lilly', 'Lilly']; 'Alphabet Inc. (Class A)' -> ['Alphabet']."""
    name = re.sub(r"\(.*?\)", "", company_name or "")
    name = _NAME_SUFFIXES.sub("", name)
    name = re.sub(r"\s+(and|&)\s*$", "", name.strip(" ,.&")).strip()
    variants = []
    if len(name) >= 2:
        variants.append(name)
    # single-word CamelCase brand: "ExxonMobil" -> "Exxon Mobil" (+ "Exxon" via
    # the first-word rule); multi-word names like "NextEra Energy" stay intact
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", name)
    if spaced != name and " " not in name:
        variants.append(spaced)
        name = spaced
    words = [w for w in re.split(r"\s+", name) if w]
    if len(words) > 1:
        first = words[0].strip(",.")
        if len(first) >= 4 and first.lower() not in _GENERIC_FIRST_WORDS:
            variants.append(first)
        last = words[-1].strip(",.")
        if len(words) == 2 and len(last) >= 5 and last.lower() not in _GENERIC_FIRST_WORDS:
            variants.append(last)  # "Eli Lilly" -> "Lilly"
    return list(dict.fromkeys(variants))


def _ticker_pattern(ticker: str) -> re.Pattern:
    t = re.escape(ticker)
    if ticker in _AMBIGUOUS_TICKERS or len(ticker) <= 1:
        return re.compile(rf"(\${t}\b|\({t}\)|\b(nyse|nasdaq|cboe)\s*:\s*{t}\b)", re.I)
    return re.compile(rf"(?<![A-Za-z]){t}(?![A-Za-z])")


def _normalize_headline(headline: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", headline.lower()).strip()


def _clean_summary(summary: Optional[str], headline: str) -> str:
    if not summary or summary.lower().startswith(("http://", "https://")):
        return ""
    if _normalize_headline(summary) == _normalize_headline(headline):
        return ""
    summary = re.sub(r"\s*(click (here|for)[^.]*\.?|read more[^.]*\.?)$", "", summary, flags=re.I).strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return summary


def score_article(article: Dict[str, Any], mentions_in_headline: bool) -> Tuple[float, List[str]]:
    text = f"{article['headline']} {article.get('summary') or ''}"
    score, tags = 0.0, []
    for tag, weight, pattern in EVENT_RULES:
        if pattern.search(text):
            score += weight
            tags.append(tag)
    if mentions_in_headline:
        score += 1.0
    if len(article.get("summary") or "") >= 80:
        score += 0.5
    return score, tags


def _week_index(published_at: str, window_start: date) -> int:
    return max(0, (date.fromisoformat(published_at[:10]) - window_start).days // 7)


def select_for_ticker(
    raw: Dict[str, Any],
    company_name: str,
    cap: int,
) -> List[Dict[str, Any]]:
    """Decision for every raw article of one ticker (newest first)."""
    ticker = raw["ticker"]
    window_start = date.fromisoformat(raw["window_start"])
    tpat = _ticker_pattern(ticker)
    names = [re.compile(rf"\b{re.escape(n)}\b", re.I) for n in name_variants(company_name)]

    decided: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    seen = set()
    for art in raw["articles"]:
        headline, summary = art["headline"], art.get("summary") or ""
        in_headline = bool(tpat.search(headline) or any(p.search(headline) for p in names))
        relevant = in_headline or bool(tpat.search(summary) or any(p.search(summary) for p in names))
        score, tags = score_article(art, in_headline)
        row = {
            **art,
            "ticker": ticker,
            "score": round(score, 2),
            "tags": tags,
            "week": _week_index(art["published_at"], window_start),
            "clean_summary": _clean_summary(art.get("summary"), headline),
        }
        key = _normalize_headline(headline)
        if not relevant:
            row["decision"] = DROP_IRRELEVANT
        elif any(p.search(headline) for p in NOISE_PATTERNS):
            row["decision"] = DROP_NOISE
        elif key in seen:
            row["decision"] = DROP_DUPLICATE
        else:
            seen.add(key)
            row["decision"] = None
            candidates.append(row)
        decided.append(row)

    # pass 1: best article of each week (coverage), pass 2: best remaining overall
    ranked = sorted(candidates, key=lambda r: (-r["score"], r["published_at"]))
    kept_ids = set()
    by_week: Dict[int, Dict[str, Any]] = {}
    for r in ranked:
        by_week.setdefault(r["week"], r)
    for r in sorted(by_week.values(), key=lambda r: -r["score"])[:cap]:
        kept_ids.add(r["article_id"])
    # fill greedily; each topic already kept costs a little, so 30 picks aren't
    # all earnings previews when legal / M&A / leadership news exists too
    tag_counts: Dict[str, int] = {}
    for r in candidates:
        if r["article_id"] in kept_ids:
            for t in r["tags"]:
                tag_counts[t] = tag_counts.get(t, 0) + 1
    remaining = [r for r in ranked if r["article_id"] not in kept_ids]
    while remaining and len(kept_ids) < cap:
        def adjusted(r):
            penalty = sum(tag_counts.get(t, 0) for t in r["tags"]) / max(len(r["tags"]), 1)
            return r["score"] - DIVERSITY_PENALTY * penalty
        best = max(remaining, key=lambda r: (adjusted(r), r["published_at"]))
        remaining.remove(best)
        kept_ids.add(best["article_id"])
        for t in best["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    for r in candidates:
        r["decision"] = KEPT if r["article_id"] in kept_ids else DROP_CAP
    return decided


def ticker_stats(decided: List[Dict[str, Any]], weeks: int = 13) -> Dict[str, Any]:
    counts = {KEPT: 0, DROP_IRRELEVANT: 0, DROP_NOISE: 0, DROP_DUPLICATE: 0, DROP_CAP: 0}
    raw_by_week = [0] * weeks
    kept_by_week = [0] * weeks
    tags: Dict[str, int] = {}
    for r in decided:
        counts[r["decision"]] += 1
        w = min(r["week"], weeks - 1)
        raw_by_week[w] += 1
        if r["decision"] == KEPT:
            kept_by_week[w] += 1
            for t in r["tags"]:
                tags[t] = tags.get(t, 0) + 1
    return {
        "raw": len(decided),
        "relevant": len(decided) - counts[DROP_IRRELEVANT],
        "kept": counts[KEPT],
        "dropped": {k: v for k, v in counts.items() if k != KEPT},
        "raw_by_week": raw_by_week,
        "kept_by_week": kept_by_week,
        "kept_tags": tags,
    }


def render_txt(
    universe: Dict[str, Any],
    selections: Dict[str, List[Dict[str, Any]]],
    cap: int,
) -> str:
    """txt_berita: header + per-ticker sections of self-contained lines."""
    rows_by_ticker = {r["ticker"]: r for r in universe["universe"]}
    # merge the same story kept for several tickers
    owners: Dict[str, List[str]] = {}
    for ticker in universe["ticker_universe"]:
        for r in selections.get(ticker, []):
            if r["decision"] == KEPT:
                owners.setdefault(_normalize_headline(r["headline"]), []).append(ticker)

    lines = [
        f"Stock news for {len(universe['ticker_universe'])} S&P 500 companies "
        f"({', '.join(universe['sectors'])}; {'/'.join(universe['market_cap_tiers'])} cap), "
        f"90 days up to {universe['as_of_date']}. Source: Finnhub company news.",
        "Only news published on or before the as-of date is included. "
        f"At most {cap} of the most important articles per company.",
        "Format: date | ticker(s) | headline — summary",
        "",
    ]
    written = set()
    for ticker in universe["ticker_universe"]:
        kept = sorted((r for r in selections.get(ticker, []) if r["decision"] == KEPT),
                      key=lambda r: r["published_at"])
        info = rows_by_ticker.get(ticker, {})
        lines.append(f"=== {ticker} — {info.get('company_name', ticker)} "
                     f"({info.get('gics_sector', '?')}, {info.get('market_cap_tier', '?')} cap) ===")
        if not kept:
            lines.append(f"(no relevant news for {ticker} in this window)")
        for r in kept:
            key = _normalize_headline(r["headline"])
            if key in written:
                continue
            written.add(key)
            tickers = ",".join(owners.get(key, [ticker]))
            line = f"{r['published_at'][:10]} | {tickers} | {r['headline']}"
            if r["clean_summary"]:
                line += f" — {r['clean_summary']}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
