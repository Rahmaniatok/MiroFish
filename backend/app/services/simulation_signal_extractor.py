"""
Fase 11b/11c (digabung) — Ekstraksi sinyal stance per (persona, ticker) dari `actions.jsonl` hasil
simulasi OASIS (Fase 11a). Desain: docs/design/fase11b_signal_extraction.md.

Alur (dipanggil berurutan oleh extract_simulation_signals, orkestrasi §6):
    1. dedup_agent_content       — §1: exact byte-match per (agent_id, teks), lintas seluruh simulasi.
    2. extract_signals_batch     — §3 opsi (c): 1 panggilan LLM batch per platform, pola index+echo-back
                                    PERSIS get_or_generate_enrichment (persona_oasis_adapter.py).
    3. filter_ticker_mentions    — keputusan reviewer (bukan bagian dokumen 11b asli): ticker di luar
                                    universe screening DIBUANG dari sinyal utama TAPI TETAP DI-LOG/di-
                                    kembalikan sebagai discarded (audit trail).
    4. build_persona_ticker_signals — §4/§6: agregasi riwayat mentah (bukan pra-kompresi 1 angka) per
                                    (persona, ticker); ticker yang tidak pernah disebut -> key TIDAK ADA.

Catatan rekonsiliasi skema (bukan penyimpangan diam-diam dari dokumen 11b): dokumen desain
menyimpan `history` per KONTEN UNIK (dengan field `rounds`, list). Instruksi implementasi (hari ini,
lebih konkret) memberi skema literal `"history": [{round, platform, stance, score, agent_id,
source_text_hash}]` — SATU baris per ROUND, bukan per konten unik. Modul ini mengikuti skema literal
instruksi implementasi (lebih baru/konkret) sebagai yang berlaku: dedup tetap memangkas BIAYA LLM
(satu konten unik = satu ekstraksi, bukan diulang N kali — lihat extract_signals_batch), tapi hasil
akhir `history` tetap merentang per-round supaya Fase 12 melihat pola durasi/recency secara langsung.
`evidence_count` dihitung dari jumlah `source_text_hash` BERBEDA (bukan `len(history)`), supaya tetap
tidak menghitung ganda repetisi verbatim sesuai keputusan §4 dokumen ("Isaac->GOOGL = 1, bukan 7").
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import Config
from ..utils.logger import get_logger
from ..utils.openai_chat_compat import create_chat_completion, extract_chat_completion_text
from .persona_generator import _get_llm_client, _repair_json, _temperature_for_attempt

logger = get_logger('mirofish.simulation_signal_extractor')

# --- §1 dedup ---
_CONTENT_FIELD_BY_ACTION_TYPE = {
    "CREATE_POST": "content",
    "CREATE_COMMENT": "content",
    "QUOTE_POST": "quote_content",
}

# --- §3 ekstraksi ---
MAX_ATTEMPTS = 3
STANCES = ("bullish", "bearish", "neutral", "mixed")
_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")
_TEXT_PREVIEW_LEN = 20

# --- §7 discard reason ---
REASON_TICKER_OUT_OF_UNIVERSE = "ticker_out_of_universe"


# ---------------------------------------------------------------------------
# Struktur data
# ---------------------------------------------------------------------------

@dataclass
class DedupedContent:
    """1 opini unik (byte-identik) dari 1 agent, lintas seluruh simulasi. `rounds` selalu urut naik."""
    agent_id: int
    agent_name: str
    platform: str
    action_type: str
    text: str
    rounds: List[int] = field(default_factory=list)

    @property
    def occurrence_count(self) -> int:
        return len(self.rounds)

    def source_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:8]


@dataclass
class ExtractedContentSignals:
    """Hasil ekstraksi 1 DedupedContent: list sinyal ticker (kosong bila tidak menyebut ticker apa pun)."""
    content: DedupedContent
    tickers: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DiscardedSignal:
    """1 mention ticker yang dibuang karena di luar universe (§7 + keputusan reviewer hari ini)."""
    ticker: str
    agent_id: int
    agent_name: str
    platform: str
    rounds: List[int]
    stance: str
    score: float
    reason: str = REASON_TICKER_OUT_OF_UNIVERSE


# ---------------------------------------------------------------------------
# 1. Dedup (docs/design/fase11b_signal_extraction.md §1)
# ---------------------------------------------------------------------------

def dedup_agent_content(actions: List[Dict[str, Any]], agent_id: int) -> List[DedupedContent]:
    """
    Dedup exact byte-match (hanya `strip()` whitespace ujung, TANPA case-fold) per (agent_id, teks),
    LINTAS SELURUH `actions` (bukan per-window waktu) — §1. `actions` = baris `actions.jsonl` apa
    adanya (event round_start/round_end/simulation_* boleh ada di dalamnya, diabaikan di sini), dengan
    key `"platform"` SUDAH ditambahkan oleh pemanggil (lihat `_load_actions_file`).

    Round 0 DIKELUARKAN: itu `ManualAction` seed (FOLLOW graf lengkap + initial CREATE_POST berisi
    headline berita asli disalin verbatim oleh Fase 11a), bukan opini persona — §1 "Dedup TIDAK berlaku
    untuk round 0".

    Kunci dedup TIDAK menyertakan `action_type` (lihat docstring modul §1): opini yang sama persis yang
    diulang lewat `action_type` berbeda tetaplah opini yang sama. `action_type`/`platform` yang disimpan
    di grup adalah milik kemunculan PERTAMA (kasus beda `action_type` untuk teks identik dari agent yang
    sama tidak pernah teramati di data nyata — edge case yang disengaja diterima, bukan diabaikan tanpa
    sadar).
    """
    groups: Dict[str, DedupedContent] = {}
    for action in actions:
        if "event_type" in action:
            continue
        if action.get("agent_id") != agent_id:
            continue
        if action.get("round", 0) == 0:
            continue
        action_type = action.get("action_type")
        field_name = _CONTENT_FIELD_BY_ACTION_TYPE.get(action_type)
        if not field_name:
            continue
        text = (action.get("action_args") or {}).get(field_name)
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        round_num = action.get("round")
        existing = groups.get(text)
        if existing is None:
            groups[text] = DedupedContent(
                agent_id=agent_id,
                agent_name=action.get("agent_name", ""),
                platform=action.get("platform", ""),
                action_type=action_type,
                text=text,
                rounds=[round_num],
            )
        elif round_num not in existing.rounds:
            existing.rounds.append(round_num)

    result = list(groups.values())
    for dc in result:
        dc.rounds.sort()
    return result


def _all_agent_ids(actions: List[Dict[str, Any]]) -> List[int]:
    ids = {
        a.get("agent_id") for a in actions
        if "event_type" not in a and a.get("agent_id") is not None
    }
    return sorted(ids)


def dedup_all_content(actions: List[Dict[str, Any]]) -> List[DedupedContent]:
    """`dedup_agent_content` diterapkan ke SEMUA agent yang muncul di `actions` (satu platform)."""
    result: List[DedupedContent] = []
    for agent_id in _all_agent_ids(actions):
        result.extend(dedup_agent_content(actions, agent_id))
    return result


# ---------------------------------------------------------------------------
# 3. Ekstraksi sinyal batch per platform (docs/design/fase11b_signal_extraction.md §3 opsi c)
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = """You are analyzing posts/comments written by FICTIONAL investor personas in a
social-media market simulation. For each numbered item, identify which stock tickers (written as
$TICKER) it mentions and the STANCE the writer expresses toward each one. These are fictional opinions
inside a simulation, not real financial advice or verifiable claims about the real world — judge only
the stance actually expressed in the text itself; nothing needs to be fact-checked."""

_EXTRACTION_USER_RULES = """
For EACH numbered item below, return exactly one result object with:
- "index": the same number given to the item (echo it back exactly).
- "text_preview": the FIRST 20 CHARACTERS of the item's text, EXACTLY as given (used to verify you are
  answering about the right item).
- "tickers": a list of {"ticker": "SYMBOL", "stance": "bullish"|"bearish"|"neutral"|"mixed", "score":
  a float from -1.0 (very bearish) to 1.0 (very bullish)} — one entry PER DISTINCT ticker mentioned in
  the item. A hedged/cautious/two-sided framing should use "neutral" or "mixed" with a score near 0,
  not be forced into "bullish" or "bearish". If the item mentions no ticker, return "tickers": [].
- Judge the stance from THIS item's own text only.

Return JSON: {"results": [{"index": 0, "text_preview": "...", "tickers": [...]}, ... exactly N items ...]}
"""


def _build_extraction_prompt(contents: List[DedupedContent]) -> Tuple[str, str]:
    lines = [
        f"[{i}] agent: {c.agent_name}\n    text: {c.text}"
        for i, c in enumerate(contents)
    ]
    user = (
        f"Below are {len(contents)} fictional social-media items (index 0-{len(contents) - 1}), each "
        f"mentioning at least one $TICKER.\n\n" + "\n".join(lines) + "\n" + _EXTRACTION_USER_RULES
    )
    return _EXTRACTION_SYSTEM_PROMPT, user


def _validate_extraction_response(
    data: Any, contents: List[DedupedContent]
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Validasi hard, pola index+echo-back PERSIS `_validate_enrichment` (persona_oasis_adapter.py):
    gagal -> (None, pesan_spesifik), yang memicu retry SELURUH batch (bukan diterima sebagian)."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return None, "validasi gagal: key 'results' tidak ada atau bukan list"
    items = data["results"]
    if len(items) != len(contents):
        return None, f"validasi gagal: expected {len(contents)} results, got {len(items)}"

    parsed: List[Dict[str, Any]] = []
    for position, (item, content) in enumerate(zip(items, contents)):
        tag = f"item #{position}"
        if not isinstance(item, dict):
            return None, f"validasi gagal: {tag} bukan objek"
        if item.get("index") != position:
            return None, f"validasi gagal: {tag} index={item.get('index')!r}, expected {position}"
        preview = item.get("text_preview")
        expected_preview = content.text[:_TEXT_PREVIEW_LEN]
        if not isinstance(preview, str) or preview != expected_preview:
            return None, (
                f"validasi gagal: {tag} text_preview={preview!r} != {expected_preview!r} "
                f"(kemungkinan atribusi silang antar-item)"
            )
        tickers = item.get("tickers")
        if not isinstance(tickers, list):
            return None, f"validasi gagal: {tag} 'tickers' bukan list"
        clean_tickers: List[Dict[str, Any]] = []
        for t in tickers:
            if not isinstance(t, dict):
                return None, f"validasi gagal: {tag} entri ticker bukan objek"
            ticker = t.get("ticker")
            if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z]{1,5}", ticker.strip()):
                return None, f"validasi gagal: {tag} ticker={t.get('ticker')!r} tidak valid"
            stance = t.get("stance")
            stance = stance.strip().casefold() if isinstance(stance, str) else None
            if stance not in STANCES:
                return None, f"validasi gagal: {tag} stance={t.get('stance')!r} bukan salah satu {STANCES}"
            score = t.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not (-1.0 <= float(score) <= 1.0):
                return None, f"validasi gagal: {tag} score={t.get('score')!r} bukan float -1..1"
            clean_tickers.append({"ticker": ticker.strip().upper(), "stance": stance, "score": float(score)})
        parsed.append({"content": content, "tickers": clean_tickers})
    return parsed, None


def extract_signals_batch(deduped_contents: List[DedupedContent], platform: str) -> Dict[str, Any]:
    """
    1 panggilan LLM batch untuk SEMUA `deduped_contents` di `platform` ini yang menyebut >=1 $TICKER
    (§3 opsi c). Konten tanpa ticker tidak dikirim ke LLM (tidak ada yang diekstrak untuknya — langsung
    `tickers=[]`). Retry SELURUH batch bila validasi index/text_preview gagal (kemungkinan atribusi
    silang), max `MAX_ATTEMPTS` kali, temperature mengikuti `_temperature_for_attempt` (Fase 10, di-
    import — tidak disalin ulang). Gagal setelah `MAX_ATTEMPTS` -> `{"success": False, "error": ...}`
    untuk platform ini SAJA, TANPA fallback diam-diam (pola sama seperti `get_or_generate_enrichment`).

    Return: {"success": True, "platform": ..., "results": List[ExtractedContentSignals], "attempt": int}
         atau {"success": False, "platform": ..., "error": str}
    """
    with_ticker: List[DedupedContent] = []
    results: List[ExtractedContentSignals] = []
    for c in deduped_contents:
        if _TICKER_RE.search(c.text):
            with_ticker.append(c)
        else:
            results.append(ExtractedContentSignals(content=c, tickers=[]))

    if not with_ticker:
        return {"success": True, "platform": platform, "results": results, "attempt": 0}

    try:
        client = _get_llm_client()
    except Exception as e:
        return {"success": False, "platform": platform, "error": f"LLM client tidak tersedia: {e}"}

    system_prompt, user_prompt = _build_extraction_prompt(with_ticker)
    last_error = "tidak ada attempt dijalankan"

    for attempt in range(MAX_ATTEMPTS):
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
            content_text = extract_chat_completion_text(response)
            finish_reason = getattr(response.choices[0], "finish_reason", None)
        except Exception as e:
            last_error = f"LLM call gagal: {e}"
            logger.warning(f"Ekstraksi sinyal {platform} attempt {attempt + 1}/{MAX_ATTEMPTS}: {last_error[:150]}")
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(1 * (attempt + 1))
            continue

        if finish_reason == "length":
            logger.warning(
                f"Ekstraksi sinyal {platform} attempt {attempt + 1}: output terpotong "
                f"(finish_reason=length), mencoba perbaikan"
            )

        data = _repair_json(content_text)
        if data is None:
            last_error = "JSON tidak valid dan tidak bisa diperbaiki"
            logger.warning(f"Ekstraksi sinyal {platform} attempt {attempt + 1}/{MAX_ATTEMPTS}: {last_error}")
            continue

        parsed, err = _validate_extraction_response(data, with_ticker)
        if err:
            last_error = err
            logger.warning(f"Ekstraksi sinyal {platform} attempt {attempt + 1}/{MAX_ATTEMPTS}: {err}")
            continue

        for item in parsed:
            results.append(ExtractedContentSignals(content=item["content"], tickers=item["tickers"]))
        return {"success": True, "platform": platform, "results": results, "attempt": attempt + 1}

    logger.error(f"extract_signals_batch({platform}) gagal setelah {MAX_ATTEMPTS} attempt: {last_error}")
    return {
        "success": False,
        "platform": platform,
        "error": f"gagal setelah {MAX_ATTEMPTS} attempt; terakhir: {last_error}",
    }


# ---------------------------------------------------------------------------
# Filter allow-list ticker (keputusan reviewer, bukan bagian dokumen 11b asli)
# ---------------------------------------------------------------------------

def filter_ticker_mentions(
    extracted_signals: List[ExtractedContentSignals],
    allowed_tickers: Iterable[str],
) -> Tuple[List[ExtractedContentSignals], List[DiscardedSignal]]:
    """
    Buang mention ticker DI LUAR `allowed_tickers` (universe hasil `screen_universe` run ini) dari
    sinyal utama, TAPI kembalikan terpisah sebagai `DiscardedSignal` (audit trail — TIDAK hilang tanpa
    jejak) + log WARNING per ticker+agent+round. Pola sama seperti Fase 9 "unknown ticker mention":
    dibuang dari DATA, tidak dari LOG.
    """
    allowed = {t.strip().upper() for t in allowed_tickers}
    kept: List[ExtractedContentSignals] = []
    discarded: List[DiscardedSignal] = []
    for sig in extracted_signals:
        c = sig.content
        keep_tickers = []
        for t in sig.tickers:
            ticker = t["ticker"]
            if ticker in allowed:
                keep_tickers.append(t)
            else:
                logger.warning(
                    f"Ticker di luar universe dibuang dari sinyal: ticker={ticker} agent_id={c.agent_id} "
                    f"agent_name={c.agent_name} platform={c.platform} rounds={c.rounds}"
                )
                discarded.append(DiscardedSignal(
                    ticker=ticker, agent_id=c.agent_id, agent_name=c.agent_name, platform=c.platform,
                    rounds=list(c.rounds), stance=t["stance"], score=t["score"],
                ))
        kept.append(ExtractedContentSignals(content=c, tickers=keep_tickers))
    return kept, discarded


# ---------------------------------------------------------------------------
# 4. Agregasi persona -> ticker -> trajectory (docs/design/fase11b_signal_extraction.md §4/§6)
# ---------------------------------------------------------------------------

def build_persona_ticker_signals(
    extracted_signals_per_platform: Dict[str, List[ExtractedContentSignals]],
    discarded_signals: List[DiscardedSignal],
) -> Dict[str, Any]:
    """
    Agregasi RIWAYAT MENTAH (bukan pra-kompresi ke 1 angka) per (persona, ticker) — §4. Persona yang
    tidak pernah menyebut suatu ticker: TIDAK ADA key untuk ticker itu (ketiadaan entry = "tidak ada
    opini", bukan `stance="neutral"` default).

    `history` per ticker adalah list per-ROUND (satu baris per round kemunculan, bisa berbagi
    `source_text_hash` yang sama bila berasal dari konten yang sama yang terulang — lihat catatan
    rekonsiliasi skema di docstring modul). `evidence_count` dihitung dari jumlah `source_text_hash`
    BERBEDA (jumlah KONTEN UNIK), BUKAN `len(history)` — supaya repetisi verbatim tidak dihitung ganda
    sesuai §4 ("Isaac->GOOGL = 1, bukan 7").

    Return: {"personas": {...}, "universe": {"tickers_discussed": [...],
             "tickers_discarded_out_of_universe": [...]}, "deduped_content_by_hash": {...}}
    (`tickers_silent` BELUM dihitung di sini — perlu `allowed_tickers` penuh yang tidak jadi parameter
    fungsi ini; dihitung oleh `extract_simulation_signals` yang memang memilikinya — §7.)
    """
    personas: Dict[str, Dict[str, Any]] = {}
    deduped_by_hash: Dict[str, Dict[str, Any]] = {}

    for sig_list in extracted_signals_per_platform.values():
        for sig in sig_list:
            if not sig.tickers:
                continue
            c = sig.content
            h = c.source_hash()
            deduped_by_hash[h] = {
                "text": c.text,
                "agent_id": c.agent_id,
                "agent_name": c.agent_name,
                "platform": c.platform,
                "action_type": c.action_type,
                "rounds": list(c.rounds),
            }
            persona_dict = personas.setdefault(c.agent_name, {})
            for t in sig.tickers:
                ticker_dict = persona_dict.setdefault(t["ticker"], {"history": []})
                for round_num in c.rounds:
                    ticker_dict["history"].append({
                        "round": round_num,
                        "platform": c.platform,
                        "stance": t["stance"],
                        "score": t["score"],
                        "agent_id": c.agent_id,
                        "source_text_hash": h,
                    })

    tickers_discussed = set()
    for persona_dict in personas.values():
        for ticker, tdict in persona_dict.items():
            history = sorted(tdict["history"], key=lambda item: item["round"])
            tdict["history"] = history
            latest = history[-1]
            tdict["latest_stance"] = latest["stance"]
            tdict["latest_score"] = latest["score"]
            tdict["evidence_count"] = len({item["source_text_hash"] for item in history})
            tdict["mentioned_in_rounds"] = sorted({item["round"] for item in history})
            tickers_discussed.add(ticker)

    return {
        "personas": personas,
        "universe": {
            "tickers_discussed": sorted(tickers_discussed),
            "tickers_discarded_out_of_universe": sorted({d.ticker for d in discarded_signals}),
        },
        "deduped_content_by_hash": deduped_by_hash,
    }


# ---------------------------------------------------------------------------
# 6. Orkestrasi
# ---------------------------------------------------------------------------

def _write_atomic(path: str, writer) -> None:
    """Sama persis pola `_write_atomic` di `persona_oasis_adapter.py` (build_oasis_artifacts)."""
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


def _load_actions_file(sim_dir: str, platform: str) -> List[Dict[str, Any]]:
    """Baca `<sim_dir>/<platform>/actions.jsonl`, tambahkan `"platform"` ke tiap baris (dipakai
    `dedup_agent_content` untuk mengisi `DedupedContent.platform`). Raise `FileNotFoundError` /
    `json.JSONDecodeError` apa adanya -> ditangkap pemanggil (`extract_simulation_signals`)."""
    path = os.path.join(sim_dir, platform, "actions.jsonl")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    actions: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            d["platform"] = platform
            actions.append(d)
    return actions


def _derive_allowed_tickers(sim_dir: str) -> List[str]:
    """
    Universe hasil `screen_universe` untuk run ini, diturunkan dari PARAMETER yang tersimpan di
    `simulation_config.json` (`persona_run.screening` + `persona_run.as_of_date`). `screen_universe`
    DIPANGGIL ULANG (bukan dibaca dari cache) karena Fase 11a hanya menyimpan parameter screening, bukan
    daftar ticker hasil screening itu sendiri (lihat `docs/design/fase11b_signal_extraction.md`,
    keputusan implementasi). Dipisah dari `extract_simulation_signals` supaya pemanggil bisa melewatinya
    lewat parameter `allowed_tickers` eksplisit (mis. di test, untuk menghindari panggilan yfinance
    sungguhan).
    """
    config_path = os.path.join(sim_dir, "simulation_config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    persona_run = config.get("persona_run") or {}
    screening = persona_run.get("screening") or {}
    from ..data_layer.universe import screen_universe
    universe = screen_universe(
        sectors=screening.get("sectors"),
        market_cap_tiers=screening.get("market_cap_tiers"),
        as_of_date=persona_run.get("as_of_date"),
    )
    return [u["ticker"] for u in universe]


def _read_persona_run_id(sim_dir: str) -> Optional[str]:
    """Best-effort: `persona_run_id` untuk metadata output, TIDAK wajib ada (lihat catatan di
    `extract_simulation_signals` — test yang memasok `allowed_tickers` eksplisit tidak perlu fixture
    `simulation_config.json`)."""
    config_path = os.path.join(sim_dir, "simulation_config.json")
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return (json.load(f).get("persona_run") or {}).get("run_id")
    except Exception:
        return None


def extract_simulation_signals(
    simulation_id: str,
    sim_dir: Optional[str] = None,
    allowed_tickers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Orkestrasi penuh §1-§7: baca `actions.jsonl` kedua platform -> dedup -> ekstraksi LLM batch per
    platform -> filter allow-list ticker -> agregasi persona->ticker -> tulis `signal_extraction.json`
    (atomik, pola sama seperti `build_oasis_artifacts`).

    `actions.jsonl` salah satu platform tidak ada/rusak -> `{"success": False, "error": ...}` TANPA
    tulis apa pun (kegagalan keras, bukan warning — beda dari kegagalan ekstraksi LLM per platform di
    bawah). Kegagalan ekstraksi LLM untuk SATU platform (`extract_signals_batch` gagal 3x) TIDAK
    menggagalkan seluruh fungsi: platform itu berkontribusi nol sinyal + 1 baris di `warnings`,
    platform lain (bila sukses) tetap diproses & dilaporkan penuh.

    `allowed_tickers=None` (default) -> diturunkan otomatis dari `simulation_config.json` lewat
    `_derive_allowed_tickers` (panggilan `screen_universe` sungguhan). Untuk test/pemanggilan yang ingin
    menghindari itu, berikan `allowed_tickers` eksplisit.
    """
    sim_dir = sim_dir or os.path.join(Config.OASIS_SIMULATION_DATA_DIR, simulation_id)
    warnings: List[str] = []

    try:
        twitter_actions = _load_actions_file(sim_dir, "twitter")
    except FileNotFoundError:
        return {"success": False, "error": f"actions.jsonl twitter tidak ditemukan di {sim_dir}"}
    except (OSError, json.JSONDecodeError) as e:
        return {"success": False, "error": f"actions.jsonl twitter rusak/gagal dibaca: {e}"}

    try:
        reddit_actions = _load_actions_file(sim_dir, "reddit")
    except FileNotFoundError:
        return {"success": False, "error": f"actions.jsonl reddit tidak ditemukan di {sim_dir}"}
    except (OSError, json.JSONDecodeError) as e:
        return {"success": False, "error": f"actions.jsonl reddit rusak/gagal dibaca: {e}"}

    if allowed_tickers is None:
        try:
            allowed_tickers = _derive_allowed_tickers(sim_dir)
        except Exception as e:
            return {"success": False, "error": f"gagal menurunkan allowed_tickers dari simulation_config.json: {e}"}

    deduped = {
        "twitter": dedup_all_content(twitter_actions),
        "reddit": dedup_all_content(reddit_actions),
    }

    extracted_per_platform: Dict[str, List[ExtractedContentSignals]] = {}
    discarded_all: List[DiscardedSignal] = []
    for platform, contents in deduped.items():
        batch_result = extract_signals_batch(contents, platform)
        if not batch_result["success"]:
            warnings.append(f"{platform}: ekstraksi sinyal gagal - {batch_result['error']}")
            extracted_per_platform[platform] = []
            continue
        kept, discarded = filter_ticker_mentions(batch_result["results"], allowed_tickers)
        extracted_per_platform[platform] = kept
        discarded_all.extend(discarded)

    built = build_persona_ticker_signals(extracted_per_platform, discarded_all)

    allowed_set = {t.strip().upper() for t in allowed_tickers}
    tickers_silent = sorted(
        allowed_set
        - set(built["universe"]["tickers_discussed"])
        - set(built["universe"]["tickers_discarded_out_of_universe"])
    )
    built["universe"]["tickers_silent"] = tickers_silent

    output = {
        "simulation_id": simulation_id,
        "persona_run_id": _read_persona_run_id(sim_dir),
        "personas": built["personas"],
        "universe": built["universe"],
        "deduped_content_by_hash": built["deduped_content_by_hash"],
        "extraction_meta": {
            "method": "llm_batch_per_platform",
            "model": Config.LLM_MODEL_NAME,
            "unique_contents": {p: len(c) for p, c in deduped.items()},
        },
        "warnings": warnings,
    }

    out_path = os.path.join(sim_dir, "signal_extraction.json")
    _write_atomic(out_path, lambda f: json.dump(output, f, ensure_ascii=False, indent=2))

    return {"success": True, "signals": output, "warnings": warnings}
