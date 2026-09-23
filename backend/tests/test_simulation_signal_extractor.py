"""
Fase 11b/11c — tes simulation_signal_extractor.py (docs/design/fase11b_signal_extraction.md).

Offline: LLM (create_chat_completion, _get_llm_client) dan time.sleep di-patch; TIDAK ada panggilan API
sungguhan. Fixture `tests/fixtures/sample_actions_{twitter,reddit}.jsonl` adalah SUBSET baris NYATA
(byte-per-byte, tidak diubah) dari backend/uploads/simulations/sim_e2e_verify_20260922_090624/ — bukan
data karangan — supaya kasus Isaac (7x repetisi) dan Morgan/Rachel (AAPL/BRK.A di luar universe)
teruji terhadap bukti nyata, bukan hipotetis.
"""

import json
import os
from types import SimpleNamespace

import pytest

from app.services import simulation_signal_extractor as ext

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


# ---------------------------------------------------------------------------
# Helper / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(ext.time, "sleep", lambda s: None)
    monkeypatch.setattr(ext, "_get_llm_client", lambda: object())
    monkeypatch.setattr(ext, "extract_chat_completion_text", lambda r: r.choices[0].message.content)


class FakeLLM:
    """Urutan respons: str (content) atau Exception. Pola sama seperti test_persona_oasis_adapter.py."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, client, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=r))])


@pytest.fixture
def llm(monkeypatch):
    def _setup(responses):
        fake = FakeLLM(responses)
        monkeypatch.setattr(ext, "create_chat_completion", fake)
        return fake
    return _setup


def load_fixture(name):
    path = os.path.join(FIXTURES_DIR, name)
    actions = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                actions.append(json.loads(line))
    return actions


def _tag_platform(actions, platform):
    """Sama seperti yang dilakukan _load_actions_file: tambahkan 'platform' ke tiap baris."""
    return [{**a, "platform": platform} for a in actions]


def _make_action(round_num, agent_id, agent_name, action_type, content, extra=None):
    args = {"content": content}
    if extra:
        args.update(extra)
    return {
        "round": round_num, "timestamp": "2026-01-01T00:00:00", "agent_id": agent_id,
        "agent_name": agent_name, "action_type": action_type, "action_args": args,
        "result": None, "success": True,
    }


def write_actions(sim_dir, platform, actions):
    d = sim_dir / platform
    d.mkdir(parents=True, exist_ok=True)
    with (d / "actions.jsonl").open("w", encoding="utf-8") as f:
        for a in actions:
            f.write(json.dumps(a) + "\n")


def extraction_response(contents, ticker_map=None):
    """ticker_map: {index: [{"ticker":.., "stance":.., "score":..}, ...]}"""
    ticker_map = ticker_map or {}
    results = [
        {"index": i, "text_preview": c.text[:20], "tickers": ticker_map.get(i, [])}
        for i, c in enumerate(contents)
    ]
    return json.dumps({"results": results})


# ---------------------------------------------------------------------------
# 1. dedup_agent_content / dedup_all_content — data NYATA
# ---------------------------------------------------------------------------

def test_dedup_isaac_case_seven_occurrences_collapse_to_one():
    """Kasus uji eksplisit dari KONTEKS: komentar Isaac muncul 7x (round 3,5,6,7,8,9,10) -> TEPAT 1
    DedupedContent dengan rounds tercatat benar."""
    actions = _tag_platform(load_fixture("sample_actions_reddit.jsonl"), "reddit")
    result = ext.dedup_agent_content(actions, agent_id=7)
    assert len(result) == 1
    dc = result[0]
    assert dc.rounds == [3, 5, 6, 7, 8, 9, 10]
    assert dc.occurrence_count == 7
    assert dc.action_type == "CREATE_COMMENT"
    assert dc.platform == "reddit"
    assert dc.agent_name == "Isaac 'The Irrational Genius' Illusionist"
    assert "indeed the future" in dc.text


def test_dedup_round_zero_excluded():
    """Round 0 (FOLLOW seed + initial CREATE_POST headline) BUKAN opini persona -> tidak muncul di hasil
    dedup untuk agent manapun yang hanya beraksi di round 0 pada fixture ini."""
    actions = _tag_platform(load_fixture("sample_actions_reddit.jsonl"), "reddit")
    # agent_id=4 (Greg) hanya muncul di round 0 (initial post) pada fixture reddit -> hasil dedup kosong
    result = ext.dedup_agent_content(actions, agent_id=4)
    assert result == []


def test_dedup_event_type_lines_ignored():
    actions = _tag_platform(load_fixture("sample_actions_reddit.jsonl"), "reddit")
    event_lines = [a for a in actions if "event_type" in a]
    assert len(event_lines) >= 4  # simulation_start/end + round_start/end ada di fixture
    # tidak boleh menyebabkan error / masuk hasil dedup manapun
    for agent_id in (0, 5, 7):
        for dc in ext.dedup_agent_content(actions, agent_id):
            assert dc.text  # semua hasil punya teks asli, bukan turunan dari baris event


def test_dedup_same_content_different_agent_stays_separate():
    """KONTEKS: 'konten sama dari AGENT BERBEDA -> TETAP 2 entry terpisah'. Data nyata: Dana (agent_id=1)
    dan Morgan (agent_id=0) sama-sama menulis persis 'Warren Buffett stepping down? Bet against! AI
    stocks like $GOOGL are overvalued. Sell now before the crash. #Doomscrolling #MarketCrash'."""
    actions = _tag_platform(load_fixture("sample_actions_twitter.jsonl"), "twitter")
    dana = ext.dedup_agent_content(actions, agent_id=1)
    morgan = ext.dedup_agent_content(actions, agent_id=0)
    shared_text = "Warren Buffett stepping down? Bet against! AI stocks like $GOOGL are overvalued. Sell now before the crash. #Doomscrolling #MarketCrash"
    dana_dc = next(dc for dc in dana if dc.text == shared_text)
    morgan_dc = next(dc for dc in morgan if dc.text == shared_text)
    assert dana_dc.agent_id == 1 and morgan_dc.agent_id == 0
    assert dana_dc is not morgan_dc

    all_deduped = ext.dedup_all_content(actions)
    matching = [dc for dc in all_deduped if dc.text == shared_text]
    assert len(matching) == 2  # TIDAK digabung jadi 1 lintas agent
    assert {dc.agent_id for dc in matching} == {0, 1}


def test_dedup_isaac_content_never_mixed_with_other_agents():
    actions = _tag_platform(load_fixture("sample_actions_reddit.jsonl"), "reddit")
    all_deduped = ext.dedup_all_content(actions)
    isaac_groups = [dc for dc in all_deduped if dc.agent_id == 7]
    assert len(isaac_groups) == 1
    assert all(dc.agent_id != 7 for dc in all_deduped if dc is not isaac_groups[0])


# ---------------------------------------------------------------------------
# filter_ticker_mentions — data NYATA (AAPL/BRK.A di luar universe Energy+Communication Services)
# ---------------------------------------------------------------------------

def test_filter_real_fixture_aapl_and_brk_discarded_googl_kept():
    """KONTEKS: 'AAPL dan BRK.A yang disebut Morgan/Rachel (di luar universe Energy+Comm Services)
    HARUS masuk discarded, TIDAK masuk output utama'."""
    actions = _tag_platform(load_fixture("sample_actions_reddit.jsonl"), "reddit")
    morgan = ext.dedup_agent_content(actions, agent_id=0)
    rachel = ext.dedup_agent_content(actions, agent_id=5)
    aapl_content = next(dc for dc in morgan if "$AAPL" in dc.text)
    brk_content = next(dc for dc in rachel if "$BRK" in dc.text)

    sigs = [
        ext.ExtractedContentSignals(content=aapl_content, tickers=[
            {"ticker": "GOOGL", "stance": "bullish", "score": 0.5},
            {"ticker": "AAPL", "stance": "bullish", "score": 0.5},
        ]),
        ext.ExtractedContentSignals(content=brk_content, tickers=[
            {"ticker": "BRK", "stance": "neutral", "score": 0.0},
        ]),
    ]
    universe_run_nyata = ["GOOGL", "META", "NFLX", "CVX"]  # 4 dari 44 ticker screening asli, cukup utk tes
    kept, discarded = ext.filter_ticker_mentions(sigs, universe_run_nyata)

    discarded_tickers = {d.ticker for d in discarded}
    assert discarded_tickers == {"AAPL", "BRK"}
    kept_tickers = {t["ticker"] for sig in kept for t in sig.tickers}
    assert kept_tickers == {"GOOGL"}
    # discarded tetap membawa konteks penuh untuk audit
    aapl_discard = next(d for d in discarded if d.ticker == "AAPL")
    assert aapl_discard.agent_id == 0 and aapl_discard.agent_name.startswith("Morgan")
    assert aapl_discard.platform == "reddit" and aapl_discard.rounds == aapl_content.rounds


def test_filter_discarded_ticker_is_logged_not_silently_dropped(monkeypatch):
    logged = []
    monkeypatch.setattr(ext.logger, "warning", lambda msg: logged.append(msg))
    content = ext.DedupedContent(agent_id=3, agent_name="Elena", platform="twitter",
                                  action_type="CREATE_POST", text="x", rounds=[2, 4])
    sig = ext.ExtractedContentSignals(content=content, tickers=[
        {"ticker": "TSLA", "stance": "bullish", "score": 0.4},
    ])
    kept, discarded = ext.filter_ticker_mentions([sig], allowed_tickers=["GOOGL"])
    assert len(discarded) == 1 and kept[0].tickers == []
    assert len(logged) == 1
    assert "TSLA" in logged[0] and "agent_id=3" in logged[0] and "[2, 4]" in logged[0]


def test_filter_ticker_in_universe_passes_through_unchanged():
    content = ext.DedupedContent(agent_id=0, agent_name="A", platform="twitter",
                                  action_type="CREATE_POST", text="x", rounds=[1])
    sig = ext.ExtractedContentSignals(content=content, tickers=[{"ticker": "GOOGL", "stance": "bullish", "score": 0.5}])
    kept, discarded = ext.filter_ticker_mentions([sig], allowed_tickers=["GOOGL", "META"])
    assert discarded == []
    assert kept[0].tickers == [{"ticker": "GOOGL", "stance": "bullish", "score": 0.5}]


# ---------------------------------------------------------------------------
# extract_signals_batch — LLM di-mock
# ---------------------------------------------------------------------------

def test_extraction_batch_success_first_attempt(llm):
    contents = [
        ext.DedupedContent(agent_id=0, agent_name="A", platform="twitter", action_type="CREATE_POST",
                            text="Bullish on $GOOGL today", rounds=[1]),
        ext.DedupedContent(agent_id=1, agent_name="B", platform="twitter", action_type="CREATE_POST",
                            text="No ticker mentioned here", rounds=[2]),
    ]
    # hanya contents[0] (menyebut $GOOGL) yang dikirim ke LLM -> respons dibangun dari SUBSET itu
    resp = extraction_response([contents[0]], {0: [{"ticker": "GOOGL", "stance": "bullish", "score": 0.7}]})
    fake = llm([resp])
    result = ext.extract_signals_batch(contents, "twitter")
    assert result["success"] and result["attempt"] == 1
    assert len(fake.calls) == 1
    # hanya item BER-ticker yang dikirim ke LLM (item 'No ticker' tidak ikut, langsung tickers=[])
    user_prompt = fake.calls[0]["messages"][1]["content"]
    assert "Bullish on $GOOGL" in user_prompt and "No ticker mentioned" not in user_prompt
    by_text = {r.content.text: r.tickers for r in result["results"]}
    assert by_text["Bullish on $GOOGL today"] == [{"ticker": "GOOGL", "stance": "bullish", "score": 0.7}]
    assert by_text["No ticker mentioned here"] == []


def test_extraction_batch_no_ticker_content_skips_llm_entirely(llm):
    contents = [ext.DedupedContent(agent_id=0, agent_name="A", platform="reddit", action_type="CREATE_POST",
                                    text="just chatting, no symbols", rounds=[1])]
    fake = llm([])  # LLM tidak boleh dipanggil sama sekali
    result = ext.extract_signals_batch(contents, "reddit")
    assert result["success"] and result["attempt"] == 0
    assert fake.calls == []
    assert result["results"][0].tickers == []


def test_extraction_batch_retries_on_index_mismatch(llm):
    """Mock LLM index tidak cocok posisi -> retry (validasi PERSIS pola enrichment Fase 11a)."""
    contents = [
        ext.DedupedContent(agent_id=0, agent_name="A", platform="twitter", action_type="CREATE_POST",
                            text="Bullish on $GOOGL today", rounds=[1]),
    ]
    bad = json.dumps({"results": [{"index": 1, "text_preview": "Bullish on $GOOGL to", "tickers": []}]})
    good = extraction_response(contents, {0: [{"ticker": "GOOGL", "stance": "bullish", "score": 0.6}]})
    fake = llm([bad, good])
    result = ext.extract_signals_batch(contents, "twitter")
    assert result["success"] and result["attempt"] == 2
    assert len(fake.calls) == 2
    assert [c["temperature"] for c in fake.calls] == [1.0, 0.9]  # _temperature_for_attempt (Fase 10, diimpor)


def test_extraction_batch_retries_on_text_preview_mismatch(llm):
    """text_preview tidak cocok = indikasi atribusi silang antar-item di batch -> retry."""
    contents = [
        ext.DedupedContent(agent_id=0, agent_name="A", platform="twitter", action_type="CREATE_POST",
                            text="Bullish on $GOOGL today", rounds=[1]),
        ext.DedupedContent(agent_id=1, agent_name="B", platform="twitter", action_type="CREATE_POST",
                            text="Bearish on $META now", rounds=[2]),
    ]
    swapped = json.dumps({"results": [
        {"index": 0, "text_preview": "Bearish on $META now"[:20], "tickers": []},  # preview milik item lain
        {"index": 1, "text_preview": "Bullish on $GOOGL toda"[:20], "tickers": []},
    ]})
    good = extraction_response(contents, {
        0: [{"ticker": "GOOGL", "stance": "bullish", "score": 0.6}],
        1: [{"ticker": "META", "stance": "bearish", "score": -0.5}],
    })
    fake = llm([swapped, good])
    result = ext.extract_signals_batch(contents, "twitter")
    assert result["success"] and result["attempt"] == 2


def test_extraction_batch_fails_after_max_attempts_no_silent_fallback(llm):
    contents = [ext.DedupedContent(agent_id=0, agent_name="A", platform="reddit", action_type="CREATE_POST",
                                    text="Bullish on $GOOGL today", rounds=[1])]
    always_bad = json.dumps({"results": [{"index": 9, "text_preview": "x", "tickers": []}]})
    fake = llm([always_bad, always_bad, always_bad])
    result = ext.extract_signals_batch(contents, "reddit")
    assert result["success"] is False
    assert "3 attempt" in result["error"]
    assert result["platform"] == "reddit"
    assert len(fake.calls) == 3


def test_extraction_batch_llm_exception_retries_then_fails(llm):
    fake = llm([RuntimeError("boom")] * 3)
    contents = [ext.DedupedContent(agent_id=0, agent_name="A", platform="twitter", action_type="CREATE_POST",
                                    text="$GOOGL rally", rounds=[1])]
    result = ext.extract_signals_batch(contents, "twitter")
    assert result["success"] is False and "boom" in result["error"]
    assert len(fake.calls) == 3


def test_extraction_batch_invalid_stance_or_score_rejected(llm):
    contents = [ext.DedupedContent(agent_id=0, agent_name="A", platform="twitter", action_type="CREATE_POST",
                                    text="$GOOGL rally", rounds=[1])]
    bad = json.dumps({"results": [{"index": 0, "text_preview": "$GOOGL rally"[:20],
                                    "tickers": [{"ticker": "GOOGL", "stance": "super-bullish", "score": 0.5}]}]})
    good = extraction_response(contents, {0: [{"ticker": "GOOGL", "stance": "bullish", "score": 0.6}]})
    fake = llm([bad, good])
    result = ext.extract_signals_batch(contents, "twitter")
    assert result["success"] and result["attempt"] == 2


# ---------------------------------------------------------------------------
# Multi-ticker per konten unik (kasus nyata: Morgan "$GOOGL and $AAPL")
# ---------------------------------------------------------------------------

def test_multi_ticker_content_produces_signal_for_each_ticker():
    content = ext.DedupedContent(agent_id=0, agent_name="Morgan", platform="reddit",
                                  action_type="CREATE_POST", text="$GOOGL and $AAPL both look good",
                                  rounds=[3, 4])
    sig = ext.ExtractedContentSignals(content=content, tickers=[
        {"ticker": "GOOGL", "stance": "bullish", "score": 0.6},
        {"ticker": "AAPL", "stance": "bullish", "score": 0.5},
    ])
    built = ext.build_persona_ticker_signals({"reddit": [sig]}, [])
    persona = built["personas"]["Morgan"]
    assert set(persona.keys()) == {"GOOGL", "AAPL"}
    assert len(persona["GOOGL"]["history"]) == 2  # 1 baris per round (2 round)
    assert len(persona["AAPL"]["history"]) == 2
    assert persona["GOOGL"]["evidence_count"] == 1  # 1 KONTEN UNIK meski 2 round -> tidak dihitung ganda
    assert persona["AAPL"]["evidence_count"] == 1
    assert persona["GOOGL"]["mentioned_in_rounds"] == [3, 4]


def test_extraction_batch_multi_ticker_from_llm_response(llm):
    """Morgan nyata: '$GOOGL and $AAPL' -> LLM diminta mengembalikan 2 entri ticker untuk 1 item."""
    contents = [ext.DedupedContent(agent_id=0, agent_name="Morgan", platform="reddit",
                                    action_type="CREATE_COMMENT",
                                    text="Let's focus on AI stocks like $GOOGL and $AAPL. Embrace the irrationality to thrive!",
                                    rounds=[4, 5, 6, 7, 8, 9, 10])]
    resp = extraction_response(contents, {0: [
        {"ticker": "GOOGL", "stance": "bullish", "score": 0.6},
        {"ticker": "AAPL", "stance": "bullish", "score": 0.55},
    ]})
    fake = llm([resp])
    result = ext.extract_signals_batch(contents, "reddit")
    assert result["success"]
    tickers = {t["ticker"] for t in result["results"][0].tickers}
    assert tickers == {"GOOGL", "AAPL"}


# ---------------------------------------------------------------------------
# Agregasi: persona tanpa mention -> tidak ada entry
# ---------------------------------------------------------------------------

def test_persona_without_mention_has_no_entry_for_ticker():
    content = ext.DedupedContent(agent_id=0, agent_name="Morgan", platform="reddit",
                                  action_type="CREATE_POST", text="$GOOGL looks strong", rounds=[1])
    sig = ext.ExtractedContentSignals(content=content, tickers=[{"ticker": "GOOGL", "stance": "bullish", "score": 0.5}])
    built = ext.build_persona_ticker_signals({"reddit": [sig]}, [])
    assert "META" not in built["personas"]["Morgan"]
    assert "META" not in built["universe"]["tickers_discussed"]
    assert built["universe"]["tickers_discussed"] == ["GOOGL"]


def test_persona_with_zero_ticker_signals_gets_no_entry_at_all():
    """Konten yang diekstrak tapi tickers=[] (mis. tidak menyebut ticker apa pun) tidak membuat
    persona itu muncul di dict personas sama sekali."""
    content = ext.DedupedContent(agent_id=2, agent_name="Alex", platform="reddit",
                                  action_type="CREATE_COMMENT", text="just agreeing, no tickers", rounds=[3])
    sig = ext.ExtractedContentSignals(content=content, tickers=[])
    built = ext.build_persona_ticker_signals({"reddit": [sig]}, [])
    assert "Alex" not in built["personas"]


# ---------------------------------------------------------------------------
# extract_simulation_signals — orkestrasi penuh
# ---------------------------------------------------------------------------

def test_extract_simulation_signals_missing_platform_file_returns_error(tmp_path):
    sim_dir = tmp_path / "sim_missing"
    write_actions(sim_dir, "twitter", [_make_action(1, 0, "A", "CREATE_POST", "$GOOGL up")])
    # reddit/actions.jsonl sengaja tidak dibuat
    out = ext.extract_simulation_signals("sim_missing", sim_dir=str(sim_dir), allowed_tickers=["GOOGL"])
    assert out["success"] is False
    assert "reddit" in out["error"] and "tidak ditemukan" in out["error"]
    assert not (sim_dir / "signal_extraction.json").exists()


def test_extract_simulation_signals_missing_twitter_file_names_twitter(tmp_path):
    sim_dir = tmp_path / "sim_missing2"
    write_actions(sim_dir, "reddit", [_make_action(1, 0, "A", "CREATE_POST", "$GOOGL up")])
    out = ext.extract_simulation_signals("sim_missing2", sim_dir=str(sim_dir), allowed_tickers=["GOOGL"])
    assert out["success"] is False
    assert "twitter" in out["error"]


def test_extract_simulation_signals_one_platform_extraction_failure_reported_separately(tmp_path, llm):
    """KONTEKS: 'gagal 3x salah satu platform -> error eksplisit untuk platform itu, platform lain
    (kalau sukses) TETAP dilaporkan terpisah (bukan seluruh fungsi gagal total)'."""
    sim_dir = tmp_path / "sim_partial"
    write_actions(sim_dir, "twitter", [_make_action(1, 0, "A", "CREATE_POST", "Bullish $GOOGL")])
    write_actions(sim_dir, "reddit", [_make_action(1, 1, "B", "CREATE_POST", "Bearish $META")])

    always_bad = json.dumps({"results": [{"index": 99, "text_preview": "x", "tickers": []}]})
    good_reddit = json.dumps({"results": [{"index": 0, "text_preview": "Bearish $META"[:20],
                                            "tickers": [{"ticker": "META", "stance": "bearish", "score": -0.6}]}]})
    fake = llm([always_bad, always_bad, always_bad, good_reddit])  # twitter diproses dulu (insertion order)

    out = ext.extract_simulation_signals("sim_partial", sim_dir=str(sim_dir), allowed_tickers=["GOOGL", "META"])
    assert out["success"] is True  # kegagalan ekstraksi 1 platform TIDAK menggagalkan seluruh fungsi
    assert len(fake.calls) == 4
    assert any("twitter" in w and "gagal" in w for w in out["warnings"])
    assert "A" not in out["signals"]["personas"]  # twitter gagal -> tidak ada sinyal dari agent A
    assert out["signals"]["personas"]["B"]["META"]["latest_stance"] == "bearish"


def test_extract_simulation_signals_writes_file_matching_return_and_schema(tmp_path, llm):
    sim_dir = tmp_path / "sim_full"
    write_actions(sim_dir, "twitter", [_make_action(1, 0, "A", "CREATE_POST", "$GOOGL up")])
    write_actions(sim_dir, "reddit", [_make_action(1, 1, "B", "CREATE_POST", "$META down")])
    good_t = json.dumps({"results": [{"index": 0, "text_preview": "$GOOGL up"[:20],
                                       "tickers": [{"ticker": "GOOGL", "stance": "bullish", "score": 0.5}]}]})
    good_r = json.dumps({"results": [{"index": 0, "text_preview": "$META down"[:20],
                                       "tickers": [{"ticker": "META", "stance": "bearish", "score": -0.5}]}]})
    llm([good_t, good_r])

    out = ext.extract_simulation_signals("sim_full", sim_dir=str(sim_dir), allowed_tickers=["GOOGL", "META", "CVX"])
    assert out["success"]

    written = json.loads((sim_dir / "signal_extraction.json").read_text(encoding="utf-8"))
    assert written == out["signals"]  # FILE hasil tulisan == return value, bukan cuma di memori
    assert set(written) == {
        "simulation_id", "persona_run_id", "personas", "universe",
        "deduped_content_by_hash", "extraction_meta", "warnings",
    }
    assert written["simulation_id"] == "sim_full"
    assert written["personas"]["A"]["GOOGL"]["latest_stance"] == "bullish"
    assert written["personas"]["B"]["META"]["latest_stance"] == "bearish"
    assert written["universe"]["tickers_discussed"] == ["GOOGL", "META"]
    assert written["universe"]["tickers_silent"] == ["CVX"]
    assert written["extraction_meta"]["method"] == "llm_batch_per_platform"
    assert written["warnings"] == []


def test_tickers_silent_excludes_discussed_and_discarded(tmp_path, llm):
    sim_dir = tmp_path / "sim_silent"
    write_actions(sim_dir, "twitter", [_make_action(1, 0, "A", "CREATE_POST", "$GOOGL up")])
    write_actions(sim_dir, "reddit", [_make_action(1, 1, "B", "CREATE_POST", "$AAPL up")])
    good_t = json.dumps({"results": [{"index": 0, "text_preview": "$GOOGL up"[:20],
                                       "tickers": [{"ticker": "GOOGL", "stance": "bullish", "score": 0.5}]}]})
    good_r = json.dumps({"results": [{"index": 0, "text_preview": "$AAPL up"[:20],
                                       "tickers": [{"ticker": "AAPL", "stance": "bullish", "score": 0.5}]}]})
    llm([good_t, good_r])
    # AAPL SENGAJA tidak dimasukkan allowed_tickers -> harus jadi discarded, bukan discussed/silent
    universe = ["GOOGL", "CVX", "META"]
    out = ext.extract_simulation_signals("sim_silent", sim_dir=str(sim_dir), allowed_tickers=universe)
    assert out["success"]
    u = out["signals"]["universe"]
    assert u["tickers_discussed"] == ["GOOGL"]
    assert u["tickers_discarded_out_of_universe"] == ["AAPL"]
    assert u["tickers_silent"] == ["CVX", "META"]
    assert "B" not in out["signals"]["personas"]  # AAPL dibuang -> B tidak punya sinyal apa pun


def test_extract_simulation_signals_deduped_content_by_hash_is_resolvable(tmp_path, llm):
    """source_text_hash di history bisa ditelusuri balik lewat deduped_content_by_hash di file yang sama
    (traceability §6 dokumen — tanpa perlu baca ulang actions.jsonl)."""
    sim_dir = tmp_path / "sim_trace"
    write_actions(sim_dir, "twitter", [_make_action(3, 7, "Isaac", "CREATE_COMMENT", "$GOOGL to the moon")])
    write_actions(sim_dir, "reddit", [])
    resp = json.dumps({"results": [{"index": 0, "text_preview": "$GOOGL to the moon"[:20],
                                     "tickers": [{"ticker": "GOOGL", "stance": "bullish", "score": 0.8}]}]})
    llm([resp])
    out = ext.extract_simulation_signals("sim_trace", sim_dir=str(sim_dir), allowed_tickers=["GOOGL"])
    assert out["success"]
    history_entry = out["signals"]["personas"]["Isaac"]["GOOGL"]["history"][0]
    h = history_entry["source_text_hash"]
    resolved = out["signals"]["deduped_content_by_hash"][h]
    assert resolved["text"] == "$GOOGL to the moon"
    assert resolved["agent_id"] == 7 and resolved["rounds"] == [3]
