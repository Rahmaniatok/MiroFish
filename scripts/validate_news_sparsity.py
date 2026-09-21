"""
SCRATCH validation script — Fase 9a bagian "0. Validasi Empiris Sebelum
Implementasi" (docs/design/fase9a_entity_extraction_schema.md).

WAJIB dijalankan SEBELUM menulis kode ekstraksi LLM (news_entity_extractor.py).
Tidak memanggil LLM sama sekali — murni cek substring/regex murah: dari sample
artikel nyata (Fase 8, get_news_data), berapa persen yang menyebut >=2 entitas
perusahaan berbeda (dari universe kecil di bawah) dalam headline+summary
gabungan. Angka ini menentukan apakah layak lanjut ke ekstraksi LLM penuh.

Not production code. Safe to delete after the validation report is recorded.

Usage:
  cd backend && ../scripts/validate_news_sparsity.py
  (atau) cd backend && python ../scripts/validate_news_sparsity.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.data_layer.news_data import get_news_data  # noqa: E402

# Universe kecil, campur besar/kecil, cukup untuk sample >=50 artikel real
# (mengikuti pola TICKERS di investigate_news.py Fase 8a, diperluas).
UNIVERSE = {
    "AAPL": ["Apple"],
    "MSFT": ["Microsoft"],
    "NVDA": ["Nvidia", "NVIDIA"],
    "GOOGL": ["Google", "Alphabet"],
    "AMZN": ["Amazon"],
    "JPM": ["JPMorgan", "JP Morgan"],
    "GS": ["Goldman Sachs"],
    "KO": ["Coca-Cola", "Coca Cola"],
    "WMT": ["Walmart"],
    "XOM": ["Exxon", "ExxonMobil"],
    "PFE": ["Pfizer"],
    "AVGO": ["Broadcom"],
}

_NAME_PATTERNS = {
    ticker: [re.compile(re.escape(alias), re.IGNORECASE) for alias in aliases]
    for ticker, aliases in UNIVERSE.items()
}
_TICKER_PATTERNS = {
    ticker: re.compile(r"\b" + re.escape(ticker) + r"\b")
    for ticker in UNIVERSE
}


def mentioned_tickers(text: str) -> set:
    found = set()
    for ticker in UNIVERSE:
        if _TICKER_PATTERNS[ticker].search(text):
            found.add(ticker)
            continue
        for pattern in _NAME_PATTERNS[ticker]:
            if pattern.search(text):
                found.add(ticker)
                break
    return found


def main():
    articles = []  # list of (source_ticker, article_id, combined_text)
    seen_ids = set()

    for ticker in UNIVERSE:
        result = get_news_data(ticker, as_of_date=None)  # live, 90 hari terakhir
        print(f"{ticker}: success={result.get('success')} "
              f"n_articles={len(result.get('articles') or [])}")
        if not result.get("success") or not result.get("articles"):
            continue
        for raw in result["articles"]:
            article_id = raw["article_id"]
            if article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            combined = (raw.get("headline") or "") + " " + (raw.get("summary") or "")
            articles.append((ticker, article_id, combined))

    print(f"\nTotal artikel unik terkumpul: {len(articles)}")
    if len(articles) < 50:
        print(
            f"PERINGATAN: hanya {len(articles)} artikel unik terkumpul, "
            f"kurang dari 50 yang diminta desain 9a — angka di bawah tetap "
            f"dilaporkan tapi sample-nya lebih kecil dari target."
        )

    multi_entity_count = 0
    examples = []
    for source_ticker, article_id, text in articles:
        hits = mentioned_tickers(text)
        if len(hits) >= 2:
            multi_entity_count += 1
            if len(examples) < 8:
                examples.append((article_id, source_ticker, sorted(hits), text[:140]))

    total = len(articles)
    pct = (100.0 * multi_entity_count / total) if total else 0.0

    print(f"\n{'=' * 70}")
    print(f"HASIL VALIDASI EMPIRIS (Fase 9a bagian 0)")
    print(f"{'=' * 70}")
    print(f"Total artikel unik disample : {total}")
    print(f"Menyebut >=2 entitas company : {multi_entity_count} ({pct:.1f}%)")
    print(f"Ambang keputusan (ilustratif): 10%")
    print(f"Status                       : {'LANJUT' if pct >= 10 else 'STOP — perlu keputusan bersama'}")

    if examples:
        print(f"\nContoh artikel dengan >=2 entitas company:")
        for article_id, source_ticker, hits, snippet in examples:
            print(f"  - id={article_id} (dari query {source_ticker}) entitas={hits}")
            print(f"    {snippet!r}")

    return pct


if __name__ == "__main__":
    main()
