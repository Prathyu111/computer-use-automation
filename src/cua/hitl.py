"""Minimal same-session human handoff. Operator UI is mocked; the lock is real."""

from __future__ import annotations

import os
from typing import Any

from cua.evidence import EvidenceSink
from cua.models import ControlLock
from cua.session import Session


class HumanIntervention:
    def __init__(self, session: Session, evidence: EvidenceSink) -> None:
        self.session = session
        self.evidence = evidence

    def request(
        self,
        reason: str,
        *,
        step_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ControlLock:
        self.session.cede_to_human()
        ticket = {
            "reason": reason,
            "step_id": step_id,
            "url": self.session.current_url() if self.session.page else None,
            "lock": self.session.lock.value,
            **(extra or {}),
        }
        self.evidence.log("hitl_request", **ticket)
        self.evidence.write_json("intervention_request.json", ticket)

        page = self.session.page
        if page is not None:
            path = self.evidence.screenshot_path("hitl")
            try:
                page.screenshot(path=str(path))
            except Exception:
                pass

        auto = os.environ.get("CUA_HITL_AUTO_RESUME", "0") == "1"
        if auto:
            self.evidence.log("hitl_auto_resume", reason="CUA_HITL_AUTO_RESUME=1")
            self.session.return_to_agent()
            return self.session.lock

        print("\n=== HUMAN INTERVENTION ===")
        print(f"Reason: {reason}")
        print("Operate the same live browser window (automation is paused).")
        print("Then return control to the agent.\n")
        try:
            answer = input("Type resume to return control, abort to stop: ").strip().lower()
        except EOFError:
            answer = "abort"
        if answer == "resume":
            self.evidence.log("hitl_resume")
            self.session.return_to_agent()
        else:
            self.evidence.log("hitl_abort")
            self.session.pause()
        return self.session.lock
