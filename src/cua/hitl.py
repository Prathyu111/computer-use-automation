"""Minimal same-session human handoff. Operator UI is mocked; the lock is real."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from cua.evidence import EvidenceSink
from cua.models import ControlLock
from cua.session import Session

HitlMode = Literal["approve", "takeover"]


@dataclass
class HitlOutcome:
    lock: ControlLock
    human_completed: bool
    mode: HitlMode
    value: str | None = None


class HumanIntervention:
    def __init__(self, session: Session, evidence: EvidenceSink) -> None:
        self.session = session
        self.evidence = evidence

    def request(
        self,
        reason: str,
        *,
        step_id: str | None = None,
        mode: HitlMode = "takeover",
        extra: dict[str, Any] | None = None,
        require_value: bool = False,
    ) -> HitlOutcome:
        before = self._snapshot_state()
        self.session.cede_to_human()
        ticket = {
            "reason": reason,
            "step_id": step_id,
            "mode": mode,
            "url_before": before.get("url"),
            "heading_before": before.get("heading"),
            "lock": self.session.lock.value,
            **(extra or {}),
        }
        self.evidence.log("hitl_request", **ticket)
        self.evidence.write_json("intervention_request.json", ticket)
        self._screenshot("hitl_before")

        auto_complete = os.environ.get("CUA_HITL_AUTO_COMPLETE", "0") == "1"
        auto_resume = os.environ.get("CUA_HITL_AUTO_RESUME", "0") == "1"
        if auto_complete or auto_resume:
            completed = auto_complete
            supplied = (os.environ.get("CUA_HITL_VALUE") or "").strip() or None
            if require_value and completed and not supplied:
                completed = False
            self.evidence.log(
                "hitl_auto",
                mode=mode,
                human_completed=completed,
                reason="CUA_HITL_AUTO_COMPLETE" if auto_complete else "CUA_HITL_AUTO_RESUME",
            )
            self.session.return_to_agent()
            self._log_after(completed)
            return HitlOutcome(
                self.session.lock,
                completed,
                mode,
                value=supplied if completed else None,
            )

        print("\n=== HUMAN INTERVENTION ===")
        print(f"Reason: {reason}")
        print(f"Mode: {mode} (same live browser session; automation is paused).")
        print("resume  = return control; agent may act")
        if require_value:
            print("done <value> = human completed extract with the supplied value")
            print("             (done without a value is refused)")
        else:
            print("done    = human completed this step; agent must not re-do it")
        print("abort   = stop the run\n")
        try:
            answer = input("Type resume, done, or abort: ").strip()
        except (EOFError, OSError):
            answer = "abort"
        lowered = answer.lower()
        if lowered == "resume":
            self.evidence.log("hitl_resume", mode=mode)
            self.session.return_to_agent()
            self._log_after(False)
            return HitlOutcome(self.session.lock, False, mode)
        if lowered == "abort":
            self.evidence.log("hitl_abort", mode=mode)
            self.session.pause()
            self._log_after(False)
            return HitlOutcome(self.session.lock, False, mode)
        verb, _, rest = answer.partition(" ")
        if verb.lower() in {"done", "complete"}:
            supplied = rest.strip() or None
            if require_value and not supplied:
                self.evidence.log("hitl_done_refused", mode=mode, reason="extract requires a value")
                self.session.return_to_agent()
                self._log_after(False)
                return HitlOutcome(self.session.lock, False, mode)
            self.evidence.log("hitl_done", mode=mode)
            self.session.return_to_agent()
            self._log_after(True)
            return HitlOutcome(self.session.lock, True, mode, value=supplied)
        self.evidence.log("hitl_abort", mode=mode)
        self.session.pause()
        self._log_after(False)
        return HitlOutcome(self.session.lock, False, mode)

    def _snapshot_state(self) -> dict[str, str | None]:
        page = self.session.page
        if page is None:
            return {"url": None, "heading": None}
        heading = None
        try:
            heading = page.locator("h1, h2").first.inner_text(timeout=500)
        except Exception:
            heading = None
        return {"url": page.url, "heading": heading}

    def _screenshot(self, label: str) -> None:
        page = self.session.page
        if page is None:
            return
        try:
            page.screenshot(path=str(self.evidence.screenshot_path(label)))
        except Exception:
            pass

    def _log_after(self, human_completed: bool) -> None:
        after = self._snapshot_state()
        self.evidence.log(
            "hitl_after",
            human_completed=human_completed,
            url_after=after.get("url"),
            heading_after=after.get("heading"),
            lock=self.session.lock.value,
        )
        self._screenshot("hitl_after")
