"""
Phase 4a — Multi-round debate room for the 6 fixed personas.

============================================================================
WHY THIS FILE EXISTS
============================================================================
MiroFish's original "simulation" stage ran a large generated population of
social-media agents across two independent OASIS platforms (Twitter + Reddit),
each platform its own `OasisEnv` + SQLite DB + recommender, driven by a
subprocess (`backend/scripts/run_parallel_simulation.py`) and a monitor thread.
Round-to-round memory there is the OASIS platform feed (the recsys shows each
agent other agents' recent posts) plus each `camel` agent's own conversation
memory. The optional `ZepGraphMemoryUpdater` is a ONE-WAY, post-hoc writer that
pushes action descriptions into the project graph for later analysis — it never
feeds back into an agent mid-simulation.

The investment engine has replaced that population with EXACTLY 6 fixed,
deterministic personas per ticker (Phase 3a + 3d): 5 investor archetypes whose
bullish/neutral/bearish stance is derived from real entity values, plus "Gamma",
a Shari'ah compliance monitor whose compliant / non_compliant / indeterminate
verdict is derived from the seed graph's ShariaScreen entity. There is no social
graph, no virality, no crowd — the viewpoint diversity is by construction.

So Phase 4a keeps only the part of the simulation stage that still applies —
the ROUND STRUCTURE — and drops the OASIS platform substrate:

  * ONE shared debate room (no dual platform / retail-crowd — that population
    was never built and the engine gives us nothing for it).
  * A configurable number of rounds (`DEFAULT_DEBATE_ROUNDS`, small by default).
  * Round N: every persona produces exactly ONE statement, reacting to the seed
    data AND to every statement made in rounds 1..N-1. "Memory" is an explicit
    transcript passed into each prompt (the direct analogue of the OASIS feed,
    minus the recommender).
  * Data grounding is unchanged from Phase 3: statements are built around real
    entity figures pulled from the seed graph digest.

------------------------------------------------------------------------------
GAMMA'S VERDICT IS A LOOP INVARIANT (structural guarantee)
------------------------------------------------------------------------------
`_derive_sharia_verdict(idx)` is called ONCE, before the round loop starts. The
result is stored in `self._gamma_verdict` and never reassigned. Every round,
`DebateStatement.disposition` for Gamma is taken from that frozen dict — it is
NOT parsed out of the LLM response. The LLM (when enabled) only writes Gamma's
prose and is explicitly told the verdict is already decided. So Gamma's recorded
verdict cannot drift across rounds no matter what any other persona argues or
what the model emits — the invariance is enforced in the control flow, not just
requested in a prompt.
============================================================================
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.logger import get_logger
from ..utils.openai_chat_compat import (
    create_chat_completion,
    extract_chat_completion_text,
)
from .oasis_profile_generator import (
    GAMMA_ARCHETYPE,
    INVESTOR_ARCHETYPES,
    OasisAgentProfile,
    OasisProfileGenerator,
    _derive_investor_stance,
    _derive_sharia_verdict,
    _index_seed_entities,
    _seed_digest,
)
from .zep_entity_reader import FilteredEntities

logger = get_logger("mirofish.debate_room")

# Small on purpose — tune later. 3 is enough to see statements evolve while
# keeping the LLM cost of a manual run modest (6 personas x rounds calls).
DEFAULT_DEBATE_ROUNDS = 3

# Roles a persona can hold in the room.
ROLE_INVESTOR = "investor"
ROLE_COMPLIANCE = "compliance"


class DebateError(RuntimeError):
    """Raised when the debate cannot be run for a seed (e.g. no personas)."""


@dataclass
class DebateStatement:
    """One persona's single statement in one round."""

    round_num: int                 # 1-indexed
    position: int                  # persona slot 0..5 (matches Phase 3 order)
    persona_key: str               # value/growth/technical/quality/macro/sharia
    persona_name: str              # e.g. "Value Investor · AAPL" / "Gamma · AAPL"
    role: str                      # ROLE_INVESTOR | ROLE_COMPLIANCE
    disposition: str               # investor: stance; Gamma: verdict (INVARIANT)
    text: str                      # the statement prose
    grounded_figures: List[str] = field(default_factory=list)
    used_llm: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_num": self.round_num,
            "position": self.position,
            "persona_key": self.persona_key,
            "persona_name": self.persona_name,
            "role": self.role,
            "disposition": self.disposition,
            "text": self.text,
            "grounded_figures": list(self.grounded_figures),
            "used_llm": self.used_llm,
        }


@dataclass
class DebateTranscript:
    """The full multi-round debate for one ticker."""

    ticker: str
    as_of_date: Optional[str]
    rounds: int
    personas: List[OasisAgentProfile]
    # statements[r] is the list of DebateStatement for round r+1, in persona order
    statements: List[List[DebateStatement]]
    gamma_verdict: Dict[str, Any]
    used_llm: bool

    # -- convenience views ------------------------------------------------
    def round_statements(self, round_num: int) -> List[DebateStatement]:
        """1-indexed round accessor."""
        if round_num < 1 or round_num > len(self.statements):
            raise IndexError(f"round {round_num} out of range 1..{len(self.statements)}")
        return self.statements[round_num - 1]

    def flat(self) -> List[DebateStatement]:
        return [s for rnd in self.statements for s in rnd]

    def for_persona(self, persona_key: str) -> List[DebateStatement]:
        return [s for s in self.flat() if s.persona_key == persona_key]

    def gamma_statements(self) -> List[DebateStatement]:
        return self.for_persona(GAMMA_ARCHETYPE["key"])

    def gamma_verdicts(self) -> List[str]:
        """The verdict recorded on Gamma's statement in every round (must be constant)."""
        return [s.disposition for s in self.gamma_statements()]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of_date": self.as_of_date,
            "rounds": self.rounds,
            "used_llm": self.used_llm,
            "gamma_verdict": dict(self.gamma_verdict),
            "personas": [p.to_dict() for p in self.personas],
            "statements": [[s.to_dict() for s in rnd] for rnd in self.statements],
        }


# ===========================================================================
# Prompt building (LLM path)
# ===========================================================================

def _debate_system_prompt() -> str:
    return (
        "You run a multi-round investment debate. Each speaker is a fixed persona "
        "arguing ONE stock. Speakers must stay in character and stay grounded in the "
        "real figures they are given: every statement must quote at least one actual "
        "metric value. A speaker may respond to what earlier speakers said, but an "
        "investor must not abandon the stance they were assigned, and the compliance "
        "monitor must not change its verdict. Return valid JSON only: "
        '{"statement": "..."} with no unescaped newlines inside the string.'
    )


def _prior_statements_block(prior: List[DebateStatement]) -> str:
    if not prior:
        return "(this is round 1 - no prior statements; react to the seed data only)"
    lines = []
    for s in prior:
        lines.append(f"[round {s.round_num}] {s.persona_name} ({s.disposition}): {s.text}")
    return "\n".join(lines)


def _investor_round_prompt(
    archetype: Dict[str, Any],
    idx: Dict[str, Any],
    digest: str,
    stance_info: Dict[str, Any],
    round_num: int,
    prior: List[DebateStatement],
) -> str:
    ticker = idx.get("ticker") or "the stock"
    company = idx.get("company_name") or ticker
    evidence = "; ".join(stance_info.get("evidence") or []) or "(no specific figures available)"
    return f"""ROUND {round_num} of an investment debate on {ticker} ({company}).

YOU ARE: {archetype['name']} (fixed investing style - do not switch styles).
How you think: {archetype['lens']}

YOUR ASSIGNED STANCE (do not contradict it): {stance_info['stance']}
Why: {stance_info['rationale']}
Your key figures: {evidence}

Full data on {ticker}:
{digest}

Statements so far:
{_prior_statements_block(prior)}

Write your round {round_num} statement (first person, 2-4 sentences, one paragraph,
no line breaks). {"Open the debate by stating your case." if round_num == 1 else
"React to at least one point another speaker made in an earlier round - name them - then reaffirm your " + stance_info['stance'] + " view."}
Quote at least one real figure. Return JSON: {{"statement": "..."}}
"""


def _gamma_round_prompt(
    idx: Dict[str, Any],
    digest: str,
    verdict_info: Dict[str, Any],
    round_num: int,
    prior: List[DebateStatement],
) -> str:
    ticker = idx.get("ticker") or "the stock"
    company = idx.get("company_name") or ticker
    sc = idx.get("sharia") or {}
    evidence = "; ".join(verdict_info.get("evidence") or []) or "(screen figures unavailable)"
    return f"""ROUND {round_num} of an investment debate on {ticker} ({company}).

YOU ARE: Gamma, the Shari'ah compliance monitor. You are NOT an investor - you are a
binding compliance gate. {GAMMA_ARCHETYPE['lens']}

THE VERDICT IS ALREADY DECIDED AND FIXED FOR EVERY ROUND: {verdict_info['verdict']}
Reason: {verdict_info['rationale']}
Screening figures: {evidence}

Do NOT re-judge, soften, or change this verdict. It does not move because an
investor is bullish or bearish.

Full data on {ticker}:
{digest}

Statements so far:
{_prior_statements_block(prior)}

Write your round {round_num} statement (first person, 2-4 sentences, one paragraph,
no line breaks). Restate the verdict ({verdict_info['verdict']}) and make clear it is
a hard filter. {"" if round_num == 1 else "You may briefly note that the investors' arguments do not affect it."}
Quote the actual debt/market-cap ratio (and the excluded category if non_compliant).
Return JSON: {{"statement": "..."}}
"""


def _call_statement_llm(client: Any, model: str, system: str, prompt: str) -> str:
    """One LLM call returning the 'statement' string; raises on failure."""
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = create_chat_completion(
                client,
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.6 - attempt * 0.2,
            )
            content = extract_chat_completion_text(response)
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                # tolerate a bare string or fenced JSON
                cleaned = content.strip().strip("`").strip()
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:].strip()
                data = json.loads(cleaned)
            statement = str(data.get("statement") or "").strip()
            if statement:
                return statement
            last_error = ValueError("LLM response had no 'statement'")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise last_error or RuntimeError("statement generation failed")


# ===========================================================================
# Deterministic path (no LLM) - still cites real figures, still references
# prior rounds so the transcript visibly evolves.
# ===========================================================================

def _investor_round_statement_rule_based(
    archetype: Dict[str, Any],
    idx: Dict[str, Any],
    stance_info: Dict[str, Any],
    round_num: int,
    prior: List[DebateStatement],
) -> str:
    ticker = idx.get("ticker") or "the stock"
    stance = stance_info["stance"]
    evidence = stance_info.get("evidence") or []
    figures = "; ".join(evidence) if evidence else "the seed figures"

    if round_num == 1 or not prior:
        return (
            f"{archetype['name']} on {ticker}, opening the debate: I am {stance}. "
            f"{stance_info['rationale']} The figures that drive this: {figures}."
        )

    # Reference the earliest prior statement by name so the evolution is visible
    # and assertable without an LLM.
    ref = prior[0]
    return (
        f"Round {round_num}: {ref.persona_name} spoke in round {ref.round_num}, and "
        f"I have now seen {len(prior)} statement(s) from earlier rounds. None of it "
        f"changes the numbers, so I hold my {stance} view on {ticker}: "
        f"{stance_info['rationale']} Still anchored on {figures}."
    )


def _gamma_round_statement_rule_based(
    idx: Dict[str, Any],
    verdict_info: Dict[str, Any],
) -> str:
    """Deterministic Gamma statement — IDENTICAL every round (pure restatement).

    Takes no `round_num` / `prior` on purpose: Gamma's verdict is a loop
    invariant and in the no-LLM path we make that unmistakable by emitting the
    exact same text in every round.
    """
    ticker = idx.get("ticker") or "the stock"
    sc = idx.get("sharia") or {}
    verdict = verdict_info["verdict"]
    verdict_word = verdict.replace("_", "-")
    evidence = verdict_info.get("evidence") or []
    ratio = sc.get("debt_to_market_cap")
    category = sc.get("excluded_category")

    cited = []
    if ratio is not None:
        cited.append(f"debt/market-cap {ratio:.4f}")
    if verdict == "non_compliant" and category:
        cited.append(f"business-activity exclusion: {category}")
    cited_str = "; ".join(cited) if cited else "screen figures unavailable"

    return (
        f"Gamma, AAOIFI SS-21 compliance gate on {ticker}. Verdict: {verdict_word} "
        f"({cited_str}). {verdict_info['rationale']} This verdict is a hard filter and "
        f"does not change round to round or move with the investors' arguments"
        + (f"; screening figures: {'; '.join(evidence)}." if evidence else ".")
    )


# ===========================================================================
# The debate room
# ===========================================================================

class DebateRoom:
    """Runs the 6 Phase-3 personas through a multi-round shared debate."""

    def __init__(
        self,
        seed: FilteredEntities,
        *,
        use_llm: bool = True,
        generator: Optional[OasisProfileGenerator] = None,
        model_name: Optional[str] = None,
    ):
        self.seed = seed
        self.use_llm = use_llm
        self.idx = _index_seed_entities(seed.entities)
        self.digest = _seed_digest(self.idx)
        self.ticker = self.idx.get("ticker") or "STOCK"
        self.as_of_date = self.idx.get("as_of_date")

        self._generator = generator or self._make_generator(use_llm, model_name)
        self._client = getattr(self._generator, "client", None)
        self._model = model_name or getattr(self._generator, "model_name", None) or Config.LLM_MODEL_NAME

        # --- Phase 3 personas: one seed graph in, 6 personas out ------------
        self.personas: List[OasisAgentProfile] = self._generator.generate_profiles_from_entities(
            entities=seed.entities,
            use_llm=use_llm,
        )
        if not self.personas:
            raise DebateError(f"no personas generated for {self.ticker}")

        # --- Dispositions derived ONCE — loop invariants -------------------
        # Investor stance per archetype (same derivation as Phase 3a).
        self._stances: Dict[str, Dict[str, Any]] = {
            a["key"]: _derive_investor_stance(a["key"], self.idx)
            for a in INVESTOR_ARCHETYPES
        }
        # Gamma's verdict — computed here, BEFORE any round, never recomputed.
        # Every round reads this exact dict for Gamma's recorded disposition.
        self._gamma_verdict: Dict[str, Any] = _derive_sharia_verdict(self.idx)

    # ------------------------------------------------------------------
    @staticmethod
    def _make_generator(
        use_llm: bool, model_name: Optional[str]
    ) -> OasisProfileGenerator:
        if use_llm:
            return OasisProfileGenerator(model_name=model_name)
        # Offline: build a generator without touching the network / API key,
        # mirroring the Phase 3 test convention.
        gen = object.__new__(OasisProfileGenerator)
        gen.client = None
        gen.model_name = model_name or "offline"
        gen.zep_client = None
        gen.graph_id = None
        return gen

    # ------------------------------------------------------------------
    def _persona_specs(self) -> List[Dict[str, Any]]:
        """The 6 persona slots in Phase 3 order: 5 investors, then Gamma."""
        specs = [
            {"position": i, "kind": ROLE_INVESTOR, "key": a["key"], "archetype": a}
            for i, a in enumerate(INVESTOR_ARCHETYPES)
        ]
        specs.append(
            {
                "position": len(INVESTOR_ARCHETYPES),
                "kind": ROLE_COMPLIANCE,
                "key": GAMMA_ARCHETYPE["key"],
                "archetype": GAMMA_ARCHETYPE,
            }
        )
        return specs

    def _persona_name(self, position: int, fallback_key: str) -> str:
        if 0 <= position < len(self.personas):
            return self.personas[position].name
        return f"{fallback_key} · {self.ticker}"

    # ------------------------------------------------------------------
    def _make_statement(
        self,
        spec: Dict[str, Any],
        round_num: int,
        prior: List[DebateStatement],
    ) -> DebateStatement:
        position = spec["position"]
        key = spec["key"]
        name = self._persona_name(position, key)

        if spec["kind"] == ROLE_COMPLIANCE:
            verdict_info = self._gamma_verdict            # <-- frozen, never re-derived
            disposition = verdict_info["verdict"]         # <-- recorded from the invariant
            if self.use_llm and disposition != "unknown":
                try:
                    text = _call_statement_llm(
                        self._client,
                        self._model,
                        _debate_system_prompt(),
                        _gamma_round_prompt(
                            self.idx, self.digest, verdict_info, round_num, prior
                        ),
                    )
                    used_llm = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Gamma round %d LLM failed (%s) - using template", round_num, str(exc)[:120]
                    )
                    text = _gamma_round_statement_rule_based(self.idx, verdict_info)
                    used_llm = False
            else:
                text = _gamma_round_statement_rule_based(self.idx, verdict_info)
                used_llm = False
            return DebateStatement(
                round_num=round_num,
                position=position,
                persona_key=key,
                persona_name=name,
                role=ROLE_COMPLIANCE,
                disposition=disposition,
                text=text,
                grounded_figures=list(verdict_info.get("evidence") or []),
                used_llm=used_llm,
            )

        # investor
        archetype = spec["archetype"]
        stance_info = self._stances[key]
        disposition = stance_info["stance"]
        if self.use_llm:
            try:
                text = _call_statement_llm(
                    self._client,
                    self._model,
                    _debate_system_prompt(),
                    _investor_round_prompt(
                        archetype, self.idx, self.digest, stance_info, round_num, prior
                    ),
                )
                used_llm = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "%s round %d LLM failed (%s) - using template",
                    archetype["name"], round_num, str(exc)[:120],
                )
                text = _investor_round_statement_rule_based(
                    archetype, self.idx, stance_info, round_num, prior
                )
                used_llm = False
        else:
            text = _investor_round_statement_rule_based(
                archetype, self.idx, stance_info, round_num, prior
            )
            used_llm = False
        return DebateStatement(
            round_num=round_num,
            position=position,
            persona_key=key,
            persona_name=name,
            role=ROLE_INVESTOR,
            disposition=disposition,
            text=text,
            grounded_figures=list(stance_info.get("evidence") or []),
            used_llm=used_llm,
        )

    # ------------------------------------------------------------------
    def run(self, rounds: int = DEFAULT_DEBATE_ROUNDS) -> DebateTranscript:
        if rounds < 1:
            raise DebateError(f"rounds must be >= 1, got {rounds}")

        specs = self._persona_specs()
        all_rounds: List[List[DebateStatement]] = []
        prior: List[DebateStatement] = []   # every statement from rounds < current

        for round_num in range(1, rounds + 1):
            this_round: List[DebateStatement] = []
            for spec in specs:
                stmt = self._make_statement(spec, round_num, prior)
                this_round.append(stmt)
            all_rounds.append(this_round)
            # Only completed rounds feed the next one — the 6 statements within a
            # round are "simultaneous", which keeps the round reproducible.
            prior = prior + this_round
            logger.info(
                "debate %s round %d/%d: %d statements (gamma verdict=%s)",
                self.ticker, round_num, rounds, len(this_round),
                self._gamma_verdict["verdict"],
            )

        return DebateTranscript(
            ticker=self.ticker,
            as_of_date=self.as_of_date,
            rounds=rounds,
            personas=self.personas,
            statements=all_rounds,
            gamma_verdict=dict(self._gamma_verdict),
            used_llm=self.use_llm,
        )


def run_debate(
    seed: FilteredEntities,
    *,
    rounds: int = DEFAULT_DEBATE_ROUNDS,
    use_llm: bool = True,
    generator: Optional[OasisProfileGenerator] = None,
    model_name: Optional[str] = None,
) -> DebateTranscript:
    """Run the 6 Phase-3 personas through a `rounds`-round shared debate.

    Args:
        seed: `build_seed_from_ticker(...)` output (the seed graph).
        rounds: number of debate rounds (default `DEFAULT_DEBATE_ROUNDS` = 3).
        use_llm: True -> the LLM writes each statement's prose; False ->
            deterministic templates that still cite real figures and still
            reference prior rounds.
        generator: optional pre-built `OasisProfileGenerator` (mostly for tests).
        model_name: optional LLM model override.

    Returns:
        `DebateTranscript`. Gamma's verdict (`transcript.gamma_verdicts()`) is
        identical in every round by construction.
    """
    return DebateRoom(
        seed, use_llm=use_llm, generator=generator, model_name=model_name
    ).run(rounds=rounds)
