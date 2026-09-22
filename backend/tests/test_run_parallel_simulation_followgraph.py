"""
Fase 11a — perubahan terbatas pada backend/scripts/run_parallel_simulation.py:
  1. follow-network complete graph (di-gating: social_graph.follow_network == "complete")
  2. perbaikan bug last_rowid (aksi round 0 tercatat lagi sebagai round 1) — TIDAK di-gating
  3. warm-up LLM sebelum round 0

Offline: OASIS berjalan sungguhan (env, platform, SQLite), tetapi keputusan LLM agent diganti aksi
CREATE_POST deterministik (SocialAgent.perform_action_by_llm di-patch) dan model CAMEL tidak pernah
dipanggil. Jalur Twitter memakai recsys twhin-bert (butuh cache HuggingFace lokal; di-skip bila tidak ada).
"""

import asyncio
import csv
import importlib.util
import json
import os
import sqlite3
import sys

import pytest

oasis = pytest.importorskip("oasis")
pytest.importorskip("camel")

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "run_parallel_simulation.py")
N_AGENTS = 8
INITIAL_POSTS = [
    {"content": "Chip rally extends $NVDA — via Reuters (2025-06-27)", "poster_type": "Investor", "poster_agent_id": 1},
    {"content": "Banks pass stress test $JPM — via Bloomberg (2025-06-26)", "poster_type": "Investor", "poster_agent_id": 3},
]


def _twhin_cached():
    try:
        from huggingface_hub import try_to_load_from_cache
        return isinstance(try_to_load_from_cache("Twitter/twhin-bert-base", "config.json"), str)
    except Exception:
        return False


PLATFORMS = [
    pytest.param("twitter", marks=pytest.mark.skipif(not _twhin_cached(), reason="Twitter/twhin-bert-base tidak ada di cache HF")),
    "reddit",
]


@pytest.fixture(scope="module")
def rps(tmp_path_factory):
    """Import skrip sebagai modul dari cwd bersih (oasis membuat ./log saat import)."""
    workdir = tmp_path_factory.mktemp("rps_import")
    previous = os.getcwd()
    os.chdir(workdir)
    try:
        spec = importlib.util.spec_from_file_location("run_parallel_simulation_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_parallel_simulation_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        os.chdir(previous)
    return module


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch, rps):
    """Tidak ada panggilan jaringan: model CAMEL dibuat tanpa dipanggil, keputusan agent dibuat deterministik."""
    from camel.models import ModelFactory
    from camel.types import ModelPlatformType

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    monkeypatch.setattr(
        rps, "create_model",
        lambda config, use_boost=False: ModelFactory.create(model_platform=ModelPlatformType.OPENAI, model_type="gpt-4o-mini"),
    )

    async def fake_perform_action_by_llm(self):
        return await self.perform_action_by_data(
            oasis.ActionType.CREATE_POST, content=f"llm post by agent {self.social_agent_id}")

    monkeypatch.setattr(oasis.SocialAgent, "perform_action_by_llm", fake_perform_action_by_llm)


def make_sim_dir(tmp_path, social_graph=None, initial_posts=INITIAL_POSTS):
    sim_dir = tmp_path / "sim"
    sim_dir.mkdir()
    with (sim_dir / "twitter_profiles.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "name", "username", "user_char", "description"])
        for i in range(N_AGENTS):
            writer.writerow([i, f"Name {i}", f"agent_{i}", f"Agent {i} is an investor.", f"bio {i}"])
    (sim_dir / "reddit_profiles.json").write_text(json.dumps([
        {"user_id": i, "username": f"agent_{i}", "name": f"Name {i}", "bio": f"bio {i}",
         "persona": f"Agent {i} is an investor.", "age": 30 + i, "gender": "other", "mbti": "INTJ", "country": "United States"}
        for i in range(N_AGENTS)]), encoding="utf-8")
    config = {
        "simulation_id": "sim_test", "project_id": "", "graph_id": "",
        "time_config": {
            "total_simulation_hours": 1, "minutes_per_round": 30,                 # 2 round
            "agents_per_hour_min": N_AGENTS, "agents_per_hour_max": N_AGENTS,
            "peak_hours": [], "peak_activity_multiplier": 1.0, "off_peak_hours": [], "off_peak_activity_multiplier": 1.0,
            "morning_hours": [], "morning_activity_multiplier": 1.0, "work_hours": [], "work_activity_multiplier": 1.0,
        },
        "agent_configs": [
            {"agent_id": i, "entity_name": f"Name {i}", "activity_level": 1.0, "active_hours": list(range(24))}
            for i in range(N_AGENTS)],
        "event_config": {"initial_posts": initial_posts, "scheduled_events": [], "hot_topics": [], "narrative_direction": ""},
        "llm_model": "gpt-4o-mini",
    }
    if social_graph is not None:
        config["social_graph"] = social_graph
    return sim_dir, config


def run_platform(rps, platform, sim_dir, config):
    """Jalankan run_twitter_simulation / run_reddit_simulation apa adanya; return (PlatformSimulation, jumlah edge igraph)."""
    manager = rps.SimulationLogManager(str(sim_dir))
    logger = manager.get_twitter_logger() if platform == "twitter" else manager.get_reddit_logger()
    runner = rps.run_twitter_simulation if platform == "twitter" else rps.run_reddit_simulation

    async def go():
        result = await runner(config, str(sim_dir), logger, manager, None)
        edges = result.agent_graph.get_num_edges() if result.agent_graph is not None else None
        await result.env.close()
        return result, edges

    return asyncio.run(go())


def read_actions(sim_dir, platform):
    events = []
    with (sim_dir / platform / "actions.jsonl").open(encoding="utf-8") as f:
        for line in f:
            events.append(json.loads(line))
    return events


def actions_in_round(events, round_num):
    return [e for e in events if e.get("round") == round_num and "action_type" in e]


def round_end_count(events, round_num):
    return next(e["actions_count"] for e in events if e.get("round") == round_num and e.get("event_type") == "round_end")


def db_rows(sim_dir, platform, sql):
    conn = sqlite3.connect(str(sim_dir / f"{platform}_simulation.db"))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. follow-network (di-gating)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", PLATFORMS)
def test_complete_follow_network_creates_56_edges_and_7_followers_each(rps, tmp_path, platform):
    sim_dir, config = make_sim_dir(tmp_path, social_graph={"follow_network": "complete"})
    _, igraph_edges = run_platform(rps, platform, sim_dir, config)

    assert db_rows(sim_dir, platform, "SELECT COUNT(*) FROM follow") == [(56,)]
    assert db_rows(sim_dir, platform, "SELECT COUNT(*) FROM (SELECT DISTINCT follower_id, followee_id FROM follow)") == [(56,)]
    assert db_rows(sim_dir, platform, "SELECT COUNT(*) FROM follow WHERE follower_id = followee_id") == [(0,)]
    assert db_rows(sim_dir, platform, "SELECT COUNT(*) FROM trace WHERE action = 'follow'") == [(56,)]
    users = db_rows(sim_dir, platform, "SELECT user_id, num_followers, num_followings FROM user ORDER BY user_id")
    assert users == [(i, 7, 7) for i in range(N_AGENTS)]
    assert igraph_edges == 56                                      # add_edge kosmetik tersinkron


@pytest.mark.parametrize("platform", PLATFORMS)
def test_follow_actions_are_logged_as_round_0_not_round_1(rps, tmp_path, platform):
    sim_dir, config = make_sim_dir(tmp_path, social_graph={"follow_network": "complete"})
    run_platform(rps, platform, sim_dir, config)
    events = read_actions(sim_dir, platform)

    round0 = actions_in_round(events, 0)
    assert sum(1 for e in round0 if e["action_type"] == "FOLLOW") == 56
    assert sum(1 for e in round0 if e["action_type"] == "CREATE_POST") == len(INITIAL_POSTS)
    assert round_end_count(events, 0) == 56 + len(INITIAL_POSTS)
    for round_num in (1, 2):
        assert not [e for e in actions_in_round(events, round_num) if e["action_type"] == "FOLLOW"]
        assert round_end_count(events, round_num) == N_AGENTS      # hanya 8 aksi LLM, tanpa limpahan round 0
    assert events[-1]["event_type"] == "simulation_end" and events[-1]["total_actions"] == 56 + 2 + 2 * N_AGENTS


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("social_graph", [None, {}, {"follow_network": "none"}, {"follow_network": "Complete"}])
def test_config_without_complete_follow_network_behaves_as_before(rps, tmp_path, platform, social_graph):
    """Gating: key hilang / nilai lain -> tidak ada aksi FOLLOW sama sekali, baik di DB maupun di log."""
    sim_dir, config = make_sim_dir(tmp_path, social_graph=social_graph)
    _, igraph_edges = run_platform(rps, platform, sim_dir, config)
    events = read_actions(sim_dir, platform)

    assert db_rows(sim_dir, platform, "SELECT COUNT(*) FROM follow") == [(0,)]
    assert db_rows(sim_dir, platform, "SELECT COUNT(*) FROM trace WHERE action = 'follow'") == [(0,)]
    assert igraph_edges == 0
    assert not [e for e in events if e.get("action_type") == "FOLLOW"]
    assert round_end_count(events, 0) == len(INITIAL_POSTS)          # round 0 = hanya post awal
    assert db_rows(sim_dir, platform, "SELECT COUNT(*) FROM user WHERE num_followers != 0 OR num_followings != 0") == [(0,)]


# ---------------------------------------------------------------------------
# 2. bug last_rowid (tidak di-gating: berlaku untuk semua config)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("social_graph", [None, {"follow_network": "complete"}])
def test_initial_posts_are_not_logged_again_as_round_1_actions(rps, tmp_path, platform, social_graph):
    sim_dir, config = make_sim_dir(tmp_path, social_graph=social_graph)
    run_platform(rps, platform, sim_dir, config)
    events = read_actions(sim_dir, platform)

    initial_contents = {p["content"] for p in INITIAL_POSTS}
    for e in events:
        if "action_type" not in e or e["round"] == 0:
            continue
        assert e["action_args"].get("content") not in initial_contents, f"initial post muncul lagi di round {e['round']}"
    # Round 0: tiap initial post tercatat tepat sekali.
    round0_posts = [e["action_args"]["content"] for e in actions_in_round(events, 0) if e["action_type"] == "CREATE_POST"]
    assert sorted(round0_posts) == sorted(initial_contents)
    # Round 1 dan 2: hanya 8 aksi LLM masing-masing (sebelum perbaikan: 8 + 2 = 10 di round 1).
    assert round_end_count(events, 1) == N_AGENTS and round_end_count(events, 2) == N_AGENTS
    assert len(actions_in_round(events, 1)) == N_AGENTS


@pytest.mark.parametrize("platform", PLATFORMS)
def test_bug_fix_also_applies_without_any_initial_posts(rps, tmp_path, platform):
    sim_dir, config = make_sim_dir(tmp_path, social_graph=None, initial_posts=[])
    run_platform(rps, platform, sim_dir, config)
    events = read_actions(sim_dir, platform)
    assert round_end_count(events, 0) == 0
    assert round_end_count(events, 1) == N_AGENTS


# ---------------------------------------------------------------------------
# 3. warm-up LLM
# ---------------------------------------------------------------------------

class FakeAsyncClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.closed = False
        FakeAsyncClient.instances.append(self)
        self.chat = self
        self.completions = self

    behaviour = "ok"       # "ok" | "error" | "hang"

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if FakeAsyncClient.behaviour == "error":
            raise ConnectionError("endpoint unreachable")
        if FakeAsyncClient.behaviour == "hang":
            await asyncio.sleep(30)
        return object()

    async def close(self):
        self.closed = True


class RecordingLogger:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(msg)


@pytest.fixture
def fake_client(monkeypatch, rps):
    FakeAsyncClient.instances = []
    FakeAsyncClient.behaviour = "ok"
    monkeypatch.setattr(rps, "AsyncOpenAI", FakeAsyncClient)
    for var in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_NAME", "LLM_BOOST_API_KEY", "LLM_BOOST_BASE_URL", "LLM_BOOST_MODEL_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "key-general")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "model-general")
    return FakeAsyncClient


def test_resolve_llm_endpoint_mirrors_create_model(rps, fake_client, monkeypatch):
    assert rps.resolve_llm_endpoint({}, use_boost=False) == ("key-general", "https://llm.example/v1", "model-general")
    # boost diminta tetapi tidak dikonfigurasi -> jatuh ke konfigurasi umum
    assert rps.resolve_llm_endpoint({}, use_boost=True) == ("key-general", "https://llm.example/v1", "model-general")
    monkeypatch.setenv("LLM_BOOST_API_KEY", "key-boost")
    monkeypatch.setenv("LLM_BOOST_BASE_URL", "https://boost.example/v1")
    assert rps.resolve_llm_endpoint({}, use_boost=True) == ("key-boost", "https://boost.example/v1", "model-general")   # model boost kosong -> model umum
    monkeypatch.setenv("LLM_BOOST_MODEL_NAME", "model-boost")
    assert rps.resolve_llm_endpoint({}, use_boost=True) == ("key-boost", "https://boost.example/v1", "model-boost")
    monkeypatch.delenv("LLM_MODEL_NAME")
    assert rps.resolve_llm_endpoint({"llm_model": "from-config"}, use_boost=False)[2] == "from-config"


def test_warm_up_success_makes_one_cheap_call_and_logs_duration_to_main_log_only(rps, fake_client, tmp_path):
    logger = RecordingLogger()
    assert asyncio.run(rps.warm_up_llm({}, main_logger=logger)) is True
    client = fake_client.instances[0]
    assert len(client.calls) == 1 and client.calls[0]["model"] == "model-general"
    assert client.kwargs["api_key"] == "key-general" and client.kwargs["base_url"] == "https://llm.example/v1"
    assert client.closed is True
    assert any("预热完成" in line and "秒" in line for line in logger.lines)
    assert all(line.startswith("[Warm-up]") for line in logger.lines)
    assert "key-general" not in " ".join(logger.lines)                 # API key tidak pernah dilog


def test_warm_up_failure_is_a_warning_not_an_exception(rps, fake_client):
    fake_client.behaviour = "error"
    logger = RecordingLogger()
    assert asyncio.run(rps.warm_up_llm({}, main_logger=logger)) is False
    assert any("预热失败" in line and "endpoint unreachable" in line for line in logger.lines)
    assert fake_client.instances[0].closed is True


def test_warm_up_timeout_returns_false(rps, fake_client, monkeypatch):
    fake_client.behaviour = "hang"
    monkeypatch.setattr(rps, "WARMUP_TIMEOUT_SECONDS", 0.05)
    assert asyncio.run(rps.warm_up_llm({}, main_logger=RecordingLogger())) is False


def test_warm_up_without_api_key_is_skipped(rps, fake_client, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY")
    assert asyncio.run(rps.warm_up_llm({}, main_logger=RecordingLogger())) is False
    assert fake_client.instances == []


def test_warm_up_llms_dedups_identical_endpoints_and_warms_distinct_ones(rps, fake_client, monkeypatch):
    asyncio.run(rps.warm_up_llms({}, RecordingLogger()))
    assert len(fake_client.instances) == 1                             # Twitter (umum) dan Reddit (boost->umum) sama
    fake_client.instances.clear()
    monkeypatch.setenv("LLM_BOOST_API_KEY", "key-boost")
    monkeypatch.setenv("LLM_BOOST_BASE_URL", "https://boost.example/v1")
    asyncio.run(rps.warm_up_llms({}, RecordingLogger()))
    assert sorted(c.kwargs["base_url"] for c in fake_client.instances) == ["https://boost.example/v1", "https://llm.example/v1"]
    fake_client.instances.clear()
    asyncio.run(rps.warm_up_llms({}, RecordingLogger(), twitter=False, reddit=True))
    assert [c.kwargs["base_url"] for c in fake_client.instances] == ["https://boost.example/v1"]
    fake_client.instances.clear()
    asyncio.run(rps.warm_up_llms({}, RecordingLogger(), twitter=False, reddit=False))
    assert fake_client.instances == []


def test_main_warms_up_before_round_0_and_continues_when_warm_up_fails(rps, fake_client, tmp_path, monkeypatch):
    fake_client.behaviour = "error"
    sim_dir, config = make_sim_dir(tmp_path)
    config_path = sim_dir / "simulation_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    order = []

    real_warm_up = rps.warm_up_llms

    async def recording_warm_up(*args, **kwargs):
        order.append("warm_up")
        await real_warm_up(*args, **kwargs)

    async def fake_twitter(*args, **kwargs):
        order.append("twitter")
        return rps.PlatformSimulation()

    async def fake_reddit(*args, **kwargs):
        order.append("reddit")
        return rps.PlatformSimulation()

    monkeypatch.setattr(rps, "warm_up_llms", recording_warm_up)
    monkeypatch.setattr(rps, "run_twitter_simulation", fake_twitter)
    monkeypatch.setattr(rps, "run_reddit_simulation", fake_reddit)
    monkeypatch.setattr(sys, "argv", ["run_parallel_simulation.py", "--config", str(config_path), "--no-wait"])
    monkeypatch.chdir(tmp_path)

    asyncio.run(rps.main())
    assert order[0] == "warm_up" and sorted(order[1:]) == ["reddit", "twitter"]     # warm-up gagal, simulasi tetap jalan
    assert len(fake_client.instances) == 1 and fake_client.instances[0].calls
    log = (sim_dir / "simulation.log").read_text(encoding="utf-8")
    assert "预热失败" in log
    for platform in ("twitter", "reddit"):                              # warm-up bukan aksi agent: tidak masuk actions.jsonl
        actions_log = sim_dir / platform / "actions.jsonl"
        if actions_log.exists():
            text = actions_log.read_text(encoding="utf-8")
            assert "Warm-up" not in text and "预热" not in text


def test_main_honours_single_platform_flags_for_warm_up(rps, fake_client, tmp_path, monkeypatch):
    sim_dir, config = make_sim_dir(tmp_path)
    config_path = sim_dir / "simulation_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    seen = []

    async def recording_warm_up(cfg, logger, twitter=True, reddit=True):
        seen.append((twitter, reddit))

    async def fake_platform(*args, **kwargs):
        return rps.PlatformSimulation()

    monkeypatch.setattr(rps, "warm_up_llms", recording_warm_up)
    monkeypatch.setattr(rps, "run_twitter_simulation", fake_platform)
    monkeypatch.setattr(rps, "run_reddit_simulation", fake_platform)
    monkeypatch.chdir(tmp_path)
    for flag, expected in (("--twitter-only", (True, False)), ("--reddit-only", (False, True))):
        monkeypatch.setattr(sys, "argv", ["run_parallel_simulation.py", "--config", str(config_path), "--no-wait", flag])
        asyncio.run(rps.main())
        assert seen[-1] == expected
