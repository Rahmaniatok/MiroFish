"""
Fase 10b — Generator 8 persona investor gaya MiroFish original.

Mengikuti persis docs/design/fase10a_persona_generation.md. Jalur ini TERPISAH
dari Fase 3 (oasis_profile_generator.py: 6 persona tetap, stance dihitung dari
angka pasti). Di sini LLM bebas menentukan kepribadian/filosofi/bias, dengan
temperature TINGGI dan hasil yang sengaja NON-DETERMINISTIK (bagian 4 dokumen:
disengaja, bukan bug). Persistence hanya "jangan generate ulang kalau sudah ada",
bukan memaksa hasil sama.

Output berhenti di identitas persona. Tidak ada stance/score, dan tidak ada
follow-network / activity profile / sentiment baseline (itu Fase 11a).
"""

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from ..config import Config
from ..data_layer import cache as _cache_module
from ..data_layer.news_data import get_news_data
from ..data_layer.universe import screen_universe
from ..utils.logger import get_logger
from ..utils.openai_chat_compat import create_chat_completion, extract_chat_completion_text
from .oasis_profile_generator import GAMMA_ARCHETYPE, INVESTOR_ARCHETYPES

logger = get_logger('mirofish.persona_generator')

# --- Sampling berita (dokumen bagian 2) ---
MAX_TICKERS = 20
MAX_TICKERS_PER_SECTOR = 5
ARTICLES_PER_TICKER = 3
SUMMARY_MAX_CHARS = 200
MIN_ARTICLES_FOR_GROUNDING = 10

# --- Persona (dokumen bagian 1 & 5) ---
PERSONA_COUNT = 8
BIASES_MIN, BIASES_MAX = 2, 4

# --- Temperature (dokumen bagian 3; Langkah 0 memastikan kompatibel dengan
# provider yang dipakai: vLLM OpenAI-compatible, range temperature [0, 2]) ---
BASE_TEMPERATURE = 1.0
TEMPERATURE_STEP = 0.1
TEMPERATURE_FLOOR = 0.8

# Persistence: tabel sendiri (bukan cache.py) — lihat docstring _save_run.
PERSONA_DB_PATH = os.path.join(os.path.dirname(_cache_module.DB_PATH), 'persona_runs.db')

_REQUIRED_STR_FIELDS = (
    "name", "tagline", "investment_philosophy", "philosophy_label",
    "personality", "communication_style", "edge_vs_others",
)


@dataclass
class InvestorPersona:
    """Identitas 1 persona. SENGAJA tanpa stance/score/verdict/confidence (Fase 11b)."""
    name: str
    tagline: str
    investment_philosophy: str
    philosophy_label: str
    biases: List[str]
    personality: str
    communication_style: str
    edge_vs_others: str
    short_bio: Optional[str] = None


@dataclass
class GroundingResult:
    """Hasil builder grounding (Tahap 4, docs/design/tahap4_persona_from_graph_design.md
    §1) — bentuk SAMA PERSIS dari kedua sumber (`build_grounding_from_news`/
    `build_grounding_from_graph`), isi `provenance` beda per sumber (§5)."""
    news_lines: List[str]
    grounding: str   # "news" | "zep_graph" | "none" -- dipakai _build_persona_prompt
    source: str      # "news" | "zep_graph" -- SELALU sumber builder, walau grounding=="none"
    provenance: Dict[str, Any]


# ---------------------------------------------------------------------------
# 1. Sampling berita
# ---------------------------------------------------------------------------

def _select_tickers(universe: List[Dict[str, Any]]) -> List[str]:
    """Maks 20 ticker, cap 5/sektor. Urutan universe (market cap menurun) dipertahankan."""
    selected: List[str] = []
    per_sector: Dict[str, int] = {}
    for entry in universe:
        if len(selected) >= MAX_TICKERS:
            break
        sector = entry.get("gics_sector") or ""
        if per_sector.get(sector, 0) >= MAX_TICKERS_PER_SECTOR:
            continue
        per_sector[sector] = per_sector.get(sector, 0) + 1
        selected.append(entry["ticker"])
    return selected


def _sample_articles(
    universe: List[Dict[str, Any]], as_of_date: Optional[str]
) -> List[Dict[str, Any]]:
    """
    Kumpulkan artikel sample (maks 20 x 3 = 60), sudah dedup by article_id.
    Tiap dict = artikel Fase 8b + "ticker" (ticker yang di-query).
    Ticker success=False dilewati begitu saja (tidak diganti ticker lain).
    """
    seen_ids = set()
    sample: List[Dict[str, Any]] = []
    for ticker in _select_tickers(universe):
        result = get_news_data(ticker, as_of_date=as_of_date)
        if not result.get("success"):
            logger.info(f"Sampling berita: {ticker} dilewati ({result.get('error')})")
            continue
        articles = sorted(
            result.get("articles") or [],
            key=lambda a: a.get("published_at") or "",
            reverse=True,
        )[:ARTICLES_PER_TICKER]
        for art in articles:
            art_id = art.get("article_id")
            if art_id in seen_ids:
                continue  # duplikat dibuang, TIDAK diganti artikel ke-4
            seen_ids.add(art_id)
            sample.append({**art, "ticker": ticker})
    return sample


def _format_article_line(art: Dict[str, Any]) -> str:
    date_part = (art.get("published_at") or "")[:10]
    parts = [
        f"[{date_part}] {art['ticker']}",
        art.get("publisher") or "unknown",
        (art.get("headline") or "").strip(),
    ]
    summary = (art.get("summary") or "").strip()
    if summary:
        parts.append(summary[:SUMMARY_MAX_CHARS])
    return " | ".join(parts)


def _sample_articles_for_storage(sample: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Subset artikel sample yang disimpan di metadata run (headline sudah ada di memori; tanpa fetch baru)."""
    return [
        {
            "article_id": a.get("article_id"),
            "ticker": a.get("ticker"),
            "headline": a.get("headline"),
            "publisher": a.get("publisher"),
            "published_at": a.get("published_at"),
        }
        for a in sample
    ]


def _grounding_for(sample: List[Dict[str, Any]]) -> str:
    return "news" if len(sample) >= MIN_ARTICLES_FOR_GROUNDING else "none"


def build_grounding_from_news(
    universe: List[Dict[str, Any]], as_of_date: Optional[str]
) -> GroundingResult:
    """Bangun grounding dari sampling berita — Tahap 4 §1 desain (Opsi a).

    Menggantikan `_sample_news_for_grounding` (dihapus — supersede penuh oleh
    fungsi ini) DAN logika inline yang sebelumnya ada langsung di dalam
    `generate_personas`. Perilaku sampling itu sendiri (`_sample_articles`/
    `_grounding_for`/`_format_article_line`) TIDAK berubah, cuma dipindah +
    dibungkus jadi `GroundingResult` supaya `generate_personas` bisa generik
    menerima grounding sudah jadi dari sumber manapun.
    """
    sample = _sample_articles(universe, as_of_date)
    grounding = _grounding_for(sample)
    if grounding == "none":
        return GroundingResult(
            news_lines=[], grounding="none", source="news",
            provenance={"article_ids": [], "sample_articles": []},
        )
    return GroundingResult(
        news_lines=[_format_article_line(a) for a in sample],
        grounding="news",
        source="news",
        provenance={
            "article_ids": [a["article_id"] for a in sample],
            "sample_articles": _sample_articles_for_storage(sample),
        },
    )


# --- Tahap 4 §2 desain: anggaran token dinamis untuk grounding dari graph Zep ---
ZEP_GROUNDING_TOKEN_BUDGET = 15_000  # 500 (prompt) + 15.000 (grounding) + ~3.000
                                       # (output 8 persona) ~= 18.500 token ~= 56,5%
                                       # dari context window 32.768 (Qwen2.5-7B)
_GROUNDING_TOKENIZER_MODEL_ID = "Qwen/Qwen2.5-7B-Instruct-AWQ"  # HARDCODE, sengaja
    # TIDAK diturunkan dari Config.LLM_MODEL_NAME (default 'gpt-4o-mini' bukan repo
    # HuggingFace valid) — lihat catatan operasional desain §2.
_grounding_tokenizer = None  # lazy singleton module-level, dimuat SEKALI per proses


def _get_grounding_tokenizer():
    """AutoTokenizer.from_pretrained makan 1,7-6,3 detik/panggilan (diukur
    investigasi) — dimuat sekali, bukan tiap panggilan build_grounding_from_graph."""
    global _grounding_tokenizer
    if _grounding_tokenizer is None:
        from transformers import AutoTokenizer
        _grounding_tokenizer = AutoTokenizer.from_pretrained(_GROUNDING_TOKENIZER_MODEL_ID)
    return _grounding_tokenizer


def _format_entity_text(entity: Dict[str, Any]) -> str:
    """1 entity FilteredEntities -> 1 blok teks. Format PERSIS investigasi
    (divalidasi dengan tokenizer nyata): "Type: Name - Summary" + bullet
    SEMUA related_edges (tidak dipotong sebagian — lihat desain §2)."""
    custom_labels = [l for l in entity.get("labels", []) if l not in ("Entity", "Node")]
    entity_type = custom_labels[0] if custom_labels else "Entity"
    lines = [f"{entity_type}: {entity['name']} - {entity.get('summary') or ''}"]
    for edge in entity.get("related_edges") or []:
        lines.append(f"  - {edge.get('edge_name', '')}: {edge.get('fact', '')}")
    return "\n".join(lines)


def build_grounding_from_graph(
    filtered_entities: Dict[str, Any],
    token_budget: int = ZEP_GROUNDING_TOKEN_BUDGET,
) -> GroundingResult:
    """Bangun grounding dari FilteredEntities Tahap 3 — Tahap 4 §2 desain.

    Urutkan entity by degree (len(related_edges)) tertinggi->terendah, masukkan
    satu per satu sambil menghitung token berjalan (tokenizer asli, bukan
    estimasi), STOP begitu entity berikutnya akan melebihi `token_budget`
    (entity itu TIDAK dimasukkan, bukan dipotong sebagian). EDGE CASE 2: entity
    PERTAMA (degree tertinggi) SELALU masuk apa adanya walau sendirian sudah
    melebihi budget — membuang entity paling terhubung demi kepatuhan ketat ke
    budget adalah trade-off yang lebih buruk (lihat desain §2 untuk alasan).
    `filtered_entities` kosong -> GroundingResult kosong (grounding="none"),
    JANGAN error.
    """
    entities = filtered_entities.get("entities") or []
    empty = GroundingResult(
        news_lines=[], grounding="none", source="zep_graph",
        provenance={
            "source_entity_count": 0, "source_entity_names": [],
            "source_grounding_tokens": 0, "filter_method_used": "top_degree_token_budget",
        },
    )
    if not entities:
        return empty

    ranked = sorted(entities, key=lambda e: len(e.get("related_edges") or []), reverse=True)
    tokenizer = _get_grounding_tokenizer()

    news_lines: List[str] = []
    included_names: List[str] = []
    total_tokens = 0

    for entity in ranked:
        text = _format_entity_text(entity)
        entity_tokens = len(tokenizer.encode(text))
        if total_tokens > 0 and total_tokens + entity_tokens > token_budget:
            break  # STOP -- entity ini TIDAK dimasukkan, bukan dipotong sebagian
        news_lines.append(text)
        included_names.append(entity["name"])
        total_tokens += entity_tokens
        # Guard `total_tokens > 0` di atas SENGAJA membuat entity PERTAMA selalu
        # masuk walau sendirian > token_budget (EDGE CASE 2 desain §2).

    if not news_lines:
        return empty  # tidak realistis tercapai (EDGE CASE 2 selalu meloloskan 1), defensif

    return GroundingResult(
        news_lines=news_lines,
        grounding="zep_graph",
        source="zep_graph",
        provenance={
            "source_entity_count": len(news_lines),
            "source_entity_names": included_names,
            "source_grounding_tokens": total_tokens,
            "filter_method_used": "top_degree_token_budget",
        },
    )


# ---------------------------------------------------------------------------
# 2. Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a creative director for an investment-debate simulation. You invent
fictional retail and professional investors with distinct minds. You are NOT
building a balanced panel of sensible experts: you are building 8 characters
who would genuinely disagree, argue and irritate each other in a public
social-media discussion about stocks.

Rules:
- Output ONLY a JSON object: {"personas": [ ...exactly 8 objects... ]}
- Every persona must be meaningfully DIFFERENT from every other one. No two may
  share an interchangeable philosophy, communication style or bias set.
- Do not use real people's names, and do not name a persona after a classic
  archetype (Value, Growth, Quant, Macro, Sentiment).
- Investment philosophy is open: it may resemble a known school or be something
  new (e.g. "momentum chaser", "contrarian skeptic"). Do not restrict yourself.
- Do NOT assign stances, ratings or scores on any specific stock. Those come
  later, during the debate."""

_USER_HEAD = """Invent exactly 8 investor personas who are mutually different.

Spread the 8 across these axes (each axis must show at least 3 distinct values):
 - time horizon (days ... decades)
 - attitude to risk
 - how they use news and information
 - communication style (terse, rambling, sarcastic, academic, hype-driven, ...)
At least 2 must be unconventional (not a sober fundamental investor), and at
least 1 must be strongly emotional or irrational in a way that is a bias.
"""

_GROUNDING_INSTRUCTION = """
Use the market context below only as loose inspiration for the mood. You do
not need to reference it, and you must not treat it as facts to verify.
"""

_NO_NEWS_INSTRUCTION = """
Draw only on your general understanding of how markets and investor psychology
work. Do not refer to any specific news event or company development.
"""

_USER_SCHEMA = """
Return JSON:
{"personas": [
  {
    "name": "unique invented name",
    "tagline": "one sentence, <=140 chars",
    "investment_philosophy": "2-4 sentences",
    "philosophy_label": "1-4 free-form words",
    "biases": ["2-4 short items"],
    "personality": "2-4 sentences",
    "communication_style": "2-4 sentences, how they write posts",
    "edge_vs_others": "one sentence: what makes them unlike the other 7",
    "short_bio": "optional, 1-3 sentences"
  }, ...
]}"""


def _build_persona_prompt(news_lines: List[str], grounding: str) -> Tuple[str, str]:
    """Return (system_prompt, user_prompt). grounding=="none" -> tanpa blok konteks.

    BUGFIX (ditemukan verifikasi E2E Tahap 4->6, 2026-09-24): kondisi ini
    sebelumnya `grounding == "news"` -- hardcode ke SATU sumber grounding,
    sehingga grounding.grounding=="zep_graph" (Tahap 4, entity dari graph Zep)
    diam-diam jatuh ke jalur "tanpa grounding sama sekali", walau
    `news_lines` berisi puluhan-ratusan baris entity nyata. Dikonfirmasi
    lewat eksekusi langsung: prompt yang terkirim ke LLM tidak mengandung
    satupun baris entity, dan token yang benar-benar terpakai (~499) cocok
    persis dengan jalur "no grounding", bukan ~13.400 yang seharusnya.

    Kondisi diperbaiki jadi `grounding != "none"` (bukan enumerasi eksplisit
    "news"/"zep_graph") SUPAYA kelas bug yang SAMA tidak terulang kalau nanti
    ada sumber grounding ketiga -- blok konteks otomatis disertakan untuk
    SEMUA sumber yang punya isi, satu-satunya nilai yang sengaja
    mengecualikannya adalah "none" itu sendiri.
    """
    if grounding != "none" and news_lines:
        context_block = (
            f"\nMARKET CONTEXT SAMPLE ({len(news_lines)} items):\n"
            + "\n".join(news_lines)
            + "\n"
        )
        user = _USER_HEAD + _GROUNDING_INSTRUCTION + context_block + _USER_SCHEMA
    else:
        user = _USER_HEAD + _NO_NEWS_INSTRUCTION + _USER_SCHEMA
    return _SYSTEM_PROMPT, user


# ---------------------------------------------------------------------------
# 3. Parsing + validasi
# ---------------------------------------------------------------------------

def _repair_json(content: str) -> Optional[Dict[str, Any]]:
    """
    Helper perbaikan JSON milik Fase 10 (terpisah dari _try_fix_json Fase 3):
    parse langsung -> buang code fence -> ambil objek terluar -> tutup string/
    kurung yang terpotong (scanner sadar-string) -> buang koma menggantung ->
    ganti karakter kontrol. Return None kalau tetap gagal. Item terakhir yang
    terpotong akan gagal validasi field wajib -> retry (bukan diterima).
    """
    if not content:
        return None
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    def _try(s: str) -> Optional[Dict[str, Any]]:
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    parsed = _try(text)
    if parsed is not None:
        return parsed

    start = text.find("{")
    if start == -1:
        return None
    text = text[start:]

    # Scan: lacak string/escape dan tumpukan kurung untuk menutup potongan.
    stack: List[str] = []
    in_string = False
    escape = False
    end_of_object = None
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                end_of_object = i + 1
                break

    if end_of_object is not None:
        candidate = text[:end_of_object]
    else:
        candidate = text
        if in_string:
            if escape:
                candidate = candidate[:-1]
            candidate += '"'
        candidate = re.sub(r",\s*$", "", candidate)
        candidate += "".join(reversed(stack))

    for variant in (
        candidate,
        re.sub(r",(\s*[}\]])", r"\1", candidate),
        re.sub(r"[\x00-\x1f\x7f]", " ", re.sub(r",(\s*[}\]])", r"\1", candidate)),
    ):
        parsed = _try(variant)
        if parsed is not None:
            return parsed
    return None


def _norm(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _validate_personas(data: Any) -> Tuple[Optional[List[InvestorPersona]], Optional[str]]:
    """Validasi struktural bagian 5 (b-e). Return (personas, None) atau (None, pesan_spesifik)."""
    if not isinstance(data, dict) or not isinstance(data.get("personas"), list):
        return None, "validasi b gagal: key 'personas' tidak ada atau bukan list"
    items = data["personas"]
    if len(items) != PERSONA_COUNT:
        return None, f"validasi c gagal: expected {PERSONA_COUNT} personas, got {len(items)}"

    personas: List[InvestorPersona] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return None, f"validasi d gagal: persona #{idx + 1} bukan objek"
        for field_name in _REQUIRED_STR_FIELDS:
            val = item.get(field_name)
            if not isinstance(val, str) or not val.strip():
                return None, f"validasi d gagal: persona #{idx + 1} field '{field_name}' kosong/tipe salah"
        biases = item.get("biases")
        if (
            not isinstance(biases, list)
            or not (BIASES_MIN <= len(biases) <= BIASES_MAX)
            or not all(isinstance(b, str) and b.strip() for b in biases)
        ):
            return None, (
                f"validasi d gagal: persona #{idx + 1} field 'biases' harus list "
                f"{BIASES_MIN}-{BIASES_MAX} string non-kosong"
            )
        short_bio = item.get("short_bio")
        if short_bio is not None and not isinstance(short_bio, str):
            return None, f"validasi d gagal: persona #{idx + 1} field 'short_bio' bukan string"
        personas.append(InvestorPersona(
            name=item["name"].strip(),
            tagline=item["tagline"].strip(),
            investment_philosophy=item["investment_philosophy"].strip(),
            philosophy_label=item["philosophy_label"].strip(),
            biases=[b.strip() for b in biases],
            personality=item["personality"].strip(),
            communication_style=item["communication_style"].strip(),
            edge_vs_others=item["edge_vs_others"].strip(),
            short_bio=short_bio.strip() if isinstance(short_bio, str) and short_bio.strip() else None,
        ))

    names = [_norm(p.name) for p in personas]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        return None, f"validasi e gagal: nama duplikat setelah casefold: {dupes}"
    return personas, None


def _archetype_names() -> List[Tuple[str, str]]:
    """(nama_norm, key_norm) dari daftar Fase 3 yang sudah ada — tidak di-hardcode ulang."""
    pairs = [(_norm(a["name"]), _norm(a["key"])) for a in INVESTOR_ARCHETYPES]
    pairs.append((_norm(GAMMA_ARCHETYPE["name"]), ""))
    return pairs


def _soft_checks(personas: List[InvestorPersona]) -> List[str]:
    """Hanya warning (tidak memicu retry): label filosofi duplikat, nama = archetype Fase 3."""
    warnings: List[str] = []
    labels = [_norm(p.philosophy_label) for p in personas]
    for label in sorted({l for l in labels if labels.count(l) > 1}):
        warnings.append(f"philosophy_label duplikat antar persona: '{label}'")
    archetypes = _archetype_names()
    for p in personas:
        n = _norm(p.name)
        for arch_name, arch_key in archetypes:
            if n == arch_name or (arch_key and n == arch_key) or (arch_name and arch_name in n):
                warnings.append(f"nama persona '{p.name}' menyerupai archetype Fase 3 '{arch_name}'")
                break
    return warnings


# ---------------------------------------------------------------------------
# 4. Generate
# ---------------------------------------------------------------------------

def _get_llm_client() -> OpenAI:
    if not Config.LLM_API_KEY:
        raise ValueError("LLM_API_KEY 未配置")
    return OpenAI(api_key=Config.LLM_API_KEY, base_url=Config.LLM_BASE_URL)


def _temperature_for_attempt(attempt: int) -> float:
    """attempt 0-based: 1.0, 0.9, 0.8 (lantai 0.8). Kontras Fase 3 yang 0.5-0.7."""
    return round(max(BASE_TEMPERATURE - attempt * TEMPERATURE_STEP, TEMPERATURE_FLOOR), 2)


def _provenance_for_result(grounding: GroundingResult) -> Dict[str, Any]:
    """Union field provenance kedua sumber (Tahap 4 §5 desain) — field yang
    tidak relevan untuk sumber aktif diisi nilai netral ([]/0/None), TIDAK
    dihilangkan dari dict (beda dari pola Tahap 3 §7 "key hilang = gagal":
    di sini KEDUA jalur sama-sama hasil SUKSES, cuma beda metodologi)."""
    p = grounding.provenance
    return {
        "article_ids": p.get("article_ids", []),
        "sample_articles": p.get("sample_articles", []),
        "source_graph_id": p.get("source_graph_id"),
        "source_entity_count": p.get("source_entity_count", 0),
        "source_entity_names": p.get("source_entity_names", []),
        "source_grounding_tokens": p.get("source_grounding_tokens", 0),
        "filter_method_used": p.get("filter_method_used"),
    }


def generate_personas(
    grounding: GroundingResult,
    as_of_date: Optional[str] = None,
    max_attempts: int = 3,
    sectors: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Generate PERSIS 8 persona (1 panggilan LLM per attempt), lalu simpan bila sukses.
    Non-deterministik by design. Gagal setelah max_attempts -> {"success": False, ...}
    tanpa fallback template dan tanpa persona parsial.

    `grounding` SUDAH JADI (dibangun caller lewat `build_grounding_from_news` atau
    `build_grounding_from_graph` — Tahap 4 §1 desain, Opsi a) — fungsi ini GENERIK,
    tidak tahu/peduli dari mana asalnya. sectors/market_cap_tiers HANYA dipakai
    untuk cache key (`_screening_params`) — screen_universe TIDAK lagi dipanggil
    di sini (tanggung jawab caller sekarang, konsisten dengan `build_universe_graph`
    Tahap 3 yang juga tidak menangani kegagalan screen_universe secara khusus).
    """
    screening = _screening_params(sectors, market_cap_tiers, grounding.source)
    news_lines = grounding.news_lines
    system_prompt, user_prompt = _build_persona_prompt(news_lines, grounding.grounding)
    if grounding.grounding == "none":
        logger.warning(f"grounding='none' (sumber={grounding.source})")

    client = _get_llm_client()
    last_error = "tidak ada attempt dijalankan"

    for attempt in range(max_attempts):
        temperature = _temperature_for_attempt(attempt)
        try:
            response = create_chat_completion(
                client,
                model=Config.LLM_MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=temperature,
                # tidak set max_tokens (pola existing) — 8 persona output panjang
            )
            content = extract_chat_completion_text(response)
            finish_reason = getattr(response.choices[0], "finish_reason", None)
        except Exception as e:
            last_error = f"LLM call gagal: {e}"
            logger.warning(f"Persona attempt {attempt + 1}/{max_attempts}: {last_error[:120]}")
            if attempt < max_attempts - 1:
                time.sleep(1 * (attempt + 1))
            continue

        if finish_reason == "length":
            logger.warning(f"Persona attempt {attempt + 1}: output terpotong (finish_reason=length), mencoba perbaikan")

        data = _repair_json(content)
        if data is None:
            last_error = "validasi a gagal: JSON tidak valid dan tidak bisa diperbaiki"
            logger.warning(f"Persona attempt {attempt + 1}/{max_attempts}: {last_error}")
            continue

        personas, err = _validate_personas(data)
        if err:
            last_error = err
            logger.warning(f"Persona attempt {attempt + 1}/{max_attempts}: {err}")
            continue

        warnings = _soft_checks(personas)
        for w in warnings:
            logger.warning(f"Persona soft-check: {w}")

        result = {
            "success": True,
            "run_id": uuid.uuid4().hex,
            "as_of_date": as_of_date,
            "screening": screening,
            "universe_key": _universe_key(screening),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": Config.LLM_MODEL_NAME,
            "temperature_used": temperature,
            "attempt": attempt + 1,
            "grounding": grounding.grounding,
            "grounding_source": grounding.source,
            **_provenance_for_result(grounding),
            "personas": [asdict(p) for p in personas],
            "warnings": warnings,
            "from_cache": False,
        }
        try:
            _save_run(result)
        except Exception as e:
            logger.warning(f"Gagal menyimpan persona run {result['run_id']}: {e}")
            result["persisted"] = False
        else:
            result["persisted"] = True
        return result

    logger.error(f"generate_personas gagal setelah {max_attempts} attempt: {last_error}")
    return {
        "success": False,
        "error": f"gagal setelah {max_attempts} attempt; terakhir: {last_error}",
        "as_of_date": as_of_date,
        "screening": screening,
        "grounding": grounding.grounding,
    }


# ---------------------------------------------------------------------------
# 5. Persistence
# ---------------------------------------------------------------------------
# Tabel sendiri (bukan cache.py): cache.py meng-upsert via DELETE-then-INSERT pada
# key (ticker, as_of_date, data_type), sehingga force_regenerate akan MENIMPA
# run lama dan riwayat/audit hilang. Di sini tiap run = 1 baris baru (append-only),
# get_or_generate memakai yang TERBARU. Key as_of_date None (live) dipetakan ke
# "live:<tanggal UTC>" supaya tidak kedaluwarsa 30 menit seperti cache live.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_runs (
    run_id TEXT PRIMARY KEY,
    as_of_key TEXT NOT NULL,
    universe_key TEXT NOT NULL DEFAULT '',
    screening_json TEXT NOT NULL DEFAULT '{}',
    generated_at TEXT NOT NULL,
    model TEXT NOT NULL,
    temperature_used REAL NOT NULL,
    attempt INTEGER NOT NULL,
    grounding TEXT NOT NULL,
    article_ids_json TEXT NOT NULL,
    personas_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    sample_json TEXT,
    provenance_json TEXT
);
"""

# Kunci composite = (as_of_key, universe_key). universe_key = sha256 dari PARAMETER
# screening (sectors, market_cap_tiers[, grounding_source] — Tahap 4 §3), bukan dari
# daftar ticker hasil screening: (1) bisa dihitung SEBELUM screen_universe dijalankan,
# jadi cache hit tidak perlu memindai ~500 ticker; (2) stabil — fundamental/market cap
# bukan point-in-time sehingga daftar ticker bisa bergeser antar hari untuk parameter
# yang sama, dan itu tidak boleh membuat run tersimpan "hilang". Kompromi yang disadari:
# dua daftar ticker berbeda dari parameter identik dianggap universe yang sama.


def _screening_params(
    sectors: Optional[List[str]],
    market_cap_tiers: Optional[List[str]],
    grounding_source: str = "news",
) -> Dict[str, Any]:
    """Normalisasi (urutan/duplikat tidak berpengaruh; None tetap beda dari []).

    `grounding_source="news"` (default) TIDAK menambah key baru ke payload hash
    — hash-nya PERSIS sama seperti sebelum Tahap 4 ada, supaya persona_runs
    lama (semuanya jalur news) TETAP valid sebagai cache hit. Sumber lain
    (mis. "zep_graph") SELALU menambah key ini, sehingga TIDAK PERNAH
    menghasilkan hash yang sama dengan kombinasi sectors/tiers identik di
    jalur "news" — tabrakan cache yang jadi temuan investigasi Tahap 4 §4
    secara struktural tidak mungkin terjadi lagi (Tahap 4 §3 desain).
    """
    params: Dict[str, Any] = {
        "sectors": sorted(set(sectors)) if sectors is not None else None,
        "market_cap_tiers": sorted(set(market_cap_tiers)) if market_cap_tiers is not None else None,
    }
    if grounding_source != "news":
        params["grounding_source"] = grounding_source
    return params


def _universe_key(screening: Dict[str, Any]) -> str:
    payload = json.dumps(screening, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _as_of_key(as_of_date: Optional[str]) -> str:
    return as_of_date or f"live:{datetime.now(timezone.utc).date().isoformat()}"


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(PERSONA_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(PERSONA_DB_PATH)
    conn.execute(_SCHEMA)
    # Migrasi DB lama (kunci hanya as_of_date): baris lama diberi universe_key ''
    # yang tidak akan pernah cocok dengan hash baru, jadi tidak salah dipakai ulang.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(persona_runs)")}
    if "universe_key" not in cols:
        conn.execute("ALTER TABLE persona_runs ADD COLUMN universe_key TEXT NOT NULL DEFAULT ''")
    if "screening_json" not in cols:
        conn.execute("ALTER TABLE persona_runs ADD COLUMN screening_json TEXT NOT NULL DEFAULT '{}'")
    if "sample_json" not in cols:
        # Nullable: baris lama (sebelum kolom ini) dibaca sebagai sample_articles=[].
        conn.execute("ALTER TABLE persona_runs ADD COLUMN sample_json TEXT")
    if "provenance_json" not in cols:
        # Tahap 4 §6 desain: kolom BARU (bukan reinterpretasi sample_json, yang
        # semantiknya terikat ke bentuk artikel Finnhub). Nullable: NULL untuk
        # jalur news (provenance-nya sudah cukup lewat article_ids_json/sample_json)
        # dan untuk baris lama sebelum kolom ini ada.
        conn.execute("ALTER TABLE persona_runs ADD COLUMN provenance_json TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_persona_runs_key ON persona_runs (as_of_key, universe_key, generated_at)"
    )
    return conn


def _save_run(result: Dict[str, Any]) -> None:
    conn = _connect()
    try:
        provenance_payload = None
        if result.get("grounding_source") == "zep_graph":
            # Tahap 4 §6 desain: kolom provenance_json HANYA diisi untuk jalur
            # zep_graph -- jalur news sudah punya article_ids_json/sample_json.
            provenance_payload = json.dumps(
                {
                    "source_graph_id": result.get("source_graph_id"),
                    "source_entity_count": result.get("source_entity_count"),
                    "source_entity_names": result.get("source_entity_names"),
                    "source_grounding_tokens": result.get("source_grounding_tokens"),
                    "filter_method_used": result.get("filter_method_used"),
                },
                ensure_ascii=False,
            )
        conn.execute(
            "INSERT INTO persona_runs (run_id, as_of_key, universe_key, screening_json, generated_at, "
            "model, temperature_used, attempt, grounding, article_ids_json, personas_json, warnings_json, "
            "sample_json, provenance_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result["run_id"], _as_of_key(result["as_of_date"]), result["universe_key"],
                json.dumps(result["screening"]), result["generated_at"],
                result["model"], result["temperature_used"], result["attempt"],
                result["grounding"], json.dumps(result["article_ids"]),
                json.dumps(result["personas"], ensure_ascii=False),
                json.dumps(result["warnings"], ensure_ascii=False),
                json.dumps(result.get("sample_articles", []), ensure_ascii=False),
                provenance_payload,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load_latest_run(as_of_date: Optional[str], screening: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    universe_key = _universe_key(screening)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT run_id, generated_at, model, temperature_used, attempt, grounding, "
            "article_ids_json, personas_json, warnings_json, sample_json, provenance_json "
            "FROM persona_runs "
            "WHERE as_of_key = ? AND universe_key = ? ORDER BY generated_at DESC, rowid DESC LIMIT 1",
            (_as_of_key(as_of_date), universe_key),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    # NULL (jalur news, atau baris lama sebelum kolom ini ada) -> "tidak ada
    # provenance graph", bukan crash -- pola fallback PERSIS sample_json.
    provenance = json.loads(row[10]) if row[10] else {}
    return {
        "success": True,
        "run_id": row[0],
        "as_of_date": as_of_date,
        "screening": screening,
        "universe_key": universe_key,
        "generated_at": row[1],
        "model": row[2],
        "temperature_used": row[3],
        "attempt": row[4],
        "grounding": row[5],
        # screening_json baris lama tidak pernah punya key ini -> default "news",
        # backward-compatible dengan _screening_params (Tahap 4 §3 desain).
        "grounding_source": screening.get("grounding_source", "news"),
        "article_ids": json.loads(row[6]),
        # NULL (run lama sebelum kolom sample_json) -> [] : adapter memakai fallback "tanpa headline".
        "sample_articles": json.loads(row[9]) if row[9] else [],
        "source_graph_id": provenance.get("source_graph_id"),
        "source_entity_count": provenance.get("source_entity_count", 0),
        "source_entity_names": provenance.get("source_entity_names", []),
        "source_grounding_tokens": provenance.get("source_grounding_tokens", 0),
        "filter_method_used": provenance.get("filter_method_used"),
        "personas": json.loads(row[7]),
        "warnings": json.loads(row[8]),
        "from_cache": True,
        "persisted": True,
    }


def get_or_generate_personas(
    grounding: GroundingResult,
    as_of_date: Optional[str] = None,
    force_regenerate: bool = False,
    max_attempts: int = 3,
    sectors: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Pakai persona tersimpan terbaru untuk (as_of_date, universe, grounding.source)
    bila ada; kalau tidak (atau force_regenerate=True) generate baru lewat
    generate_personas. `grounding` SUDAH dibangun caller (build_grounding_from_news/
    build_grounding_from_graph — Tahap 4 §1/§4 desain). sectors/market_cap_tiers
    HARUS sama dengan yang dipakai membangun `grounding` — parameter yang sama
    diteruskan ke generate_personas untuk cache key. Ini BUKAN determinisme:
    generate_personas() tetap non-deterministik, ini hanya menghindari generate ulang.
    """
    screening = _screening_params(sectors, market_cap_tiers, grounding.source)
    if not force_regenerate:
        try:
            stored = _load_latest_run(as_of_date, screening)
        except Exception as e:
            logger.warning(f"Gagal membaca persona tersimpan, generate baru: {e}")
            stored = None
        if stored is not None:
            logger.info(
                f"Persona cache hit as_of_date={as_of_date} universe={stored['universe_key']} "
                f"run_id={stored['run_id']}"
            )
            return stored
    return generate_personas(
        grounding, as_of_date, max_attempts, sectors=sectors, market_cap_tiers=market_cap_tiers
    )


def generate_personas_from_universe_graph(
    as_of_date: Optional[str] = None,
    sectors: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
    force_regenerate: bool = False,
) -> Dict[str, Any]:
    """Convenience murni: rantai Tahap 3 (build_universe_graph) -> Tahap 4
    (get_or_generate_personas) dalam 1 panggilan BLOCKING — Tahap 4 §4 desain.

    JANGAN dipakai dari jalur request HTTP sinkron — Tahap 3 sendiri bisa makan
    waktu ~18 menit sampai ~2,3+ JAM tergantung skala universe
    (docs/design/tahap3_zep_feed_design.md), dan cache-miss di sini akan
    membuat panggilan ini blocking selama itu tanpa sinyal apapun di
    signature-nya. Cocok HANYA untuk skrip CLI/cron/batch job yang memang
    menerima blocking selama itu.
    """
    from .universe_graph_builder import build_universe_graph  # deferred: pemakai
        # jalur news saja tidak perlu import Zep/GraphBuilderService

    tahap3_result = build_universe_graph(as_of_date, sectors, market_cap_tiers)
    if not tahap3_result["success"]:
        return {
            "success": False,
            "error": f"Tahap 3 gagal: {tahap3_result['error']}",
            "as_of_date": as_of_date,
        }
    grounding = build_grounding_from_graph(tahap3_result["filtered_entities"])
    grounding.provenance["source_graph_id"] = tahap3_result.get("graph_id")
    return get_or_generate_personas(
        grounding,
        as_of_date=as_of_date,
        force_regenerate=force_regenerate,
        sectors=sectors,
        market_cap_tiers=market_cap_tiers,
    )
