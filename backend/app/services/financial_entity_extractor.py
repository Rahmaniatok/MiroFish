"""
Phase 2a — Ekstraksi entitas FINANSIAL untuk tahap graph-building.

============================================================================
KENAPA FILE INI ADA / HUBUNGANNYA DENGAN EKSTRAKTOR ENTITAS ASLI MiroFish
============================================================================
Pipeline graph-building MiroFish yang asli meng-ekstrak entitas dari teks
berita / opini sosial:

  1. OntologyGenerator.generate()          -> mendefinisikan TIPE entitas
     (Person, Company, MediaOutlet, ...) dari teks + kebutuhan simulasi.
  2. GraphBuilderService.add_text_batches()-> mengirim potongan teks ke Zep,
     Zep meng-ekstrak INSTANCE entitas ke dalam graph.
  3. ZepEntityReader.filter_defined_entities(graph_id) -> membaca instance
     entitas itu KEMBALI keluar sebagai satu bundel
     `FilteredEntities(entities=[EntityNode, ...])`.
  4. Downstream — `oasis_profile_generator.generate_profiles_from_entities()`
     dan `simulation_config_generator._summarize_entities()` — meng-konsumsi
     `List[EntityNode]` lewat `.get_entity_type()`, `.name`, `.summary`,
     `.uuid`, `.attributes`.

Langkah (3) adalah "ekstraktor entitas" dalam pipeline: fungsi yang
menghasilkan daftar entitas berskema `EntityNode` untuk dikonsumsi tahap
berikutnya. Modul INI meng-adaptasi peran tersebut, TANPA menulis ulang:

  - KONTRAK OUTPUT dipertahankan persis — daftar entitas berskema
    `EntityNode` (uuid / name / labels / summary / attributes /
    related_edges / related_nodes). Tipe entitas tetap dikodekan lewat
    `labels` (label pertama = tipe spesifik, lalu "Entity"), sama seperti
    entitas hasil Zep, sehingga `EntityNode.get_entity_type()` dan semua
    konsumen downstream tetap bekerja tanpa perubahan.
  - Yang BERUBAH:
      * INPUT  : sebuah dict `get_stock_context(ticker, as_of_date)` dari
                 data layer Phase 1 — bukan teks / graph Zep.
      * Apa yang dihitung sebagai ENTITAS: fakta finansial tentang satu
                 saham — bukan aktor sosial / opini.

Konstruksi RELASI / EDGE antar entitas SENGAJA tidak dilakukan di sini —
itu Phase 2c. `related_edges` / `related_nodes` selalu dikembalikan kosong.

Persona generation, simulasi, dan report generation TIDAK disentuh modul ini.
============================================================================
"""

import re
from typing import Any, Dict, List, Optional

from ..utils.logger import get_logger
from .zep_entity_reader import EntityNode, FilteredEntities

logger = get_logger('mirofish.financial_entity_extractor')


# ---------------------------------------------------------------------------
# Tipe entitas finansial (dikodekan lewat EntityNode.labels[0], PascalCase
# singular — konsisten dengan tipe sosial asli: Person, Company, MediaOutlet)
# ---------------------------------------------------------------------------
ENTITY_TYPE_COMPANY = "Company"
ENTITY_TYPE_SECTOR = "Sector"
ENTITY_TYPE_VALUATION_METRIC = "ValuationMetric"
ENTITY_TYPE_FUNDAMENTAL_METRIC = "FundamentalMetric"
ENTITY_TYPE_TECHNICAL_SIGNAL = "TechnicalSignal"
# Phase 3d: the AAOIFI SS-21 compliance screen (Phase 1g) folded into the seed
# graph as a first-class entity, so the Sharia compliance persona ("Gamma") can
# read its verdict from the entity list like every other persona reads metrics.
ENTITY_TYPE_SHARIA_SCREEN = "ShariaScreen"

FINANCIAL_ENTITY_TYPES = (
    ENTITY_TYPE_COMPANY,
    ENTITY_TYPE_SECTOR,
    ENTITY_TYPE_VALUATION_METRIC,
    ENTITY_TYPE_FUNDAMENTAL_METRIC,
    ENTITY_TYPE_TECHNICAL_SIGNAL,
    ENTITY_TYPE_SHARIA_SCREEN,
)

# (key di dict fundamental, nama tampilan, unit) — masing-masing jadi SATU
# entity valuation_metric yang berbeda, dengan value-nya menempel.
_VALUATION_METRIC_SPECS = (
    ("pe_ratio", "P/E", "ratio"),
    ("pb_ratio", "P/B", "ratio"),
    ("market_cap", "Market Cap", "USD"),
)

# (nama tampilan, unit, tuple key kandidat di dict fundamental).
# Data layer Phase 1c mengisi revenue_growth_yoy & profit_margin; Phase 1f
# menambah roe / debt_to_equity / eps_growth / dividend_yield. Key yang belum
# ada di sumber data tidak akan ketemu -> entitasnya dilewati (di-log), BUKAN
# dibuat dengan nilai null / dikarang.
_FUNDAMENTAL_METRIC_SPECS = (
    ("Revenue Growth (YoY)", "fraction", ("revenue_growth_yoy",)),
    ("Profit Margin", "fraction", ("profit_margin",)),
    ("Return on Equity", "fraction", ("return_on_equity", "roe", "returnOnEquity")),
    ("Debt/Equity", "ratio", ("debt_to_equity", "debt_equity", "debtToEquity")),
    ("EPS Growth (YoY)", "fraction", ("eps_growth", "earningsGrowth")),
    ("Dividend Yield", "fraction", ("dividend_yield", "trailingAnnualDividendYield")),
)


def _slug(text: str) -> str:
    """'Consumer Electronics' -> 'consumer_electronics' (untuk bikin uuid stabil)."""
    return re.sub(r'[^a-z0-9]+', '_', str(text).lower()).strip('_') or "x"


def _is_missing(value: Any) -> bool:
    """None atau NaN dianggap 'tidak ada'."""
    return value is None or (isinstance(value, float) and value != value)


def _fmt_pct(fraction: Optional[float]) -> str:
    if _is_missing(fraction):
        return "n/a"
    return f"{fraction * 100:.2f}%"


def _fmt_money(value: Optional[float]) -> str:
    if _is_missing(value):
        return "n/a"
    value = float(value)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= divisor:
            return f"${value / divisor:.2f}{suffix}"
    return f"${value:,.0f}"


def _make_entity(
    entity_type: str,
    uuid: str,
    name: str,
    summary: str,
    attributes: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Rakit satu entitas dalam skema EntityNode (lihat
    zep_entity_reader.EntityNode.to_dict). Dibangun lewat EntityNode supaya
    skemanya dijamin identik dengan entitas hasil pipeline Zep — termasuk
    `related_edges` / `related_nodes` yang kosong (diisi Phase 2c nanti).
    """
    node = EntityNode(
        uuid=uuid,
        name=name,
        # label pertama = tipe spesifik, "Entity" = label default (sama seperti
        # node Zep); EntityNode.get_entity_type() mengembalikan label pertama.
        labels=[entity_type, "Entity"],
        summary=summary,
        attributes=attributes,
    )
    return node.to_dict()


# ---------------------------------------------------------------------------
# Ekstraktor per-kelompok entitas
# ---------------------------------------------------------------------------
def _extract_company_entity(
    ticker: str, fundamental: Dict[str, Any], as_of_label: str
) -> Dict[str, Any]:
    """Entity `company`: ticker + nama perusahaan.

    Catatan: get_stock_context() saat ini TIDAK membawa nama panjang
    perusahaan (fetch_fundamental_data hanya mengembalikan sector/industry),
    jadi `company_name` bisa None dan `name` jatuh ke ticker. Ini di-log,
    bukan dikarang.
    """
    company_name = fundamental.get("company_name") or fundamental.get("long_name")
    if _is_missing(company_name):
        company_name = None
        logger.info(
            "[%s] nama perusahaan tidak tersedia di stock_context; "
            "memakai ticker sebagai nama entity company", ticker
        )

    sector = fundamental.get("sector")
    industry = fundamental.get("industry")
    descriptor = " / ".join([p for p in (sector, industry) if not _is_missing(p)])
    summary = f"{company_name or ticker} ({ticker})"
    if descriptor:
        summary += f" — {descriptor}"
    summary += f". Konteks per {as_of_label}."

    return _make_entity(
        entity_type=ENTITY_TYPE_COMPANY,
        uuid=f"{ticker}::company",
        name=company_name or ticker,
        summary=summary,
        attributes={
            "ticker": ticker,
            "company_name": company_name,
            "sector": sector if not _is_missing(sector) else None,
            "industry": industry if not _is_missing(industry) else None,
            "as_of_date": as_of_label,
        },
    )


def _extract_sector_entity(
    ticker: str, fundamental: Dict[str, Any], as_of_label: str
) -> Optional[Dict[str, Any]]:
    """Entity `sector`: GICS sector (+ industry kalau ada)."""
    sector = fundamental.get("sector")
    if _is_missing(sector):
        logger.info("[%s] sector kosong di fundamental -> entity sector dilewati", ticker)
        return None

    industry = fundamental.get("industry")
    has_industry = not _is_missing(industry)
    summary = f"GICS sector: {sector}"
    if has_industry:
        summary += f"; industry: {industry}"
    summary += f" (klasifikasi {ticker})."

    return _make_entity(
        entity_type=ENTITY_TYPE_SECTOR,
        uuid=f"{ticker}::sector::{_slug(sector)}",
        name=sector,
        summary=summary,
        attributes={
            "ticker": ticker,
            "gics_sector": sector,
            "industry": industry if has_industry else None,
            "as_of_date": as_of_label,
        },
    )


def _extract_sharia_compliance_entity(
    ticker: str, sharia: Dict[str, Any], as_of_label: str
) -> Optional[Dict[str, Any]]:
    """Entity `sharia_screen`: the AAOIFI SS-21 screen result (Phase 1g).

    Carries the screen fields verbatim in `attributes` (no re-computation, no
    paraphrase) so a downstream consumer can enforce the verdict. Skipped +
    logged when the screen has no successful result — never fabricated.
    """
    if not sharia or not sharia.get("success"):
        logger.info(
            "[%s] sharia_compliance absen / gagal (%r) -> entity sharia_screen dilewati",
            ticker, (sharia or {}).get("error"),
        )
        return None

    ratio = sharia.get("debt_to_market_cap")
    threshold = sharia.get("debt_to_market_cap_threshold")
    overall = sharia.get("overall_compliant")
    category = sharia.get("excluded_category")

    verdict_word = (
        "compliant" if overall is True
        else "NOT compliant" if overall is False
        else "indeterminate"
    )
    ratio_str = "n/a" if _is_missing(ratio) else f"{float(ratio):.4f}"
    summary = (
        f"Sharia screen ({sharia.get('standard') or 'AAOIFI SS-21'}) {ticker}: "
        f"{verdict_word}. Debt/market-cap {ratio_str}"
    )
    if not _is_missing(threshold):
        summary += f" (AAOIFI cap <{threshold:g})"
    summary += f"; business-activity exclusion: {category or 'none'}."
    summary += " PARTIAL screen — some AAOIFI criteria are not computable from the data source."

    return _make_entity(
        entity_type=ENTITY_TYPE_SHARIA_SCREEN,
        uuid=f"{ticker}::sharia_screen",
        name=f"{ticker} Sharia Screen",
        summary=summary,
        attributes={
            "ticker": ticker,
            "standard": sharia.get("standard"),
            "debt_to_market_cap": ratio if not _is_missing(ratio) else None,
            "debt_to_market_cap_threshold": threshold if not _is_missing(threshold) else None,
            "passes_debt_screen": sharia.get("passes_debt_screen"),
            "sector_exclusion_flag": sharia.get("sector_exclusion_flag"),
            "excluded_category": category,
            "exclusion_reason": sharia.get("exclusion_reason"),
            "overall_compliant": overall,
            "is_partial_screen": True,
            "as_of_date": as_of_label,
            "is_point_in_time": False,
        },
    )


def _extract_valuation_metric_entities(
    ticker: str, fundamental: Dict[str, Any], as_of_label: str, is_pit: Optional[bool]
) -> List[Dict[str, Any]]:
    """Entity `valuation_metric`: P/E, P/B, market cap — masing-masing terpisah."""
    entities: List[Dict[str, Any]] = []
    for key, display_name, unit in _VALUATION_METRIC_SPECS:
        value = fundamental.get(key)
        if _is_missing(value):
            logger.info(
                "[%s] valuation_metric '%s' (%s) null/absen -> dilewati",
                ticker, key, display_name,
            )
            continue

        if key == "market_cap":
            rendered = _fmt_money(value)
        else:
            rendered = f"{float(value):.2f}"
        summary = f"{display_name} {ticker} = {rendered} (per {as_of_label})."
        if is_pit is False:
            summary += " NB: fundamental bukan point-in-time (nilai terkini)."

        entities.append(_make_entity(
            entity_type=ENTITY_TYPE_VALUATION_METRIC,
            uuid=f"{ticker}::valuation_metric::{key}",
            name=f"{ticker} {display_name}",
            summary=summary,
            attributes={
                "ticker": ticker,
                "metric": key,
                "display_name": display_name,
                "value": value,
                "unit": unit,
                "as_of_date": as_of_label,
                "is_point_in_time": is_pit,
            },
        ))
    return entities


def _extract_fundamental_metric_entities(
    ticker: str, fundamental: Dict[str, Any], as_of_label: str, is_pit: Optional[bool]
) -> List[Dict[str, Any]]:
    """Entity `fundamental_metric`: revenue growth, profit margin, ROE, debt/equity
    — hanya yang benar-benar terisi di data fundamental (null dilewati + di-log)."""
    entities: List[Dict[str, Any]] = []
    for display_name, unit, candidate_keys in _FUNDAMENTAL_METRIC_SPECS:
        value = None
        matched_key = None
        for candidate in candidate_keys:
            candidate_value = fundamental.get(candidate)
            if not _is_missing(candidate_value):
                value, matched_key = candidate_value, candidate
                break

        if matched_key is None:
            logger.info(
                "[%s] fundamental_metric '%s' tidak ada/null di data fundamental "
                "(cek key: %s) -> entity dilewati, tidak dikarang",
                ticker, display_name, ", ".join(candidate_keys),
            )
            continue

        rendered = _fmt_pct(value) if unit == "fraction" else f"{float(value):.2f}"
        summary = f"{display_name} {ticker} = {rendered} (per {as_of_label})."
        if is_pit is False:
            summary += " NB: fundamental bukan point-in-time (nilai terkini)."

        entities.append(_make_entity(
            entity_type=ENTITY_TYPE_FUNDAMENTAL_METRIC,
            uuid=f"{ticker}::fundamental_metric::{matched_key}",
            name=f"{ticker} {display_name}",
            summary=summary,
            attributes={
                "ticker": ticker,
                "metric": matched_key,
                "display_name": display_name,
                "value": value,
                "unit": unit,
                "as_of_date": as_of_label,
                "is_point_in_time": is_pit,
            },
        ))
    return entities


def _classify_rsi(value: float) -> str:
    if value >= 70:
        return "overbought"
    if value <= 30:
        return "oversold"
    return "neutral"


def _classify_bollinger(pct_b: float) -> str:
    if pct_b >= 1.0:
        return "at/above upper band"
    if pct_b <= 0.0:
        return "at/below lower band"
    if pct_b >= 0.8:
        return "near upper band"
    if pct_b <= 0.2:
        return "near lower band"
    return "mid band"


def _extract_technical_signal_entities(
    ticker: str, price: Dict[str, Any], as_of_label: str
) -> List[Dict[str, Any]]:
    """Entity `technical_signal`: RSI, MACD, harga-vs-SMA50, harga-vs-SMA200,
    posisi Bollinger — dari price['technical_indicators'] (Phase 1e)."""
    indicators = price.get("technical_indicators")
    if not indicators:
        logger.warning(
            "[%s] technical_indicators kosong (price gagal / data kurang) "
            "-> semua entity technical_signal dilewati", ticker
        )
        return []

    latest_date = price.get("latest_date") or as_of_label
    entities: List[Dict[str, Any]] = []

    def add(indicator: str, display_name: str, value: Any, signal: str,
            summary: str, extra: Optional[Dict[str, Any]] = None) -> None:
        attributes = {
            "ticker": ticker,
            "indicator": indicator,
            "display_name": display_name,
            "value": value,
            "signal": signal,
            "as_of_date": as_of_label,
            "observed_date": latest_date,
        }
        if extra:
            attributes.update(extra)
        entities.append(_make_entity(
            entity_type=ENTITY_TYPE_TECHNICAL_SIGNAL,
            uuid=f"{ticker}::technical_signal::{indicator}",
            name=f"{ticker} {display_name}",
            summary=summary,
            attributes=attributes,
        ))

    # RSI(14)
    rsi_value = indicators.get("rsi_14")
    if _is_missing(rsi_value):
        logger.info("[%s] technical_signal 'rsi_14' null -> dilewati", ticker)
    else:
        signal = _classify_rsi(float(rsi_value))
        add("rsi_14", "RSI (14)", rsi_value, signal,
            f"RSI(14) {ticker} = {float(rsi_value):.2f} -> {signal} (per {latest_date}).")

    # MACD 12/26/9
    macd = indicators.get("macd_signal") or {}
    histogram = macd.get("histogram")
    if _is_missing(histogram):
        logger.info("[%s] technical_signal 'macd' histogram null -> dilewati", ticker)
    else:
        signal = "bullish" if float(histogram) > 0 else "bearish" if float(histogram) < 0 else "flat"
        add("macd", "MACD (12/26/9)", histogram, signal,
            f"MACD {ticker}: histogram {float(histogram):+.4f} -> {signal} "
            f"(macd={macd.get('macd')}, signal={macd.get('signal')}, per {latest_date}).",
            extra={
                "macd": macd.get("macd"),
                "signal_line": macd.get("signal"),
                "histogram": histogram,
            })

    # Harga vs SMA50 / SMA200
    for indicator, display_name in (
        ("price_vs_sma50", "Price vs SMA50"),
        ("price_vs_sma200", "Price vs SMA200"),
    ):
        pct = indicators.get(indicator)
        if _is_missing(pct):
            logger.info("[%s] technical_signal '%s' null -> dilewati", ticker, indicator)
            continue
        signal = "above" if float(pct) > 0 else "below" if float(pct) < 0 else "at"
        add(indicator, display_name, pct, signal,
            f"{display_name} {ticker}: {float(pct):+.2f}% -> harga {signal} rata-rata "
            f"(per {latest_date}).")

    # Posisi Bollinger (%B)
    pct_b = indicators.get("bollinger_position")
    if _is_missing(pct_b):
        logger.info("[%s] technical_signal 'bollinger_position' null -> dilewati", ticker)
    else:
        signal = _classify_bollinger(float(pct_b))
        add("bollinger_position", "Bollinger %B", pct_b, signal,
            f"Bollinger %B {ticker} = {float(pct_b):.2f} -> {signal} (per {latest_date}).")

    return entities


# ---------------------------------------------------------------------------
# API publik
# ---------------------------------------------------------------------------
def extract_financial_entities(stock_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Ekstrak entitas FINANSIAL dari satu dict `get_stock_context(ticker,
    as_of_date)` (data layer Phase 1).

    Menggantikan peran `ZepEntityReader.filter_defined_entities()` di tahap
    graph-building: mengembalikan daftar entitas dengan SKEMA yang sama
    (EntityNode.to_dict): uuid / name / labels / summary / attributes /
    related_edges / related_nodes. Tipe entitas ada di `labels[0]`.

    Tipe entitas yang dihasilkan:
      - company            : 1 entitas  (ticker + nama perusahaan)
      - sector             : 0..1       (GICS sector + industry bila ada)
      - valuation_metric   : 0..3       (P/E, P/B, market cap — terpisah)
      - fundamental_metric : 0..4       (revenue growth, profit margin, ROE,
                                         debt/equity — hanya yang terisi)
      - technical_signal   : 0..5       (RSI, MACD, price-vs-SMA50/200,
                                         Bollinger %B — dari Phase 1e)
      - sharia_screen      : 0..1       (AAOIFI SS-21 screen result — Phase 1g;
                                         hanya bila sharia_compliance.success)

    Field null / tidak tersedia -> entitasnya DILEWATI (dicatat via logger),
    tidak dibuat dengan nilai null dan tidak dikarang.

    Konstruksi edge antar entitas TIDAK dilakukan di sini (Phase 2c).
    """
    if not isinstance(stock_context, dict):
        raise TypeError(f"stock_context harus dict, dapat {type(stock_context).__name__}")

    ticker = (stock_context.get("ticker") or "").strip().upper()
    if not ticker:
        logger.error("stock_context tanpa ticker -> tidak ada entitas yang bisa diekstrak")
        return []

    as_of_label = stock_context.get("as_of_date") or "live"
    price = stock_context.get("price") or {}
    fundamental = stock_context.get("fundamental") or {}

    price_ok = bool(price.get("success"))
    fundamental_ok = bool(fundamental.get("success"))
    if not price_ok:
        logger.warning(
            "[%s] bagian price gagal (error=%r) -> entitas turunan harga dilewati",
            ticker, price.get("error"),
        )
    if not fundamental_ok:
        logger.warning(
            "[%s] bagian fundamental gagal (error=%r) -> entitas turunan fundamental dilewati",
            ticker, fundamental.get("error"),
        )

    is_pit = fundamental.get("is_point_in_time") if fundamental_ok else None

    entities: List[Dict[str, Any]] = []

    # company selalu dibuat (identitas dasar), memakai apa pun yang ada
    entities.append(_extract_company_entity(ticker, fundamental, as_of_label))

    # sharia_compliance (Phase 1g) punya "success" sendiri, lepas dari fundamental
    sharia_entity = _extract_sharia_compliance_entity(
        ticker, stock_context.get("sharia_compliance") or {}, as_of_label
    )
    if sharia_entity is not None:
        entities.append(sharia_entity)

    if fundamental_ok:
        sector_entity = _extract_sector_entity(ticker, fundamental, as_of_label)
        if sector_entity is not None:
            entities.append(sector_entity)
        entities.extend(
            _extract_valuation_metric_entities(ticker, fundamental, as_of_label, is_pit)
        )
        entities.extend(
            _extract_fundamental_metric_entities(ticker, fundamental, as_of_label, is_pit)
        )

    if price_ok:
        entities.extend(_extract_technical_signal_entities(ticker, price, as_of_label))

    counts: Dict[str, int] = {}
    for entity in entities:
        entity_type = entity["labels"][0]
        counts[entity_type] = counts.get(entity_type, 0) + 1
    logger.info(
        "[%s] ekstraksi entitas finansial @ %s selesai: %d entitas (%s)",
        ticker, as_of_label, len(entities),
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "kosong",
    )

    return entities


def extract_financial_entity_nodes(stock_context: Dict[str, Any]) -> List[EntityNode]:
    """
    Sama dengan extract_financial_entities() tetapi mengembalikan objek
    `EntityNode` — bentuk yang dikonsumsi langsung oleh
    `oasis_profile_generator.generate_profiles_from_entities()` dan
    `simulation_config_generator._summarize_entities()`. Disediakan supaya
    slot pipeline lama bisa di-drop-in tanpa mengubah kode downstream.
    """
    return [_entity_dict_to_node(entity) for entity in extract_financial_entities(stock_context)]


def build_filtered_entities(stock_context: Dict[str, Any]) -> FilteredEntities:
    """
    Bungkus hasil ekstraksi menjadi `FilteredEntities`, meniru return type
    `ZepEntityReader.filter_defined_entities()` sehingga tahap graph-building
    bisa memakai fungsi ini sebagai pengganti drop-in.
    """
    nodes = extract_financial_entity_nodes(stock_context)
    entity_types = {n.get_entity_type() for n in nodes if n.get_entity_type()}
    return FilteredEntities(
        entities=nodes,
        entity_types=entity_types,
        total_count=len(nodes),
        filtered_count=len(nodes),
    )


def _entity_dict_to_node(entity: Dict[str, Any]) -> EntityNode:
    return EntityNode(
        uuid=entity["uuid"],
        name=entity["name"],
        labels=list(entity["labels"]),
        summary=entity["summary"],
        attributes=dict(entity["attributes"]),
        related_edges=list(entity.get("related_edges", [])),
        related_nodes=list(entity.get("related_nodes", [])),
    )


if __name__ == "__main__":
    # Demo / test manual — lihat instruksi di bawah.
    #   cd backend && python -m app.services.financial_entity_extractor
    import json

    from ..data_layer.market_data import get_stock_context

    TICKER = "AAPL"
    AS_OF = "2024-06-01"

    print(f"{'=' * 70}\nget_stock_context({TICKER!r}, as_of_date={AS_OF!r})"
          f" -> extract_financial_entities()\n{'=' * 70}")

    ctx = get_stock_context(TICKER, as_of_date=AS_OF)
    print(f"stock_context.success = {ctx['success']} "
          f"(price={ctx['price'].get('success')}, fundamental={ctx['fundamental'].get('success')})")
    print(f"fundamental.is_point_in_time = {ctx['fundamental'].get('is_point_in_time')}\n")

    financial_entities = extract_financial_entities(ctx)

    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for entity in financial_entities:
        by_type.setdefault(entity["labels"][0], []).append(entity)

    print(f"\nTOTAL: {len(financial_entities)} entitas "
          f"({', '.join(f'{k}={len(v)}' for k, v in sorted(by_type.items()))})\n")

    for entity_type in sorted(by_type):
        group = by_type[entity_type]
        print(f"\n{'-' * 70}\n{entity_type}  ({len(group)})\n{'-' * 70}")
        for entity in group:
            print(f"  • {entity['name']}   [uuid={entity['uuid']}]")
            print(f"      entity_type (labels)  : {entity['labels']}")
            print(f"      summary               : {entity['summary']}")
            print(f"      attributes            : "
                  f"{json.dumps(entity['attributes'], ensure_ascii=False)}")
            print(f"      related_edges/nodes   : "
                  f"{entity['related_edges']} / {entity['related_nodes']}  "
                  f"(diisi Phase 2c)")

    print(f"\n{'=' * 70}\nFull JSON dump\n{'=' * 70}")
    print(json.dumps(financial_entities, ensure_ascii=False, indent=2))
