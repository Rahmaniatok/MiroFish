"""
Fase 11a — Adapter InvestorPersona (Fase 10) -> artefak input OASIS.

Desain: docs/design/fase11a_oasis_adapter.md. Modul ini MELEWATI SimulationManager.prepare_simulation,
ZepEntityReader, OasisProfileGenerator dan SimulationConfigGenerator (semuanya bergantung pada Zep);
ia langsung menulis 4 artefak ke backend/uploads/simulations/<sim_id>/:

    twitter_profiles.csv -> reddit_profiles.json -> simulation_config.json -> state.json (TERAKHIR)

Fungsi murni (tanpa IO/LLM): compose_persona_text, derive_usernames, short_description, map_activity,
build_time_config, build_event_config. Fungsi ber-IO/LLM: get_or_generate_enrichment, build_oasis_artifacts.
"""

import csv
import json
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from ..config import Config
from ..utils.logger import get_logger
from ..utils.openai_chat_compat import create_chat_completion, extract_chat_completion_text
from . import persona_generator as _pg
from .persona_generator import (
    PERSONA_COUNT,
    InvestorPersona,
    _get_llm_client,
    _repair_json,
    _temperature_for_attempt,
)

logger = get_logger('mirofish.persona_oasis_adapter')

PersonaLike = Union[InvestorPersona, Dict[str, Any]]

# --- Komposisi teks / bio (dokumen 11a §1-2) ---
BIO_MAX_CHARS = 150
USERNAME_MAX_CHARS = 30
USERNAME_FALLBACK = "investor"
_TERMINAL_PUNCT = (".", "!", "?", "…")

# --- Enrichment (dokumen 11a §3) ---
MAX_ATTEMPTS = 3
AGE_MIN, AGE_MAX = 21, 85
RATING_MIN, RATING_MAX = 1, 5
COUNTRY_MAX_CHARS = 60
GENDERS = ("male", "female", "other")
MBTI_TYPES = (
    "INTJ", "INTP", "ENTJ", "ENTP", "INFJ", "INFP", "ENFJ", "ENFP",
    "ISTJ", "ISFJ", "ESTJ", "ESFJ", "ISTP", "ISFP", "ESTP", "ESFP",
)
_RATING_FIELDS = ("posting_intensity", "reactivity", "optimism", "influence_level")
_US_ALIASES = {"us", "usa", "u.s.", "u.s.a.", "united states of america"}

# --- Aktivitas (dokumen 11a §4) ---
ACTIVITY_LEVEL = 1.0
ACTIVE_HOURS = list(range(24))
ENTITY_TYPE = "Investor"
_RESPONSE_DELAY = {5: (1, 10), 4: (3, 20), 3: (5, 45), 2: (15, 90), 1: (60, 240)}
_INFLUENCE_WEIGHT = {1: 0.6, 2: 0.9, 3: 1.2, 4: 1.8, 5: 2.5}

# --- Round (dokumen 11a §5): (5*60)//30 == 10, dan action_logger (jam*2) juga 10 ---
TOTAL_SIMULATION_HOURS = 5
MINUTES_PER_ROUND = 30

# --- initial_posts (dokumen 11a §8) ---
MAX_INITIAL_POSTS = 4
POSTER_TYPE = "Investor"
FALLBACK_SEED_POST = "Market check-in: what are you watching in US equities right now, and what's your read?"
WARNING_NO_NEWS_SEED = "no_news_seed"

_SIMULATION_REQUIREMENT = (
    "Multi-persona investor discussion about US equities and market news "
    "(Fase 11a adapter from persona run; no Zep graph)."
)

_DEAD_CONFIG_NOTICE = (
    "NOTE: response_delay_min/max, sentiment_bias, stance, posts_per_hour, comments_per_hour, entity_uuid, "
    "entity_type and twitter_config/reddit_config are DEAD CONFIG: generated for completeness but NOT read by "
    "run_parallel_simulation.py, so they DO NOT affect the simulation. Only agent_id, entity_name, "
    "activity_level and active_hours are read by the runner; influence_weight is used only by this adapter "
    "to choose who publishes the initial posts."
)

_SIM_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ARTIFACT_ORDER = (
    "twitter_profiles.csv",
    "reddit_profiles.json",
    "simulation_config.json",
    "state.json",
)


# ---------------------------------------------------------------------------
# Util
# ---------------------------------------------------------------------------

def _as_persona(p: PersonaLike) -> InvestorPersona:
    """get_or_generate_personas mengembalikan persona sebagai dict (asdict); terima keduanya."""
    if isinstance(p, InvestorPersona):
        return p
    return InvestorPersona(
        name=p["name"],
        tagline=p["tagline"],
        investment_philosophy=p["investment_philosophy"],
        philosophy_label=p["philosophy_label"],
        biases=list(p["biases"]),
        personality=p["personality"],
        communication_style=p["communication_style"],
        edge_vs_others=p["edge_vs_others"],
        short_bio=p.get("short_bio"),
    )


def _clean(text: str) -> str:
    """Normalisasi whitespace (tanpa newline/tab/spasi ganda) + pastikan berakhir tanda baca penutup."""
    cleaned = " ".join(str(text).split())
    if cleaned and not cleaned.endswith(_TERMINAL_PUNCT):
        cleaned += "."
    return cleaned


# ---------------------------------------------------------------------------
# 1. Komposisi teks persona (murni)
# ---------------------------------------------------------------------------

def compose_persona_text(persona: PersonaLike) -> str:
    """
    Satu string tanpa newline untuk user_char (Twitter) DAN persona (Reddit). Urutan 8 bagian tetap;
    bagian "Background" dihilangkan seluruhnya bila short_bio None/kosong.
    """
    p = _as_persona(persona)
    sections = [
        f"{p.name} is an independent investor who shares views on US stocks and market news on social media.",
        f"Tagline: {p.tagline}",
        f"Investment philosophy ({p.philosophy_label}): {p.investment_philosophy}",
        f"Personality: {p.personality}",
        f"Communication style (how {p.name} writes posts): {p.communication_style}",
        f"Known biases: {'; '.join(p.biases)}",
        f"What sets {p.name} apart from the other investors: {p.edge_vs_others}",
    ]
    if p.short_bio and p.short_bio.strip():
        sections.append(f"Background: {p.short_bio}")
    return " ".join(_clean(s) for s in sections)


# ---------------------------------------------------------------------------
# 2. username / bio (murni)
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-z0-9]+", stripped.casefold())
    slug = "_".join(tokens)[:USERNAME_MAX_CHARS].rstrip("_")
    return slug or USERNAME_FALLBACK


def derive_usernames(personas: List[PersonaLike]) -> List[str]:
    """
    Slug deterministik per persona (urutan list = agent_id). Tabrakan dengan slug persona index lebih kecil
    -> suffix _{index}; masih bertabrakan (patologis) -> _{index}_{k}, k=2,3,... Yang pertama tanpa suffix.
    """
    used = set()
    usernames: List[str] = []
    for index, persona in enumerate(personas):
        slug = _slug(_as_persona(persona).name)
        candidate = slug
        if candidate in used:
            candidate = f"{slug}_{index}"
            k = 2
            while candidate in used:
                candidate = f"{slug}_{index}_{k}"
                k += 1
        used.add(candidate)
        usernames.append(candidate)
    return usernames


def short_description(persona: PersonaLike) -> str:
    """short_bio kalau ada, fallback tagline; > 150 karakter dipotong di batas kata + "…"."""
    p = _as_persona(persona)
    text = " ".join((p.short_bio if p.short_bio and p.short_bio.strip() else p.tagline).split())
    if len(text) <= BIO_MAX_CHARS:
        return text
    limit = BIO_MAX_CHARS - 3
    if text[limit] == " ":
        cut = text[:limit]
    else:
        head, sep, _tail = text[:limit].rpartition(" ")
        cut = head if sep else text[:limit]   # satu kata >147 karakter: potong keras
    return cut.rstrip() + "…"


# ---------------------------------------------------------------------------
# 3. Enrichment: demografis Reddit + 4 rating ordinal (1 panggilan LLM batch)
# ---------------------------------------------------------------------------

_ENRICHMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_oasis_enrichment (
    run_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    model TEXT NOT NULL,
    temperature_used REAL NOT NULL,
    attempt INTEGER NOT NULL,
    profiles_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL
);
"""

_ENRICHMENT_SYSTEM_PROMPT = """You are a casting director for a social-media investing simulation. You are given
8 FICTIONAL investor personas. For each one, assign plausible demographic details and a few coarse
behavioral ratings, inferred from the persona's own text. These are invented characters, not real
people. Do not rewrite or restate the personas."""

_ENRICHMENT_USER_HEAD = """Below are 8 fictional investor personas (index 0-7). For EACH persona, return demographics and
behavioral ratings that fit the character.

PERSONAS:
"""

_ENRICHMENT_USER_RULES = """
RULES:
- Judge each persona by their OWN text; do not give everyone a similar profile. Spread ages, genders,
  MBTI types and countries across the set the way a real community of retail and professional
  investors would be spread.
- age: integer 21-85.
- gender: exactly one of "male", "female", "other".
- mbti: exactly one of the 16 valid types (INTJ, INTP, ENTJ, ENTP, INFJ, INFP, ENFJ, ENFP, ISTJ, ISFJ,
  ESTJ, ESFJ, ISTP, ISFP, ESTP, ESFP), uppercase, consistent with the persona's temperament.
- country: English country name where the persona lives. They all follow the US stock market, so being
  US-based is common, but international investors (expats, foreign retail investors trading US equities)
  are equally plausible. Use a realistic mix across the 8 with at least 3 different countries. Treat a
  persona's name as only a weak hint and avoid stereotyping.
- posting_intensity 1-5: how often they post (1 = rarely, 5 = constantly).
- reactivity 1-5: how fast they react to news (1 = slow and deliberate, 5 = instant).
- optimism 1-5: default emotional tone (1 = habitually pessimistic, 3 = neutral, 5 = habitually upbeat).
- influence_level 1-5: how forcefully they command attention in a social feed, judged from
  communication_style and edge_vs_others (1 = easily ignored, 5 = dominates the conversation).
- Echo back the same index and name you were given for each item.

Return JSON:
{"profiles": [
  {"index": 0, "name": "...", "age": 34, "gender": "female", "mbti": "INTJ",
   "country": "United States", "posting_intensity": 3, "reactivity": 2,
   "optimism": 2, "influence_level": 3},
  ... exactly 8 items ...
]}"""


def _build_enrichment_prompt(personas: List[InvestorPersona]) -> Tuple[str, str]:
    blocks = []
    for index, p in enumerate(personas):
        blocks.append(
            f"[{index}] name: {p.name}\n"
            f"    philosophy_label: {p.philosophy_label}\n"
            f"    investment_philosophy: {p.investment_philosophy}\n"
            f"    personality: {p.personality}\n"
            f"    communication_style: {p.communication_style}\n"
            f"    edge_vs_others: {p.edge_vs_others}"
        )
    user = _ENRICHMENT_USER_HEAD + "\n".join(blocks) + "\n" + _ENRICHMENT_USER_RULES
    return _ENRICHMENT_SYSTEM_PROMPT, user


def _as_int(value: Any) -> Optional[int]:
    """int murni (bool ditolak); float bulat diterima."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _norm_country(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    country = value.strip()
    if not country or len(country) > COUNTRY_MAX_CHARS:
        return None
    if country.casefold() in _US_ALIASES:
        return "United States"
    return country


def _validate_enrichment(
    data: Any, personas: List[InvestorPersona]
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Validasi hard (8 poin dokumen 11a §3). Return (profiles, None) atau (None, pesan_spesifik)."""
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        return None, "validasi 2 gagal: key 'profiles' tidak ada atau bukan list"
    items = data["profiles"]
    if len(items) != len(personas):
        return None, f"validasi 2 gagal: expected {len(personas)} profiles, got {len(items)}"

    profiles: List[Dict[str, Any]] = []
    for position, (item, persona) in enumerate(zip(items, personas)):
        tag = f"profil #{position}"
        if not isinstance(item, dict):
            return None, f"validasi 3 gagal: {tag} bukan objek"
        if _as_int(item.get("index")) != position:
            return None, f"validasi 3 gagal: {tag} index={item.get('index')!r}, expected {position}"
        item_name = item.get("name")
        if not isinstance(item_name, str) or item_name.strip().casefold() != persona.name.strip().casefold():
            return None, f"validasi 3 gagal: {tag} name={item_name!r} != {persona.name!r}"
        age = _as_int(item.get("age"))
        if age is None or not (AGE_MIN <= age <= AGE_MAX):
            return None, f"validasi 4 gagal: {tag} age={item.get('age')!r} bukan int {AGE_MIN}-{AGE_MAX}"
        gender = item.get("gender")
        gender = gender.strip().casefold() if isinstance(gender, str) else None
        if gender not in GENDERS:
            return None, f"validasi 5 gagal: {tag} gender={item.get('gender')!r} bukan salah satu {GENDERS}"
        mbti = item.get("mbti")
        mbti = mbti.strip().upper() if isinstance(mbti, str) else None
        if mbti not in MBTI_TYPES:
            return None, f"validasi 6 gagal: {tag} mbti={item.get('mbti')!r} bukan salah satu dari 16 tipe"
        country = _norm_country(item.get("country"))
        if country is None:
            return None, f"validasi 7 gagal: {tag} country={item.get('country')!r} kosong/tipe salah/terlalu panjang"
        profile: Dict[str, Any] = {
            "index": position,
            "name": persona.name,
            "age": age,
            "gender": gender,
            "mbti": mbti,
            "country": country,
        }
        for field_name in _RATING_FIELDS:
            rating = _as_int(item.get(field_name))
            if rating is None or not (RATING_MIN <= rating <= RATING_MAX):
                return None, (
                    f"validasi 8 gagal: {tag} {field_name}={item.get(field_name)!r} "
                    f"bukan int {RATING_MIN}-{RATING_MAX}"
                )
            profile[field_name] = rating
        profiles.append(profile)
    return profiles, None


def _soft_checks(profiles: List[Dict[str, Any]]) -> List[str]:
    """Peringatan keberagaman (tidak memicu retry)."""
    warnings: List[str] = []
    if len({p["mbti"] for p in profiles}) < 4:
        warnings.append("keberagaman: kurang dari 4 tipe MBTI berbeda")
    if len({p["country"].casefold() for p in profiles}) < 3:
        warnings.append("keberagaman: kurang dari 3 negara berbeda")
    if len({p["gender"] for p in profiles}) == 1:
        warnings.append("keberagaman: semua persona bergender sama")
    for field_name in _RATING_FIELDS:
        if len({p[field_name] for p in profiles}) == 1:
            warnings.append(f"keberagaman: rating '{field_name}' sama di semua persona (indikasi collapse)")
    return warnings


def _connect_enrichment() -> sqlite3.Connection:
    path = _pg.PERSONA_DB_PATH   # dibaca saat panggilan (bukan saat import) agar bisa diisolasi di test
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_ENRICHMENT_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_persona_oasis_enrichment_run ON persona_oasis_enrichment (run_id, generated_at)")
    return conn


def _save_enrichment(result: Dict[str, Any]) -> None:
    conn = _connect_enrichment()
    try:
        conn.execute(
            "INSERT INTO persona_oasis_enrichment (run_id, generated_at, model, temperature_used, attempt, "
            "profiles_json, warnings_json) VALUES (?,?,?,?,?,?,?)",
            (
                result["run_id"], result["generated_at"], result["model"], result["temperature_used"],
                result["attempt"], json.dumps(result["profiles"], ensure_ascii=False),
                json.dumps(result["warnings"], ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load_enrichment(run_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect_enrichment()
    try:
        row = conn.execute(
            "SELECT generated_at, model, temperature_used, attempt, profiles_json, warnings_json "
            "FROM persona_oasis_enrichment WHERE run_id = ? ORDER BY generated_at DESC, rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "success": True,
        "run_id": run_id,
        "generated_at": row[0],
        "model": row[1],
        "temperature_used": row[2],
        "attempt": row[3],
        "profiles": json.loads(row[4]),
        "warnings": json.loads(row[5]),
        "from_cache": True,
    }


def get_or_generate_enrichment(
    run_id: str,
    personas: List[PersonaLike],
    force_regenerate: bool = False,
    max_attempts: int = MAX_ATTEMPTS,
) -> Dict[str, Any]:
    """
    Demografis Reddit + 4 rating ordinal untuk SEMUA persona lewat 1 panggilan LLM, disimpan per run_id
    (append-only) supaya persona run yang sama selalu mendapat profil yang sama.
    Gagal max_attempts kali -> {"success": False, "error": ...}: tanpa fallback statis, tanpa tulis persistence.
    """
    persona_objs = [_as_persona(p) for p in personas]
    if not force_regenerate:
        try:
            stored = _load_enrichment(run_id)
        except Exception as e:
            logger.warning(f"Gagal membaca enrichment tersimpan, generate baru: {e}")
            stored = None
        if stored is not None:
            logger.info(f"Enrichment cache hit run_id={run_id}")
            return stored

    try:
        client = _get_llm_client()
    except Exception as e:
        return {"success": False, "error": f"LLM client tidak tersedia: {e}"}
    system_prompt, user_prompt = _build_enrichment_prompt(persona_objs)
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
            )
            content = extract_chat_completion_text(response)
            finish_reason = getattr(response.choices[0], "finish_reason", None)
        except Exception as e:
            last_error = f"LLM call gagal: {e}"
            logger.warning(f"Enrichment attempt {attempt + 1}/{max_attempts}: {last_error[:120]}")
            if attempt < max_attempts - 1:
                time.sleep(1 * (attempt + 1))
            continue

        if finish_reason == "length":
            logger.warning(f"Enrichment attempt {attempt + 1}: output terpotong (finish_reason=length), mencoba perbaikan")

        data = _repair_json(content)
        if data is None:
            last_error = "validasi 1 gagal: JSON tidak valid dan tidak bisa diperbaiki"
            logger.warning(f"Enrichment attempt {attempt + 1}/{max_attempts}: {last_error}")
            continue

        profiles, err = _validate_enrichment(data, persona_objs)
        if err:
            last_error = err
            logger.warning(f"Enrichment attempt {attempt + 1}/{max_attempts}: {err}")
            continue

        warnings = _soft_checks(profiles)
        for w in warnings:
            logger.warning(f"Enrichment soft-check: {w}")

        result = {
            "success": True,
            "run_id": run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": Config.LLM_MODEL_NAME,
            "temperature_used": temperature,
            "attempt": attempt + 1,
            "profiles": profiles,
            "warnings": warnings,
            "from_cache": False,
        }
        try:
            _save_enrichment(result)
        except Exception as e:
            logger.warning(f"Gagal menyimpan enrichment run_id={run_id}: {e}")
            result["persisted"] = False
        else:
            result["persisted"] = True
        return result

    logger.error(f"get_or_generate_enrichment gagal setelah {max_attempts} attempt: {last_error}")
    return {"success": False, "error": f"gagal setelah {max_attempts} attempt; terakhir: {last_error}"}


# ---------------------------------------------------------------------------
# 4. AgentActivityConfig (murni)
# ---------------------------------------------------------------------------

def map_activity(
    enrichment_profiles: List[Dict[str, Any]],
    personas: List[PersonaLike],
    run_id: str,
) -> List[Dict[str, Any]]:
    """
    Satu entri AgentActivityConfig per persona (agent_id = index). Hanya agent_id, entity_name,
    activity_level, active_hours yang DIBACA runner; sisanya dead config (lihat _DEAD_CONFIG_NOTICE).
    activity_level=1.0 dan active_hours=24 jam untuk SEMUA agent (keputusan, bukan turunan rating).
    """
    persona_objs = [_as_persona(p) for p in personas]
    if len(enrichment_profiles) != len(persona_objs):
        raise ValueError("enrichment_profiles dan personas harus sama panjang")
    configs: List[Dict[str, Any]] = []
    for agent_id, (profile, persona) in enumerate(zip(enrichment_profiles, persona_objs)):
        posting = profile["posting_intensity"]
        delay_min, delay_max = _RESPONSE_DELAY[profile["reactivity"]]
        configs.append({
            "agent_id": agent_id,
            "entity_uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{agent_id}")),
            "entity_name": persona.name,
            "entity_type": ENTITY_TYPE,
            "activity_level": ACTIVITY_LEVEL,
            "posts_per_hour": round(0.2 * posting, 1),
            "comments_per_hour": round(0.4 * posting, 1),
            "active_hours": list(ACTIVE_HOURS),
            "response_delay_min": delay_min,
            "response_delay_max": delay_max,
            "sentiment_bias": round((profile["optimism"] - 3) * 0.25, 2),
            "stance": "neutral",
            "influence_weight": _INFLUENCE_WEIGHT[profile["influence_level"]],
        })
    return configs


def build_time_config(n_agents: int) -> Dict[str, Any]:
    """
    5 jam x 30 menit = PERSIS 10 round tanpa max_rounds. peak/off_peak/morning/work_hours HARUS list kosong
    (bukan dihapus) dan semua multiplier 1.0: tanpa itu get_active_agents_for_round memakai default MiroFish
    (off-peak multiplier) dan bisa mengaktifkan 0 agent. agents_per_hour min=max=n_agents -> semua agent aktif.
    """
    if n_agents != PERSONA_COUNT:
        raise ValueError(f"build_time_config mengharapkan tepat {PERSONA_COUNT} agent, dapat {n_agents}")
    return {
        "total_simulation_hours": TOTAL_SIMULATION_HOURS,
        "minutes_per_round": MINUTES_PER_ROUND,
        "agents_per_hour_min": n_agents,
        "agents_per_hour_max": n_agents,
        "peak_hours": [],
        "peak_activity_multiplier": 1.0,
        "off_peak_hours": [],
        "off_peak_activity_multiplier": 1.0,
        "morning_hours": [],
        "morning_activity_multiplier": 1.0,
        "work_hours": [],
        "work_activity_multiplier": 1.0,
    }


# ---------------------------------------------------------------------------
# 8. initial_posts (murni)
# ---------------------------------------------------------------------------

def _post_content(article: Dict[str, Any]) -> str:
    """"{headline} ${ticker} — via {publisher} ({tanggal})"; publisher kosong -> bagian " — via …" dibuang."""
    text = f"{article['headline']} ${article['ticker']}"
    publisher = (article.get("publisher") or "").strip()
    if publisher:
        text += f" — via {publisher}"
    date = (article.get("published_at") or "")[:10]
    if date:
        text += f" ({date})"
    return text


def build_event_config(
    personas: List[PersonaLike],
    agent_configs: List[Dict[str, Any]],
    sample_articles: Optional[List[Dict[str, Any]]],
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Return (event_config, warnings). Maks 4 initial_posts dari headline tersimpan (verbatim, tanpa LLM),
    satu artikel per ticker berbeda (published_at desc, tie article_id asc); artikel ke-k diposting agent
    ke-k menurut (-influence_weight, agent_id) — SETIAP post dari agent berbeda karena skrip runner
    menyimpan initial post sebagai dict[agent] (post kedua dari agent yang sama menimpa yang pertama).
    Tanpa artikel -> 1 post statis dari agent berpengaruh tertinggi + warning "no_news_seed".
    """
    if not agent_configs:
        raise ValueError("agent_configs kosong")
    ranked = sorted(agent_configs, key=lambda c: (-c["influence_weight"], c["agent_id"]))
    warnings: List[str] = []

    articles = [
        a for a in (sample_articles or [])
        if a.get("headline") and a.get("ticker")
    ]
    by_id = sorted(articles, key=lambda a: (a.get("article_id") is None, a.get("article_id")))
    by_recency = sorted(by_id, key=lambda a: a.get("published_at") or "", reverse=True)   # stabil: tie -> article_id naik
    chosen: List[Dict[str, Any]] = []
    seen_tickers = set()
    for article in by_recency:
        if article["ticker"] in seen_tickers:
            continue
        seen_tickers.add(article["ticker"])
        chosen.append(article)
        if len(chosen) == MAX_INITIAL_POSTS:
            break

    if chosen:
        initial_posts = [
            {"content": _post_content(article), "poster_type": POSTER_TYPE, "poster_agent_id": ranked[k]["agent_id"]}
            for k, article in enumerate(chosen[: len(ranked)])
        ]
    else:
        warnings.append(WARNING_NO_NEWS_SEED)
        logger.warning("Tidak ada headline tersimpan untuk initial_posts; memakai 1 post statis (no_news_seed)")
        initial_posts = [
            {"content": FALLBACK_SEED_POST, "poster_type": POSTER_TYPE, "poster_agent_id": ranked[0]["agent_id"]}
        ]

    event_config = {
        "initial_posts": initial_posts,
        "scheduled_events": [],
        "hot_topics": [],
        "narrative_direction": "",
    }
    return event_config, warnings


# ---------------------------------------------------------------------------
# 7. Orkestrasi: bangun di memori, tulis atomik
# ---------------------------------------------------------------------------

def _platform_config(platform: str) -> Dict[str, Any]:
    """PlatformConfig default MiroFish — DEAD CONFIG (recsys sebenarnya hard-coded di OasisEnv)."""
    return {
        "platform": platform,
        "recency_weight": 0.4,
        "popularity_weight": 0.3,
        "relevance_weight": 0.3,
        "viral_threshold": 10,
        "echo_chamber_strength": 0.5,
    }


def _write_atomic(path: str, writer) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


WARNING_ENRICHMENT_NOT_PERSISTED = "hasil enrichment gagal disimpan ke persistence (persisted=False)"


def _collect_warnings(
    personas_result: Dict[str, Any],
    enrichment: Dict[str, Any],
    event_warnings: List[str],
) -> List[str]:
    """
    Semua sumber warning, diberi prefix asal, urutan = alur pipeline: persona (Fase 10) -> enrichment -> event.
    Hanya persisted == False yang memicu warning enrichment (hasil cache tidak membawa key itu).
    """
    warnings = [f"persona: {w}" for w in (personas_result.get("warnings") or [])]
    warnings += [f"enrichment: {w}" for w in (enrichment.get("warnings") or [])]
    if enrichment.get("persisted") is False:
        warnings.append(f"enrichment: {WARNING_ENRICHMENT_NOT_PERSISTED}")
    warnings += [f"event: {w}" for w in event_warnings]
    return warnings


def _build_artifacts_in_memory(
    personas_result: Dict[str, Any],
    enrichment: Dict[str, Any],
    simulation_id: str,
) -> Tuple[Dict[str, Any], List[str]]:
    personas = [_as_persona(p) for p in personas_result["personas"]]
    if len(personas) != PERSONA_COUNT:
        raise ValueError(f"expected {PERSONA_COUNT} personas, got {len(personas)}")
    run_id = personas_result["run_id"]
    profiles = enrichment["profiles"]

    usernames = derive_usernames(personas)
    texts = [compose_persona_text(p) for p in personas]
    descriptions = [short_description(p) for p in personas]
    created_date = datetime.now().strftime("%Y-%m-%d")

    twitter_rows = [
        [agent_id, p.name, usernames[agent_id], texts[agent_id], descriptions[agent_id]]
        for agent_id, p in enumerate(personas)
    ]
    reddit_profiles = [
        {
            "user_id": agent_id,
            "username": usernames[agent_id],
            "name": p.name,
            "bio": descriptions[agent_id],
            "persona": texts[agent_id],
            "karma": 1000,
            "created_at": created_date,
            "age": profiles[agent_id]["age"],
            "gender": profiles[agent_id]["gender"],
            "mbti": profiles[agent_id]["mbti"],
            "country": profiles[agent_id]["country"],
        }
        for agent_id, p in enumerate(personas)
    ]

    agent_configs = map_activity(profiles, personas, run_id)
    time_config = build_time_config(len(personas))
    event_config, event_warnings = build_event_config(
        personas, agent_configs, personas_result.get("sample_articles") or []
    )
    warnings = _collect_warnings(personas_result, enrichment, event_warnings)
    now_iso = datetime.now().isoformat()

    simulation_config = {
        "simulation_id": simulation_id,
        "project_id": "",
        "graph_id": "",
        "simulation_requirement": _SIMULATION_REQUIREMENT,
        "time_config": time_config,
        "agent_configs": agent_configs,
        "event_config": event_config,
        "social_graph": {"follow_network": "complete"},
        "twitter_config": _platform_config("twitter"),
        "reddit_config": _platform_config("reddit"),
        "llm_model": Config.LLM_MODEL_NAME,
        "llm_base_url": Config.LLM_BASE_URL,
        "generated_at": now_iso,
        "generation_reasoning": (
            f"Generated by persona_oasis_adapter from persona run {run_id} (no Zep). {_DEAD_CONFIG_NOTICE}"
        ),
        "persona_run": {
            "run_id": run_id,
            "as_of_date": personas_result.get("as_of_date"),
            "screening": personas_result.get("screening"),
            "generated_at": personas_result.get("generated_at"),
            "grounding": personas_result.get("grounding"),
            "warnings": list(warnings),     # selalu ada (list kosong bila tidak ada warning)
        },
    }
    state = {
        "simulation_id": simulation_id,
        "project_id": "",
        "graph_id": "",
        "enable_twitter": True,
        "enable_reddit": True,
        "status": "ready",
        "entities_count": len(personas),
        "profiles_count": len(personas),
        "entity_types": [ENTITY_TYPE],
        "profiles_generated": True,
        "config_generated": True,
        "config_reasoning": f"Generated by persona_oasis_adapter from persona run {run_id}; no Zep.",
        "current_round": 0,
        "twitter_status": "not_started",
        "reddit_status": "not_started",
        "created_at": now_iso,
        "updated_at": now_iso,
        "error": None,
    }
    return {
        "twitter_rows": twitter_rows,
        "reddit_profiles": reddit_profiles,
        "simulation_config": simulation_config,
        "state": state,
    }, warnings


def build_oasis_artifacts(
    personas_result: Dict[str, Any],
    simulation_id: Optional[str] = None,
    sim_root: Optional[str] = None,
    force_regenerate_enrichment: bool = False,
) -> Dict[str, Any]:
    """
    Persona run (keluaran get_or_generate_personas) -> 4 artefak OASIS. All-or-nothing: gagal sebelum
    penulisan (persona gagal / enrichment gagal / validasi) -> {"success": False, "error"} TANPA IO.
    Penulisan atomik per file dengan urutan tetap; state.json TERAKHIR karena ia yang menandai kesiapan.
    """
    if not isinstance(personas_result, dict) or not personas_result.get("success"):
        error = personas_result.get("error") if isinstance(personas_result, dict) else None
        return {"success": False, "error": f"personas_result tidak sukses: {error or 'tidak ada detail'}"}

    simulation_id = simulation_id or f"sim_{uuid.uuid4().hex[:12]}"
    if not _SIM_ID_RE.match(simulation_id):
        return {"success": False, "error": f"simulation_id tidak valid: {simulation_id!r}"}

    try:
        run_id = personas_result["run_id"]
        enrichment = get_or_generate_enrichment(
            run_id, personas_result["personas"], force_regenerate=force_regenerate_enrichment
        )
        if not enrichment.get("success"):
            return {"success": False, "error": f"enrichment gagal: {enrichment.get('error')}"}
        built, warnings = _build_artifacts_in_memory(personas_result, enrichment, simulation_id)
    except Exception as e:
        logger.error(f"build_oasis_artifacts gagal sebelum penulisan: {e}")
        return {"success": False, "error": f"gagal membangun artefak: {e}"}

    root = sim_root or Config.OASIS_SIMULATION_DATA_DIR
    sim_dir = os.path.join(root, simulation_id)
    for existing in ("state.json", "simulation_config.json"):
        if os.path.exists(os.path.join(sim_dir, existing)):
            return {"success": False, "error": f"{simulation_id} sudah punya {existing}; tidak menimpa simulasi yang ada"}

    def write_csv(f):
        writer = csv.writer(f)
        writer.writerow(["user_id", "name", "username", "user_char", "description"])
        writer.writerows(built["twitter_rows"])

    def write_json(payload):
        return lambda f: json.dump(payload, f, ensure_ascii=False, indent=2)

    writers = {
        "twitter_profiles.csv": write_csv,
        "reddit_profiles.json": write_json(built["reddit_profiles"]),
        "simulation_config.json": write_json(built["simulation_config"]),
        "state.json": write_json(built["state"]),
    }
    assert tuple(writers) == _ARTIFACT_ORDER

    created_dir = not os.path.exists(sim_dir)
    written: List[str] = []
    try:
        os.makedirs(sim_dir, exist_ok=True)
        for filename in _ARTIFACT_ORDER:
            path = os.path.join(sim_dir, filename)
            _write_atomic(path, writers[filename])
            written.append(path)
    except Exception as e:
        logger.error(f"Penulisan artefak gagal untuk {simulation_id}: {e}")
        for path in written:
            try:
                os.remove(path)
            except OSError:
                pass
        if created_dir:
            try:
                os.rmdir(sim_dir)
            except OSError:
                pass
        return {"success": False, "error": f"penulisan artefak gagal: {e}"}

    logger.info(f"Artefak OASIS ditulis: {sim_dir} (persona run {run_id})")
    return {
        "success": True,
        "simulation_id": simulation_id,
        "sim_dir": sim_dir,
        "files": {name: os.path.join(sim_dir, name) for name in _ARTIFACT_ORDER},
        "warnings": warnings,
        "persona_run_id": run_id,
        "as_of_date": personas_result.get("as_of_date"),
    }
