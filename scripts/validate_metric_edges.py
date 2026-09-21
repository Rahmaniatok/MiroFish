"""
SCRATCH validation script — Fase 9a (baru) bagian "Langkah 0" (validasi cepat
sebelum menulis metric_edge_builder.py).

Beda dari validasi Fase 9 versi berita (validate_news_sparsity.py): di sini
komputasinya deterministik (bukan LLM), jadi yang divalidasi BUKAN "apakah
datanya cukup kaya" tapi "apakah jumlah edge yang keluar dari implementasi
cocok order-of-magnitude dengan estimasi docs/design/fase9a_metric_based_edges.md",
dan "apakah ada kasus tepi (std=0) yang belum tertangani".

Fungsi-fungsi inti di sini akan disalin (dengan penyesuaian minor) ke
backend/app/services/metric_edge_builder.py setelah divalidasi.

Not production code. Safe to delete after the validation report is recorded.

Usage:
  cd backend && python ../scripts/validate_metric_edges.py
"""
import math
import os
import sys
from collections import defaultdict
from itertools import combinations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.data_layer.market_data import get_fundamental_data  # noqa: E402
from app.services.portfolio_optimizer import build_returns_matrix  # noqa: E402

AS_OF_DATE = None  # live — cukup untuk validasi order-of-magnitude

# Kandidat ticker nyata S&P 500, sengaja dipilih dari beberapa sub-industry
# yang diperkirakan besar (semiconductors, software, banks, biotech, REIT) —
# grouping AKTUAL ditentukan empiris dari field `industry` yfinance di bawah,
# bukan diasumsikan dari nama sub-industry.
CANDIDATES = [
    # semiconductors / semi equipment
    "NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "MU", "ADI", "MCHP",
    "MRVL", "ON", "SWKS", "QRVO", "MPWR", "TER", "NXPI", "LRCX", "AMAT",
    "KLAC", "ENTG", "MKSI",
    # software
    "MSFT", "ORCL", "CRM", "ADBE", "INTU", "NOW", "SNPS", "CDNS", "WDAY",
    "PANW", "FTNT", "ADSK", "ANSS", "PTC", "ROP",
    # banks
    "JPM", "BAC", "WFC", "C", "USB", "PNC", "TFC", "MTB", "FITB", "HBAN",
    "RF", "KEY", "CFG", "ZION",
    # biotech / pharma
    "AMGN", "GILD", "VRTX", "REGN", "BIIB", "MRNA",
]

_METRICS = [
    "pe_ratio", "pb_ratio", "market_cap", "revenue_growth_yoy",
    "profit_margin", "roe", "debt_to_equity", "eps_growth", "dividend_yield",
]
_MIN_OVERLAP = 3
_TOP_K = 5


def fetch_all_fundamentals(tickers):
    out = {}
    for t in tickers:
        result = get_fundamental_data(t, as_of_date=AS_OF_DATE)
        if result.get("success"):
            out[t] = result
        else:
            print(f"  {t}: fundamental fetch gagal ({result.get('error')}) — dilewati")
    return out


def largest_industry_group(fundamentals):
    groups = defaultdict(list)
    for ticker, f in fundamentals.items():
        industry = f.get("industry")
        if industry:
            groups[industry].append(ticker)
    for industry, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  industry={industry!r}: {len(members)} ticker(s) -> {members}")
    return max(groups.items(), key=lambda kv: len(kv[1]))


def pearson_correlation_matrix(tickers):
    returns = build_returns_matrix(list(tickers), as_of_date=AS_OF_DATE, lookback="1y")
    corr = returns.corr()
    return corr, list(returns.columns)


def topk_union_edges(pair_value, tickers, top_k, key_fn):
    """pair_value: dict[(a,b) sorted tuple] -> value. Returns dict edge_key -> {rank_a, rank_b, value}."""
    # per-ticker ranked list of (peer, value)
    per_ticker_ranked = {}
    for t in tickers:
        peers = []
        for other in tickers:
            if other == t:
                continue
            k = tuple(sorted((t, other)))
            if k in pair_value:
                peers.append((other, pair_value[k]))
        peers.sort(key=lambda kv: key_fn(kv[1]), reverse=True)
        per_ticker_ranked[t] = peers[:top_k]

    edges = {}
    for t, ranked in per_ticker_ranked.items():
        for rank, (peer, value) in enumerate(ranked, start=1):
            k = tuple(sorted((t, peer)))
            if k not in edges:
                edges[k] = {"value": value, "rank_a": None, "rank_b": None}
            a, b = k
            if t == a:
                edges[k]["rank_a"] = rank
            else:
                edges[k]["rank_b"] = rank
    return edges


def compute_correlation_edges(tickers):
    corr, cols = pearson_correlation_matrix(tickers)
    dropped = [t for t in tickers if t not in cols]
    if dropped:
        print(f"  ticker di-drop dari build_returns_matrix (riwayat kurang): {dropped}")

    pair_value = {}
    for a, b in combinations(cols, 2):
        pair_value[tuple(sorted((a, b)))] = float(corr.loc[a, b])

    edges = topk_union_edges(pair_value, cols, _TOP_K, key_fn=abs)
    return edges, corr, cols, dropped


def zscore_fundamentals(fundamentals, tickers):
    """Returns {metric: {ticker: zscore}}, std0_metrics: list of metrics with std==0."""
    raw = {m: {} for m in _METRICS}
    for t in tickers:
        f = fundamentals.get(t)
        if not f:
            continue
        for m in _METRICS:
            v = f.get(m)
            if v is None:
                continue
            if m == "market_cap":
                if v <= 0:
                    continue
                v = math.log10(v)
            raw[m][t] = v

    z = {m: {} for m in _METRICS}
    std0_metrics = []
    for m in _METRICS:
        values = list(raw[m].values())
        if len(values) < 2:
            continue
        mean = sum(values) / len(values)
        var = sum((x - mean) ** 2 for x in values) / len(values)
        std = math.sqrt(var)
        if std == 0.0:
            std0_metrics.append(m)
            # GUARD (Langkah 0 keputusan): metrik std=0 di populasi run ini
            # tidak informatif untuk membedakan siapa pun -> di-skip TOTAL
            # dari z-score run ini (bukan division-by-zero, bukan error).
            continue
        for t, v in raw[m].items():
            zc = (v - mean) / std
            zc = max(-3.0, min(3.0, zc))  # winsorize
            z[m][t] = zc
    return z, std0_metrics


def rms_distance(z, a, b):
    overlap = [m for m in _METRICS if a in z[m] and b in z[m]]
    if len(overlap) < _MIN_OVERLAP:
        return None, overlap
    sq = sum((z[m][a] - z[m][b]) ** 2 for m in overlap)
    return math.sqrt(sq / len(overlap)), overlap


def compute_fundamental_edges(fundamentals, tickers):
    z, std0_metrics = zscore_fundamentals(fundamentals, tickers)
    pair_value = {}
    pair_metrics = {}
    skipped = []
    for a, b in combinations(tickers, 2):
        dist, overlap = rms_distance(z, a, b)
        if dist is None:
            skipped.append((a, b, len(overlap)))
            continue
        score = 1.0 / (1.0 + dist)
        k = tuple(sorted((a, b)))
        pair_value[k] = score
        pair_metrics[k] = overlap

    edges = topk_union_edges(pair_value, tickers, _TOP_K, key_fn=lambda v: v)
    for k in edges:
        edges[k]["metrics_compared"] = pair_metrics[k]
    return edges, std0_metrics, skipped


def compute_sector_edges(group_tickers, corr_edges_full_pairvalue, tickers_in_group):
    # reuse |correlation_value| ranking within the group only
    pair_value = {}
    for a, b in combinations(tickers_in_group, 2):
        k = tuple(sorted((a, b)))
        if k in corr_edges_full_pairvalue:
            pair_value[k] = corr_edges_full_pairvalue[k]
    edges = topk_union_edges(pair_value, tickers_in_group, _TOP_K, key_fn=abs)
    return edges


def main():
    print("=" * 70)
    print("LANGKAH 0.1 — fetch fundamental untuk kandidat, cari sub-industry terbesar")
    print("=" * 70)
    fundamentals = fetch_all_fundamentals(CANDIDATES)
    print(f"\nTotal fundamental sukses: {len(fundamentals)}/{len(CANDIDATES)}\n")

    industry, members = largest_industry_group(fundamentals)
    print(f"\n>>> Grup TERBESAR nyata: industry={industry!r}, {len(members)} ticker: {members}\n")

    if len(members) < 15:
        print(
            f"PERINGATAN: grup terbesar cuma {len(members)} ticker, di bawah target "
            f">=15 yang diminta task. Lanjut tetap dengan grup ini, tapi order-of-"
            f"magnitude comparison di bawah proporsional utk ukuran ini, bukan 72."
        )

    print("=" * 70)
    print("LANGKAH 0.2 — korelasi harga (Pearson, 1y, top-5 union, |corr|)")
    print("=" * 70)
    corr_edges, corr_matrix, cols, dropped = compute_correlation_edges(members)
    pair_value_corr = {
        tuple(sorted((a, b))): float(corr_matrix.loc[a, b])
        for a, b in combinations(cols, 2)
    }
    print(f"n_correlation_edges = {len(corr_edges)} (dari {len(cols)} ticker berhasil dpt harga)")
    neg_edges = [(k, v) for k, v in corr_edges.items() if v["value"] < 0]
    print(f"  edge dengan correlation_value NEGATIF yang tetap masuk top-K: {len(neg_edges)}")
    for k, v in list(corr_edges.items())[:5]:
        print(f"    {k}: corr={v['value']:.3f} rank_a={v['rank_a']} rank_b={v['rank_b']}")

    print("\n" + "=" * 70)
    print("LANGKAH 0.3 — kemiripan fundamental (z-score+winsorize+RMS, top-5 union)")
    print("=" * 70)
    fund_edges, std0_metrics, skipped_pairs = compute_fundamental_edges(fundamentals, members)
    print(f"n_fundamental_edges = {len(fund_edges)}")
    print(f"metrik dengan std=0 di populasi grup ini: {std0_metrics or '(tidak ada)'}")
    print(f"pasangan di-skip (overlap < {_MIN_OVERLAP}): {len(skipped_pairs)}")
    for a, b, n in skipped_pairs[:5]:
        print(f"    {a}-{b}: overlap={n} metrics < minimum {_MIN_OVERLAP}")
    for k, v in list(fund_edges.items())[:5]:
        print(f"    {k}: score={v['value']:.3f} metrics={v['metrics_compared']}")

    print("\n" + "=" * 70)
    print("LANGKAH 0.4 — sektor (reuse korelasi, top-5 dalam grup)")
    print("=" * 70)
    sector_edges = compute_sector_edges(members, pair_value_corr, members)
    print(f"n_sector_edges = {len(sector_edges)} (grup size={len(members)})")
    full_pairwise = len(members) * (len(members) - 1) // 2
    print(f"full pairwise TANPA cap untuk grup ini = C({len(members)},2) = {full_pairwise}")
    if full_pairwise:
        reduction = 100 * (1 - len(sector_edges) / full_pairwise)
        print(f"reduksi vs full pairwise = {reduction:.1f}%")

    lo = _TOP_K * len(members) // 2
    hi = _TOP_K * len(members)
    print(f"ekspektasi dokumen (rentang K*N/2 .. K*N) untuk N={len(members)}: {lo} .. {hi}")
    in_range = lo <= len(sector_edges) <= hi
    print(f"n_sector_edges dalam rentang ekspektasi? {in_range}")

    print("\n" + "=" * 70)
    print("RINGKASAN LANGKAH 0")
    print("=" * 70)
    print(f"Grup dites            : industry={industry!r}, N={len(members)}")
    print(f"price_correlated      : {len(corr_edges)} edge (termasuk {len(neg_edges)} korelasi negatif)")
    print(f"fundamentally_similar : {len(fund_edges)} edge ({len(skipped_pairs)} pasangan di-skip overlap<3)")
    print(f"same_sector           : {len(sector_edges)} edge (vs {full_pairwise} full-pairwise tanpa cap)")
    print(f"std=0 metrics ditemukan: {std0_metrics or 'TIDAK ADA'}")
    print(f"Sector edges dalam rentang ekspektasi dokumen (K*N/2..K*N)? {in_range}")


if __name__ == "__main__":
    main()
