"""Deterministic replay. No LLM in the decision loop."""

from __future__ import annotations

from typing import Any

from cua.adapter import SurfaceAdapter
from cua.evidence import EvidenceSink
from cua.hitl import HumanIntervention
from cua.models import (
    Capability,
    ControlLock,
    Handler,
    ResultKind,
    RunResult,
    Step,
)
from cua.policy import PolicyGate
from cua.redact import redact_value
from cua.session import Session


class ReplayEngine:
    def __init__(
        self,
        capability: Capability,
        params: dict[str, str],
        session: Session,
        adapter: SurfaceAdapter,
        policy: PolicyGate,
        evidence: EvidenceSink,
        hitl: HumanIntervention,
        run_id: str,
    ) -> None:
        self.cap = capability
        self.params = params
        self.session = session
        self.adapter = adapter
        self.policy = policy
        self.evidence = evidence
        self.hitl = hitl
        self.run_id = run_id
        self.outputs: dict[str, Any] = {}

    def run(self) -> RunResult:
        self._validate_inputs()
        for step in self.cap.steps:
            if not self.session.can_act():
                return self._result(ResultKind.escalated, "escalated", "session lock is not agent")
            decision = self.policy.check_step(step, self.session.current_url())
            self.evidence.log("policy", step=step.id, allowed=decision.allowed, reason=decision.reason)
            if not decision.allowed:
                return self._fail(step, "policy deny", decision.reason)
            if decision.require_hitl:
                lock = self.hitl.request(decision.reason, step_id=step.id)
                if lock is not ControlLock.agent:
                    return self._result(ResultKind.escalated, "escalated", decision.reason, step=step)

            outcome = self._run_step(step)
            if outcome is not None:
                return outcome

        if not self.adapter.checkpoint_ok(
            self.cap.success.heading_contains,
            self.cap.success.text_contains,
            self.cap.success.url_contains,
        ):
            obs = self.adapter.observe()
            return self._result(
                ResultKind.hard_failure,
                "failed",
                "terminal checkpoint failed",
                expected=self.cap.success.description,
                observed=obs.heading or obs.body_text[:200],
            )
        for spec in self.cap.outputs:
            if spec.required and spec.name not in self.outputs:
                return self._result(
                    ResultKind.hard_failure,
                    "failed",
                    f"missing required output {spec.name}",
                )
        safe_outputs = {}
        for spec in self.cap.outputs:
            if spec.name in self.outputs:
                val = str(self.outputs[spec.name])
                self.evidence.log(
                    "output",
                    name=spec.name,
                    value=redact_value(spec.name, val, spec.sensitivity),
                )
                safe_outputs[spec.name] = self.outputs[spec.name]
        return self._result(ResultKind.success, "success", "ok", outputs=safe_outputs)

    def _run_step(self, step: Step) -> RunResult | None:
        value = self._resolve_value(step.value_from)
        self.evidence.log("step_start", step=step.id, action=step.action)
        if step.action == "dismiss":
            try:
                self.adapter.act_step("dismiss", step.target, timeout_ms=1500)
            except Exception:
                self.evidence.log("recover", step=step.id, reason="no interstitial")
            return None
        try:
            extracted = self.adapter.act_step(
                step.action,
                step.target,
                value=value,
                url=step.url,
                key=step.key,
                timeout_ms=step.timeout_ms,
            )
        except Exception as exc:
            handled = self._apply_handlers(step, error=str(exc))
            if handled is not None:
                return handled
            return self._fail(step, "act failed", str(exc))

        if step.extract_to and extracted is not None:
            self.outputs[step.extract_to] = extracted

        handled = self._apply_handlers(step)
        if handled is not None:
            return handled

        if step.checkpoint and step.checkpoint.heading_contains:
            # After search, either summary or a declared business banner.
            if self.adapter.checkpoint_ok(
                step.checkpoint.heading_contains,
                step.checkpoint.text_contains,
                step.checkpoint.url_contains,
            ):
                return None
            handled = self._apply_handlers(step, checkpoint_failed=True)
            if handled is not None:
                return handled
        return None

    def _apply_handlers(
        self,
        step: Step,
        *,
        error: str | None = None,
        checkpoint_failed: bool = False,
    ) -> RunResult | None:
        for handler in step.handlers:
            if self._when_matches(handler, error=error, checkpoint_failed=checkpoint_failed):
                then = handler.then
                self.evidence.log("handler", step=step.id, then=then.action, code=then.code)
                if then.action == "return_outcome":
                    kind = then.kind or ResultKind.business_outcome
                    return self._result(kind, then.code or "unknown", then.reason or then.code or "", step=step)
                if then.action == "fail":
                    return self._fail(step, then.reason or "handler fail", error or "")
                if then.action == "escalate":
                    lock = self.hitl.request(then.reason or "handler escalate", step_id=step.id)
                    if lock is not ControlLock.agent:
                        return self._result(ResultKind.escalated, "escalated", then.reason or "", step=step)
                    return None
                if then.action == "recover":
                    return None
        if error:
            return None
        return None

    def _when_matches(
        self,
        handler: Handler,
        *,
        error: str | None,
        checkpoint_failed: bool,
    ) -> bool:
        when = handler.when
        if when.kind == "text_matches" and when.text:
            return self.adapter.text_matches(when.text)
        if when.kind == "checkpoint_failed":
            return checkpoint_failed
        if when.kind == "timeout":
            return bool(error and "timeout" in error.lower())
        if when.kind == "target_missing":
            return bool(error)
        if when.kind == "dialog" and when.text:
            obs = self.adapter.observe()
            return bool(obs.dialog and when.text.lower() in obs.dialog.lower())
        return False

    def _resolve_value(self, value_from: str | None) -> str | None:
        if not value_from:
            return None
        if value_from.startswith("$input."):
            key = value_from.split(".", 1)[1]
            return self.params[key]
        return value_from

    def _validate_inputs(self) -> None:
        for spec in self.cap.inputs:
            if spec.required and spec.name not in self.params:
                raise ValueError(f"missing required input {spec.name}")

    def _fail(self, step: Step, expected: str, observed: str) -> RunResult:
        self._snapshot("failure")
        return self._result(
            ResultKind.hard_failure,
            "failed",
            observed,
            step=step,
            expected=expected,
            observed=observed,
        )

    def _snapshot(self, label: str) -> None:
        if self.session.page is None:
            return
        try:
            self.session.page.screenshot(path=str(self.evidence.screenshot_path(label)))
        except Exception:
            pass

    def _result(
        self,
        kind: ResultKind,
        code: str,
        message: str,
        *,
        step: Step | None = None,
        expected: str | None = None,
        observed: str | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> RunResult:
        if kind is not ResultKind.success:
            self._snapshot(kind.value)
        return RunResult(
            kind=kind,
            code=code,
            capability_id=self.cap.id,
            capability_version=self.cap.version,
            run_id=self.run_id,
            outputs=outputs or {},
            failed_step_id=step.id if step else None,
            expected=expected,
            observed=observed,
            evidence_ref=str(self.evidence.dir),
            control=self.session.lock,
            message=message,
        )
