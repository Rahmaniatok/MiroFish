"""News pipeline step 6 — MiroFish report -> persona JSON (offline, LLM stubbed)."""

import time
from types import SimpleNamespace

import pytest

from app import create_app
from app.news_pipeline import run_store, step6_report_json as s6
from tests.test_pipeline_step3 import run_id  # noqa: F401  (fixture: run with news completed)

UNIVERSE = ["LLY", "JNJ", "MRK", "PFE", "ABBV", "BMY", "GILD", "ISRG", "BDX", "UNH"]

# excerpt in the style of the healthcare trial report (report_af259d80fb30)
REPORT = """# Future Prediction Report
Value Investor: Based on the simulation, they recommend adding LLY, JNJ, MRK, and PFE to their portfolios.
Growth Investor: They are likely to focus on ABBV, BMY, and GILD.
Momentum Trader: They are expected to trade heavily in ISRG, BDX, and UNH.
**调用工具：panorama_search**
<tool_call>{"name": "panorama_search"}</tool_call>
等待工具返回结果...
"""


def test_validate_normalises_and_drops_bad_tickers():
    raw = {"personas": [
        {"persona": "1. value investor", "tickers": ["$lly", "JNJ", "JNJ", "MRK", "PFE"]},
        {"persona": "Growth Investor", "tickers": ["ABBV", "NVDA", "BMY", "XXX"]},   # NVDA not in universe
        {"persona": "Momentum-Trader", "tickers": ["ISRG", "BDX", "UNH", "GILD"]},  # GILD fine (mentioned)
        {"persona": "Contrarian/Skeptic", "tickers": ["LLY", "ABT"]},               # ABT outside universe
        {"persona": "Macro Strategist", "tickers": []},
        {"persona": "Konsensus", "tickers": ["LLY"]},
    ]}
    out = s6.validate(raw, UNIVERSE + ["ABBV"], REPORT)
    got = {p["persona"]: p["tickers"] for p in out["result"]["personas"]}
    assert list(got) == s6.personas()                        # fixed order, all 8 personas
    assert got["Value Investor"] == ["LLY", "JNJ", "MRK", "PFE"]
    assert got["Growth Investor"] == ["ABBV", "BMY"]
    assert got["Momentum Trader"] == ["ISRG", "BDX", "UNH", "GILD"]
    assert got["Contrarian/Skeptic"] == ["LLY"]
    assert got["Technical Analyst"] == []
    v = out["validation"]
    assert v["Growth Investor"]["dropped_out_of_universe"] == ["NVDA"]
    assert v["Technical Analyst"]["missing"] is True
    w = "\n".join(out["warnings"])
    assert "Konsensus" in w and "Technical Analyst: missing" in w and "fewer than 5" in w


def test_validate_drops_tickers_never_in_report():
    universe = UNIVERSE + ["VRTX"]
    out = s6.validate({"personas": [{"persona": "Value Investor", "tickers": ["LLY", "VRTX"]}]}, universe, REPORT)
    assert out["result"]["personas"][0]["tickers"] == ["LLY"]
    assert out["validation"]["Value Investor"]["dropped_not_in_report"] == ["VRTX"]


def test_validate_rejects_non_persona_output():
    with pytest.raises(ValueError, match="personas"):
        s6.validate({"answer": "LLY"}, UNIVERSE, REPORT)


def test_messages_carry_format_universe_and_report():
    msgs = s6.build_messages(UNIVERSE, REPORT)
    assert '"persona": "Risk Manager/Quant"' in msgs[0]["content"]
    assert "LLY, JNJ, MRK" in msgs[0]["content"]
    assert "Never invent tickers" in msgs[0]["content"]
    assert REPORT in msgs[1]["content"]


class _Client:
    model = "stub-model"

    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def chat_json(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.raw


@pytest.fixture
def reported(run_id, monkeypatch):  # noqa: F811
    """Run whose MiroFish step is completed with a report."""
    run_store.write_artifact(run_id, "01_universe.json", {
        **run_store.read_artifact(run_id, "01_universe.json"), "ticker_universe": UNIVERSE})
    for step in ("seed", "prompt", "simulation"):
        run_store.update_step(run_id, step, run_store.STEP_COMPLETED)
    manifest = run_store.load_run(run_id)
    manifest["links"]["report_id"] = "report_x"
    run_store.save_run(manifest)
    monkeypatch.setattr(s6.ReportManager, "get_report", classmethod(
        lambda cls, rid: SimpleNamespace(report_id=rid, markdown_content=REPORT) if rid == "report_x" else None))
    return run_id


def test_convert_writes_exact_format_and_meta(reported):
    client = _Client({"personas": [{"persona": "Value Investor", "tickers": ["LLY", "JNJ"]}]})
    manifest = s6.convert(reported, client=client)
    assert client.calls[0]["temperature"] == 0.0
    assert manifest["steps"]["report_json"]["status"] == "completed"
    result = run_store.read_artifact(reported, s6.ARTIFACT)
    assert set(result) == {"personas"} and len(result["personas"]) == 8
    assert all(set(p) == {"persona", "tickers"} for p in result["personas"])
    meta = run_store.read_artifact(reported, s6.META_ARTIFACT)
    assert meta["report_id"] == "report_x" and meta["model"] == "stub-model"
    assert meta["raw_llm_output"] == client.raw


def test_api_job_runs_in_background(reported, monkeypatch):
    monkeypatch.setattr(s6, "LLMClient", lambda: _Client({"personas": [{"persona": "Growth Investor", "tickers": ["ABBV"]}]}))
    client = create_app().test_client()
    assert client.post(f"/api/pipeline/runs/{reported}/report-json").status_code == 200
    for _ in range(200):
        st = client.get(f"/api/pipeline/runs/{reported}/report-json").json["data"]
        if st["status"] in ("completed", "failed"):
            break
        time.sleep(0.02)
    assert st["status"] == "completed"
    assert st["result"]["personas"][1] == {"persona": "Growth Investor", "tickers": ["ABBV"]}
    assert run_store.load_run(reported)["current_step"] == 7


def test_job_failure_and_preconditions(reported, monkeypatch):
    class Boom:
        model = "x"

        def chat_json(self, *a, **k):
            raise RuntimeError("provider down")

    monkeypatch.setattr(s6, "LLMClient", Boom)
    s6.start(reported)
    for _ in range(200):
        if s6.status(reported)["status"] == "failed":
            break
        time.sleep(0.02)
    st = s6.status(reported)
    assert st["status"] == "failed" and "provider down" in st["error"]

    run_store.update_step(reported, "simulation", run_store.STEP_RUNNING)
    r = create_app().test_client().post(f"/api/pipeline/runs/{reported}/report-json")
    assert r.status_code == 400 and "not completed" in r.json["error"]
