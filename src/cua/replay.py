"""Deterministic replay. No LLM in the decision loop."""

from __future__ import annotations

import re
from typing import Any

from cua.adapter import SurfaceAdapter, SurfaceMismatchError
from cua.evidence import EvidenceSink
from cua.hitl import HumanIntervention
from cua.models import (
    Capability,
    Checkpoint,
    ControlLock,
    Handler,
    ResultKind,
    RunResult,
    Step,
)
from cua.policy import PolicyGate
from cua.redact import redact_value
from cua.session import Session

_MAX_RECOVER = 2
_MONEY = re.compile(r"^-?\d+(?:\.\d+)?$")


def coerce_output(type_name: str, raw: str) -> str:
    text = (raw or "").strip()
    kind = (type_name or "string").lower()
    if kind == "money":
        cleaned = text.replace("$", "").replace(",", "").strip()
        if not _MONEY.fullmatch(cleaned):
            raise ValueError(f"not a money value: {raw!r}")
        return f"{float(cleaned):.2f}"
    if kind == "number":
        cleaned = text.replace(",", "").strip()
        float(cleaned)
        return cleaned
    if not text:
        raise ValueError("empty string")
    return text


def _meaningful_checkpoint(cp: Checkpoint | None) -> bool:
    if cp is None:
        return False
    return bool(cp.heading_contains or cp.text_contains or cp.url_contains)


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
        self._irreversible_ids = set(capability.irreversible_step_ids)
        self._hitl_retried: set[str] = set()

    def run(self) -> RunResult:
        self._validate_inputs()
        for step in self.cap.steps:
            if not self.session.can_act():
                return self._result(ResultKind.escalated, "escalated", "session lock is not agent")
            decision = self.policy.check_step(
                step, self.session.current_url(), self._irreversible_ids
            )
            self.evidence.log("policy", step=step.id, allowed=decision.allowed, reason=decision.reason)
            if not decision.allowed:
                return self._fail(step, "policy deny", decision.reason)
            if decision.require_hitl:
                outcome = self.hitl.request(
                    decision.reason,
                    step_id=step.id,
                    mode="approve",
                    require_value=step.action == "extract",
                )
                if outcome.lock is not ControlLock.agent:
                    return self._result(ResultKind.escalated, "escalated", decision.reason, step=step)
                if outcome.human_completed:
                    if step.action == "extract":
                        supplied = (outcome.value or "").strip()
                        if not supplied:
                            return self._fail(
                                step,
                                "extract requires a value",
                                "HITL done without extracted value",
                            )
                        if step.extract_to:
                            self.outputs[step.extract_to] = supplied
                        continue
                    self.evidence.log("step_skipped_human", step=step.id)
                    continue

            outcome = self._run_step(step)
            if outcome is not None:
                return outcome
            off = self._origin_failure(step)
            if off is not None:
                return off

        try:
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
        except (SurfaceMismatchError, LookupError) as exc:
            return self._fail_lookup(None, "terminal checkpoint failed", exc)
        try:
            safe_outputs = self._finalize_outputs()
        except ValueError as exc:
            return self._result(ResultKind.hard_failure, "failed", str(exc), expected="typed outputs")
        return self._result(ResultKind.success, "success", "ok", outputs=safe_outputs)

    def _run_step(self, step: Step) -> RunResult | None:
        value = self._resolve_value(step.value_from)
        self.evidence.log("step_start", step=step.id, action=step.action)
        if step.action == "dismiss":
            try:
                status = self.adapter.try_dismiss()
            except SurfaceMismatchError as exc:
                return self._fail(step, "dismiss failed", str(exc), code="surface_mismatch")
            except Exception as exc:
                return self._fail(step, "dismiss failed", str(exc))
            self.evidence.log("dismiss", step=step.id, status=status)
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
        except SurfaceMismatchError as exc:
            return self._fail(step, "act failed", str(exc), code="surface_mismatch")
        except Exception as exc:
            handled = self._apply_handlers(step, error=str(exc))
            if handled is not None:
                return handled
            return self._fail(
                step,
                "act failed",
                str(exc),
            )

        if step.extract_to and extracted is not None:
            self.outputs[step.extract_to] = extracted

        handled = self._apply_handlers(step)
        if handled is not None:
            return handled

        if _meaningful_checkpoint(step.checkpoint):
            cp = step.checkpoint
            assert cp is not None
            try:
                ok = self.adapter.checkpoint_ok(cp.heading_contains, cp.text_contains, cp.url_contains)
            except SurfaceMismatchError as exc:
                return self._fail(step, cp.description or "checkpoint", str(exc), code="surface_mismatch")
            except LookupError as exc:
                return self._fail(
                    step,
                    cp.description or "checkpoint",
                    str(exc),
                )
            if ok:
                return None
            handled = self._apply_handlers(step, checkpoint_failed=True)
            if handled is not None:
                return handled
            obs = self.adapter.observe()
            return self._fail(
                step,
                cp.description or "checkpoint",
                obs.heading or obs.body_text[:200],
            )
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
                    outcome = self.hitl.request(
                        then.reason or "handler escalate",
                        step_id=step.id,
                        mode="takeover",
                        require_value=step.action == "extract",
                    )
                    if outcome.lock is not ControlLock.agent:
                        return self._result(ResultKind.escalated, "escalated", then.reason or "", step=step)
                    if outcome.human_completed:
                        if step.action == "extract":
                            supplied = (outcome.value or "").strip()
                            if not supplied:
                                return self._fail(
                                    step,
                                    "extract requires a value",
                                    "HITL done without extracted value",
                                )
                            if step.extract_to:
                                self.outputs[step.extract_to] = supplied
                            return None
                        self.evidence.log("handler_human_completed", step=step.id)
                        return None
                    if step.id in self._hitl_retried:
                        return self._fail(step, "still blocked after HITL", error or "")
                    self._hitl_retried.add(step.id)
                    return self._run_step(step)
                if then.action == "recover":
                    recovered = self._recover(step, then.recover or "retry", error)
                    if recovered is not None:
                        return recovered
                    return self._apply_handlers(step)
        return None

    def _recover(self, step: Step, kind: str, error: str | None) -> RunResult | None:
        last = error or "recover"
        for attempt in range(1, _MAX_RECOVER + 1):
            self.evidence.log("recover", step=step.id, recover=kind, attempt=attempt)
            try:
                if kind == "dismiss":
                    status = self.adapter.try_dismiss()
                    self.evidence.log("recover_dismiss", step=step.id, status=status, attempt=attempt)
                extracted = self.adapter.act_step(
                    step.action,
                    step.target,
                    value=self._resolve_value(step.value_from),
                    url=step.url,
                    key=step.key,
                    timeout_ms=step.timeout_ms,
                )
                if step.extract_to and extracted is not None:
                    self.outputs[step.extract_to] = extracted
                return None
            except SurfaceMismatchError as exc:
                last = str(exc)
                return self._fail(step, "recovery exhausted", last, code="surface_mismatch")
            except Exception as exc:
                last = str(exc)
        return self._fail(
            step,
            "recovery exhausted",
            last,
        )

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

    def _finalize_outputs(self) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for spec in self.cap.outputs:
            raw = self.outputs.get(spec.name)
            if raw is None:
                if spec.required:
                    raise ValueError(f"missing required output {spec.name}")
                continue
            try:
                coerced = coerce_output(spec.type, str(raw))
            except (ValueError, TypeError) as exc:
                if spec.required:
                    raise ValueError(f"invalid {spec.type} output {spec.name}: {exc}") from exc
                continue
            self.evidence.log(
                "output",
                name=spec.name,
                value=redact_value(spec.name, coerced, spec.sensitivity),
            )
            safe[spec.name] = coerced
        return safe

    def _origin_failure(self, step: Step) -> RunResult | None:
        url = self.session.current_url()
        if self.policy.origin_allowed(url):
            return None
        return self._fail(step, "origin allowlist after navigation", url)

    def _fail_lookup(self, step: Step | None, expected: str, exc: BaseException) -> RunResult:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not code:
            code = "failed"
        if step is None:
            return self._result(
                ResultKind.hard_failure,
                code,
                str(exc),
                expected=expected,
                observed=str(exc),
            )
        return self._fail(step, expected, str(exc), code=code)

    def _fail(self, step: Step, expected: str, observed: str, *, code: str | None = None) -> RunResult:
        self._snapshot("failure")
        return self._result(
            ResultKind.hard_failure,
            code or "failed",
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
