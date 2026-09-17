"""Owns discover vs invoke. Does not talk to the app except through the adapter path."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

from cua.adapter import SurfaceAdapter
from cua.compiler import compile_lookup_capability
from cua.discovery import DiscoveryAgent
from cua.evidence import EvidenceSink
from cua.hitl import HumanIntervention
from cua.models import (
    Capability,
    ControlLock,
    ProposedAction,
    ResultKind,
    RunResult,
)
from cua.policy import PolicyGate
from cua.replay import ReplayEngine
from cua.session import Session

load_dotenv()

_ROOT = Path(__file__).resolve().parents[2]
_CAP_DIR = _ROOT / "capabilities"


class Orchestrator:
    def __init__(self) -> None:
        self.policy = PolicyGate()
        _CAP_DIR.mkdir(exist_ok=True)

    def discover(self, goal: str, target: str, max_steps: int = 12) -> Path:
        run_id = f"discover-{uuid.uuid4().hex[:8]}"
        evidence = EvidenceSink(run_id, "discovery")
        evidence.log("start", goal=goal, target=target)
        headless = os.environ.get("CUA_HEADLESS", "1") != "0"
        session = Session(headless=headless)
        try:
            session.start(target)
            adapter = SurfaceAdapter(session.page)
            hitl = HumanIntervention(session, evidence)
            agent = DiscoveryAgent(adapter, goal=goal, entry=target)
            recorded: list[dict] = []
            for i in range(max_steps):
                if not session.can_act():
                    break
                obs = adapter.observe()
                evidence.log("observe", url=obs.url, heading=obs.heading, dialog=obs.dialog)
                action = agent.decide(obs, i)
                evidence.log("decide", action=action.model_dump())
                if action.type == "done":
                    evidence.log("done", reason=action.done_reason)
                    break
                if action.type == "stuck":
                    hitl.request(action.stuck_reason or "discovery stuck")
                    break
                self._act_proposed(action, session, adapter, hitl, evidence, recorded)
            cap = compile_lookup_capability(
                run_id=run_id,
                model_id=os.environ.get("OPENAI_MODEL"),
                entry=target,
                recorded_steps=recorded,
                parameterized_member=True,
            )
            path = _CAP_DIR / f"{cap.id}.v{cap.version}.json"
            path.write_text(cap.model_dump_json(indent=2), encoding="utf-8")
            evidence.write_json("capability.json", json.loads(path.read_text(encoding="utf-8")))
            evidence.log("compiled", path=str(path))
            return path
        finally:
            session.close()

    def invoke(self, capability_path: Path, params: dict[str, str]) -> RunResult:
        run_id = f"replay-{uuid.uuid4().hex[:8]}"
        evidence = EvidenceSink(run_id, "replay")
        cap = Capability.model_validate_json(capability_path.read_text(encoding="utf-8"))
        evidence.log("start", capability=cap.id, params={k: "***" if k.lower().endswith("id") else v for k, v in params.items()})
        headless = os.environ.get("CUA_HEADLESS", "1") != "0"
        session = Session(headless=headless)
        try:
            session.start(cap.entry)
            adapter = SurfaceAdapter(session.page)
            hitl = HumanIntervention(session, evidence)
            engine = ReplayEngine(
                capability=cap,
                params=params,
                session=session,
                adapter=adapter,
                policy=self.policy,
                evidence=evidence,
                hitl=hitl,
                run_id=run_id,
            )
            result = engine.run()
            evidence.write_json("result.json", result.model_dump())
            return result
        finally:
            session.close()

    def _act_proposed(
        self,
        action: ProposedAction,
        session: Session,
        adapter: SurfaceAdapter,
        hitl: HumanIntervention,
        evidence: EvidenceSink,
        recorded: list[dict],
    ) -> None:
        decision = self.policy.check_proposed(action, session.current_url())
        evidence.log("policy", allowed=decision.allowed, reason=decision.reason)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        if decision.require_hitl:
            lock = hitl.request(decision.reason)
            if lock is not ControlLock.agent:
                return
        adapter.act_proposed(action)
        recorded.append(action.model_dump())
