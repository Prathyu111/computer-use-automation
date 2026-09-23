"""Owns discover vs invoke. Does not talk to the app except through the adapter path."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

from cua.adapter import SurfaceAdapter
from cua.compiler import CompileError, compile_capability
from cua.discovery import DiscoveryAgent
from cua.evidence import EvidenceSink
from cua.hitl import HumanIntervention
from cua.models import (
    Capability,
    ControlLock,
    ProposedAction,
    RunResult,
    TenantOverlay,
)
from cua.policy import PolicyGate
from cua.redact import redact_value
from cua.replay import ReplayEngine
from cua.session import Session
from cua.specialization import apply_overlay

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")
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
            last_heading = ""
            last_text = ""
            last_action: ProposedAction | None = None
            last_outcome: str | None = None
            termination = "exhausted"
            for i in range(max_steps):
                if not session.can_act():
                    termination = "aborted"
                    break
                obs = adapter.observe()
                last_heading, last_text = obs.heading, obs.body_text
                evidence.log(
                    "observe",
                    url=obs.url,
                    heading=obs.heading,
                    dialog=obs.dialog,
                    controls=obs.controls,
                    labeled_fields=obs.labeled_fields,
                )
                action = agent.decide(
                    obs,
                    i,
                    last_action=last_action,
                    last_outcome=last_outcome,
                )
                evidence.log("decide", action=action.model_dump())
                if action.type == "done":
                    evidence.log("done", reason=action.done_reason)
                    termination = "done"
                    break
                if action.type == "stuck":
                    outcome = hitl.request(action.stuck_reason or "discovery stuck", mode="takeover")
                    if outcome.lock is not ControlLock.agent:
                        termination = "aborted"
                        break
                    last_action = action
                    last_outcome = (
                        "human_completed" if outcome.human_completed else "resumed"
                    )
                    continue
                last_action = action
                last_outcome = self._act_proposed(
                    action, session, adapter, hitl, evidence, recorded
                )
            obs = adapter.observe()
            last_heading, last_text = obs.heading, obs.body_text
            if termination != "done":
                evidence.log("discover_incomplete", termination=termination)
                raise CompileError(f"discovery did not complete successfully ({termination})")
            cap = compile_capability(
                run_id=run_id,
                model_id=agent.model_id,
                entry=target,
                goal=goal,
                recorded_steps=recorded,
                last_heading=last_heading,
                last_text=last_text,
            )
            path = _CAP_DIR / f"{cap.id}.v{cap.version}.json"
            path.write_text(cap.model_dump_json(indent=2), encoding="utf-8")
            evidence.write_json("capability.json", json.loads(path.read_text(encoding="utf-8")))
            evidence.log("compiled", path=str(path), steps=len(cap.steps), model_id=agent.model_id)
            return path
        finally:
            session.close()

    def invoke(
        self,
        capability_path: Path,
        params: dict[str, str],
        overlay_path: Path | None = None,
    ) -> RunResult:
        run_id = f"replay-{uuid.uuid4().hex[:8]}"
        evidence = EvidenceSink(run_id, "replay")
        loaded = Capability.model_validate_json(capability_path.read_text(encoding="utf-8"))
        cap = loaded
        overlay_id: str | None = None
        if overlay_path is not None:
            overlay = TenantOverlay.model_validate_json(
                Path(overlay_path).read_text(encoding="utf-8")
            )
            cap = apply_overlay(loaded, overlay)
            overlay_id = overlay.id
        start_payload: dict = {"capability": cap.id, "params": params}
        if overlay_id:
            start_payload["overlay_id"] = overlay_id
        evidence.log("start", **start_payload)
        headless = os.environ.get("CUA_HEADLESS", "1") != "0"
        session = Session(headless=headless)
        try:
            session.start(cap.entry)
            tenant = cap.tenant
            frame_scope = None
            if tenant is not None and tenant.surface is not None:
                frame_scope = tenant.surface.frame_scope
            adapter = SurfaceAdapter(
                session.page,
                frame_scope=frame_scope,
                surface_contract=tenant is not None,
            )
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
            dump = result.model_dump()
            for spec in cap.outputs:
                if spec.name in dump.get("outputs", {}):
                    dump["outputs"][spec.name] = redact_value(
                        spec.name, str(dump["outputs"][spec.name]), spec.sensitivity
                    )
            evidence.write_json("result.json", dump)
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
    ) -> str:
        decision = self.policy.check_proposed(action, session.current_url())
        evidence.log("policy", allowed=decision.allowed, reason=decision.reason)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        if decision.require_hitl:
            outcome = hitl.request(
                decision.reason,
                mode="approve",
                require_value=action.type == "extract",
            )
            if outcome.lock is not ControlLock.agent:
                return "aborted"
            if outcome.human_completed:
                rec = {**action.model_dump(), "completed_by": "human"}
                if not self._record_human_extract(action, outcome.value, rec):
                    return "act_failed"
                recorded.append(rec)
                return "human_completed"
        try:
            extracted = adapter.act_proposed(action)
        except PermissionError:
            raise
        except Exception as exc:
            evidence.log("act_failed", error=str(exc), action_type=action.type)
            outcome = hitl.request(
                f"act failed: {exc}",
                mode="takeover",
                require_value=action.type == "extract",
            )
            if outcome.lock is not ControlLock.agent:
                return "aborted"
            if outcome.human_completed:
                rec = {**action.model_dump(), "completed_by": "human"}
                if not self._record_human_extract(action, outcome.value, rec):
                    return "act_failed"
                recorded.append(rec)
                return "human_completed"
            return "act_failed"
        if not self.policy.origin_allowed(session.current_url()):
            raise PermissionError(f"navigated off allowlist: {session.current_url()}")
        rec = action.model_dump()
        if action.type == "extract":
            rec["extracted"] = extracted
            recorded.append(rec)
            return "extracted"
        recorded.append(rec)
        return "executed"

    @staticmethod
    def _record_human_extract(
        action: ProposedAction, supplied: str | None, rec: dict
    ) -> bool:
        if action.type != "extract":
            return True
        value = (supplied or "").strip()
        if not value:
            return False
        rec["extracted"] = value
        return True
