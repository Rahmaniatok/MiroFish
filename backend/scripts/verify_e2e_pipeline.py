"""
Fase 11 — Verifikasi END-TO-END SUNGGUHAN dari seluruh alur Fase 8-11a:

    get_or_generate_personas() -> build_oasis_artifacts() -> SimulationRunner.start_simulation()

TANPA mock: LLM sungguhan (RunPod Qwen2.5-7B), Finnhub sungguhan, yfinance sungguhan,
OASIS sungguhan (2 platform, 10 round, follow-network lengkap).

Ini BUKAN test otomatis (tidak ada assert yang menggagalkan CI) dan BUKAN skrip sekali-pakai:
dipertahankan sebagai referensi untuk regression-check manual berikutnya. Jalankan dari
direktori backend/:

    cd backend && python scripts/verify_e2e_pipeline.py

Argumen opsional: --sim-id, --sectors (comma-separated GICS names), --as-of-date (ISO atau
kosong = live), --max-wait-seconds (langit-langit polling, BUKAN estimasi durasi asli),
--skip-simulation (hanya jalankan persona+artifact, tanpa OASIS — untuk pengecekan murah).

Semua temuan (timing, warning, enrichment, follow-network, distribusi aksi, contoh konten,
estimasi token) ditulis ke <sim_dir>/e2e_verification_findings.json untuk dibaca ulang saat
menyusun laporan markdown. Biaya LLM sungguhan akan terpakai setiap kali skrip ini dijalankan.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, '..'))
_project_root = os.path.abspath(os.path.join(_backend_dir, '..'))
sys.path.insert(0, _backend_dir)

from dotenv import load_dotenv
_env_file = os.path.join(_project_root, '.env')
if os.path.exists(_env_file):
    load_dotenv(_env_file)


# ---------------------------------------------------------------------------
# Token-usage capture: patches the OpenAI SDK client itself (not any MiroFish
# source file) so every real completion call made anywhere in this process —
# persona generation, enrichment, LLM warm-up, and every OASIS agent decision
# during the simulation — is counted uniformly in one place.
# ---------------------------------------------------------------------------
_usage_log = []


def _install_usage_capture():
    import openai.resources.chat.completions.completions as _c

    orig_sync = _c.Completions.create
    orig_async = _c.AsyncCompletions.create

    def _record(response, base_url, elapsed):
        try:
            usage = getattr(response, "usage", None)
            _usage_log.append({
                "base_url": base_url,
                "model": getattr(response, "model", None),
                "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
                "elapsed_s": round(elapsed, 2),
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    def sync_wrapper(self, *args, **kwargs):
        t0 = time.monotonic()
        resp = orig_sync(self, *args, **kwargs)
        _record(resp, str(getattr(self._client, "base_url", "")), time.monotonic() - t0)
        return resp

    async def async_wrapper(self, *args, **kwargs):
        t0 = time.monotonic()
        resp = await orig_async(self, *args, **kwargs)
        _record(resp, str(getattr(self._client, "base_url", "")), time.monotonic() - t0)
        return resp

    _c.Completions.create = sync_wrapper
    _c.AsyncCompletions.create = async_wrapper


_install_usage_capture()

from app.config import Config  # noqa: E402
from app.services.persona_generator import get_or_generate_personas  # noqa: E402
from app.services.persona_oasis_adapter import build_oasis_artifacts  # noqa: E402
from app.services.simulation_runner import SimulationRunner, RunnerStatus  # noqa: E402


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def analyze_follow_network(sim_dir, platform):
    db_path = os.path.join(sim_dir, f"{platform}_simulation.db")
    if not os.path.exists(db_path):
        return {"error": f"{db_path} tidak ditemukan"}
    conn = sqlite3.connect(db_path)
    try:
        follow_row_count = conn.execute("SELECT COUNT(*) FROM follow").fetchone()[0]
        per_agent = conn.execute(
            "SELECT agent_id, num_followers, num_followings FROM user "
            "WHERE agent_id IS NOT NULL ORDER BY agent_id"
        ).fetchall()
    finally:
        conn.close()
    return {
        "follow_row_count": follow_row_count,
        "per_agent": [
            {"agent_id": a, "num_followers": nf, "num_followings": ng}
            for a, nf, ng in per_agent
        ],
        "all_agents_have_7_followers_and_7_followings": all(
            nf == 7 and ng == 7 for _a, nf, ng in per_agent
        ) if per_agent else False,
    }


def analyze_actions(sim_dir, platform):
    actions_path = os.path.join(sim_dir, platform, "actions.jsonl")
    if not os.path.exists(actions_path):
        return {"error": f"{actions_path} tidak ditemukan"}

    action_type_counter = Counter()
    agents_active_by_round = defaultdict(set)
    follow_actions_by_round = Counter()
    content_examples = []
    rounds_seen = set()
    total_lines = 0

    with open(actions_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            d = json.loads(line)
            if "event_type" in d:
                continue
            rnum = d.get("round")
            atype = d.get("action_type")
            action_type_counter[atype] += 1
            rounds_seen.add(rnum)
            agents_active_by_round[rnum].add(d.get("agent_id"))
            if atype == "FOLLOW":
                follow_actions_by_round[rnum] += 1
            content = (d.get("action_args") or {}).get("content")
            if content and atype in ("CREATE_POST", "CREATE_COMMENT"):
                content_examples.append({
                    "round": rnum,
                    "agent": d.get("agent_name"),
                    "action_type": atype,
                    "content": content,
                })

    non_round0_follows = {r: c for r, c in follow_actions_by_round.items() if r != 0}
    all_rounds = sorted(r for r in rounds_seen if r is not None and r > 0)
    agents_per_round_ge1 = {
        str(r): sorted(a for a in agents_active_by_round[r] if a is not None)
        for r in all_rounds
    }
    full_participation_rounds = sum(
        1 for r in all_rounds if len(agents_active_by_round[r]) == 8
    )

    return {
        "total_lines": total_lines,
        "action_type_distribution": dict(action_type_counter),
        "rounds_with_actions_gt0": all_rounds,
        "agents_active_per_round_gt0": agents_per_round_ge1,
        "rounds_with_all_8_agents_active": full_participation_rounds,
        "rounds_gt0_total": len(all_rounds),
        "follow_actions_by_round": dict(follow_actions_by_round),
        "follow_actions_outside_round0": non_round0_follows,
        "content_examples_sample": content_examples[:8],
        "content_examples_total": len(content_examples),
    }


def analyze_log(sim_dir):
    sim_log_path = os.path.join(sim_dir, "simulation.log")
    if not os.path.exists(sim_log_path):
        return {"error": f"{sim_log_path} tidak ditemukan"}
    with open(sim_log_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.splitlines()
    error_lines = [l for l in lines if ("Traceback" in l or "Exception" in l or "Error" in l) and "MaxTokensWarning" not in l]
    twhin_lines = [l for l in lines if "twhin" in l.lower()]
    log_dir = os.path.join(sim_dir, "log")
    return {
        "simulation_log_bytes": len(text),
        "simulation_log_lines": len(lines),
        "traceback_count": text.count("Traceback (most recent call last)"),
        "error_like_lines_sample": error_lines[:40],
        "error_like_lines_total": len(error_lines),
        "twhin_bert_mentions": twhin_lines[:20],
        "log_subdir_exists": os.path.isdir(log_dir),
        "log_subdir_writable": os.access(log_dir, os.W_OK) if os.path.isdir(log_dir) else None,
    }


def main():
    parser = argparse.ArgumentParser(description="Verifikasi E2E Fase 8-11a (SUNGGUHAN, tanpa mock)")
    parser.add_argument("--sim-id", default=None)
    parser.add_argument("--sectors", default="Energy,Communication Services")
    parser.add_argument("--as-of-date", default=None, help="ISO YYYY-MM-DD; kosong = live (hari ini)")
    parser.add_argument("--max-wait-seconds", type=int, default=2700,
                         help="Langit-langit polling run_state.json (BUKAN estimasi durasi; default 45 menit)")
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--skip-simulation", action="store_true",
                         help="Hanya jalankan persona+artifact (step 2-3), lewati OASIS run (step 4-6)")
    args = parser.parse_args()

    sectors = [s.strip() for s in args.sectors.split(",") if s.strip()]
    sim_id = args.sim_id or f"sim_e2e_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    findings = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "simulation_id": sim_id,
            "sectors": sectors,
            "market_cap_tiers": None,
            "as_of_date": args.as_of_date,
            "skip_simulation": args.skip_simulation,
        },
    }

    sim_dir = os.path.join(Config.OASIS_SIMULATION_DATA_DIR, sim_id)

    def flush_findings():
        os.makedirs(sim_dir, exist_ok=True)
        out_path = os.path.join(sim_dir, "e2e_verification_findings.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(findings, f, ensure_ascii=False, indent=2, default=str)
        log(f"Findings ditulis: {out_path}")
        return out_path

    try:
        # ------------------------------------------------------------------
        # STEP 2: get_or_generate_personas — SUNGGUHAN
        # ------------------------------------------------------------------
        log(f"STEP 2: get_or_generate_personas(as_of_date={args.as_of_date!r}, sectors={sectors})")
        t0 = time.monotonic()
        personas_result = get_or_generate_personas(
            as_of_date=args.as_of_date, sectors=sectors, market_cap_tiers=None
        )
        t_persona = time.monotonic() - t0
        log(f"STEP 2 selesai dalam {t_persona:.1f}s, success={personas_result.get('success')}")

        if not personas_result.get("success"):
            findings["step2_status"] = "GAGAL"
            findings["step2_error"] = personas_result.get("error")
            flush_findings()
            log("BERHENTI: persona generation gagal.")
            return

        findings["step2_status"] = "OK"
        findings["step2"] = {
            "elapsed_seconds": round(t_persona, 1),
            "run_id": personas_result["run_id"],
            "from_cache": personas_result.get("from_cache"),
            "grounding": personas_result["grounding"],
            "sample_articles_count": len(personas_result.get("sample_articles") or []),
            "article_ids": personas_result.get("article_ids"),
            "warnings": personas_result.get("warnings"),
            "temperature_used": personas_result.get("temperature_used"),
            "attempt": personas_result.get("attempt"),
            "personas_summary": [
                {"name": p["name"], "philosophy_label": p["philosophy_label"], "tagline": p["tagline"]}
                for p in personas_result["personas"]
            ],
        }

        # ------------------------------------------------------------------
        # STEP 3: build_oasis_artifacts — SUNGGUHAN
        # ------------------------------------------------------------------
        log(f"STEP 3: build_oasis_artifacts(simulation_id={sim_id})")
        t0 = time.monotonic()
        artifacts_result = build_oasis_artifacts(personas_result, simulation_id=sim_id)
        t_artifacts = time.monotonic() - t0
        log(f"STEP 3 selesai dalam {t_artifacts:.1f}s, success={artifacts_result.get('success')}")

        if not artifacts_result.get("success"):
            findings["step3_status"] = "GAGAL"
            findings["step3_error"] = artifacts_result.get("error")
            flush_findings()
            log("BERHENTI: build_oasis_artifacts gagal.")
            return

        findings["step3_status"] = "OK"
        sim_dir = artifacts_result["sim_dir"]
        written_config = load_json(os.path.join(sim_dir, "simulation_config.json"))
        reddit_profiles = load_json(os.path.join(sim_dir, "reddit_profiles.json"))

        findings["step3"] = {
            "elapsed_seconds": round(t_artifacts, 1),
            "simulation_id": sim_id,
            "sim_dir": sim_dir,
            "result_warnings": artifacts_result["warnings"],
            "persona_run_warnings_in_written_file": written_config["persona_run"]["warnings"],
            "warnings_match_file_vs_return": (
                artifacts_result["warnings"] == written_config["persona_run"]["warnings"]
            ),
            "enrichment_table": [
                {"name": r["name"], "age": r["age"], "gender": r["gender"],
                 "mbti": r["mbti"], "country": r["country"]}
                for r in reddit_profiles
            ],
            "enrichment_diversity_check": {
                "distinct_mbti": len({r["mbti"] for r in reddit_profiles}),
                "distinct_countries": len({r["country"] for r in reddit_profiles}),
                "distinct_genders": len({r["gender"] for r in reddit_profiles}),
                "age_range": [min(r["age"] for r in reddit_profiles), max(r["age"] for r in reddit_profiles)],
            },
            "initial_posts": written_config["event_config"]["initial_posts"],
        }

        if args.skip_simulation:
            findings["step4_status"] = "DILEWATI (--skip-simulation)"
            flush_findings()
            log("Selesai (skip-simulation). Findings ditulis.")
            return

        # ------------------------------------------------------------------
        # STEP 4: SimulationRunner.start_simulation — SUNGGUHAN, sampai selesai
        # ------------------------------------------------------------------
        log(f"STEP 4: SimulationRunner.start_simulation({sim_id}, platform='parallel')")
        t0 = time.monotonic()
        state = SimulationRunner.start_simulation(sim_id, platform="parallel")
        deadline = time.monotonic() + args.max_wait_seconds
        terminal_statuses = (RunnerStatus.COMPLETED, RunnerStatus.FAILED, RunnerStatus.STOPPED)
        final_status = None

        while time.monotonic() < deadline:
            time.sleep(args.poll_interval)
            state = SimulationRunner.get_run_state(sim_id)
            if state is None:
                log("[poll] run_state belum tersedia...")
                continue
            log(
                f"[poll] status={state.runner_status.value} round={state.current_round}/{state.total_rounds} "
                f"tw_round={state.twitter_current_round} rd_round={state.reddit_current_round} "
                f"tw_actions={state.twitter_actions_count} rd_actions={state.reddit_actions_count} "
                f"tw_completed={state.twitter_completed} rd_completed={state.reddit_completed}"
            )
            if state.runner_status in terminal_statuses:
                final_status = state.runner_status
                break

        t_simulation = time.monotonic() - t0
        log(f"STEP 4: loop polling selesai setelah {t_simulation:.1f}s, final_status={final_status}")

        findings["step4"] = {
            "elapsed_seconds_until_terminal_or_timeout": round(t_simulation, 1),
            "final_runner_status": final_status.value if final_status else "TIMEOUT_BELUM_TERMINAL",
            "run_state_final_in_memory": state.to_detail_dict() if state else None,
        }

        # Bersihkan proses yang masih menunggu IPC (perilaku produksi normal
        # setelah completed: producer tetap hidup untuk interview).
        try:
            stopped_state = SimulationRunner.stop_simulation(sim_id)
            findings["step4"]["stopped_cleanly"] = True
            findings["step4"]["run_state_after_stop"] = stopped_state.to_dict()
        except Exception as e:
            findings["step4"]["stop_error"] = f"{type(e).__name__}: {e}"

        run_state_path = os.path.join(sim_dir, "run_state.json")
        if os.path.exists(run_state_path):
            findings["step4"]["run_state_json_file"] = load_json(run_state_path)

        findings["step4"]["follow_network"] = {
            "twitter": analyze_follow_network(sim_dir, "twitter"),
            "reddit": analyze_follow_network(sim_dir, "reddit"),
        }

        findings["step4_status"] = (
            "BERHASIL PENUH" if final_status == RunnerStatus.COMPLETED else f"LIHAT DETAIL ({final_status})"
        )

        # ------------------------------------------------------------------
        # STEP 5: actions.jsonl — ringkasan
        # ------------------------------------------------------------------
        log("STEP 5: menganalisis actions.jsonl kedua platform")
        findings["step5"] = {
            "twitter": analyze_actions(sim_dir, "twitter"),
            "reddit": analyze_actions(sim_dir, "reddit"),
        }

        # ------------------------------------------------------------------
        # STEP 6: regresi log / twhin-bert / direktori log
        # ------------------------------------------------------------------
        log("STEP 6: memeriksa simulation.log untuk exception/traceback")
        findings["step6"] = analyze_log(sim_dir)

        # ------------------------------------------------------------------
        # STEP 7: estimasi token/biaya
        # ------------------------------------------------------------------
        log("STEP 7: menyusun estimasi token")
        total_prompt = sum(u["prompt_tokens"] or 0 for u in _usage_log)
        total_completion = sum(u["completion_tokens"] or 0 for u in _usage_log)
        calls_missing_usage = sum(1 for u in _usage_log if u["total_tokens"] is None)
        findings["step7"] = {
            "llm_calls_total": len(_usage_log),
            "llm_calls_missing_usage_field": calls_missing_usage,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "usage_log_detail": _usage_log,
        }

    except Exception as e:
        findings["fatal_error"] = f"{type(e).__name__}: {e}"
        findings["fatal_traceback"] = traceback.format_exc()
        log(f"FATAL: {e}\n{traceback.format_exc()}")

    finally:
        findings["finished_at"] = datetime.now(timezone.utc).isoformat()
        flush_findings()


if __name__ == "__main__":
    main()
