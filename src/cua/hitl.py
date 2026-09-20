"""Minimal same-session human handoff. Operator UI is mocked; the lock is real."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from cua.evidence import EvidenceSink
from cua.models import ControlLock
from cua.session import Session

HitlMode = Literal["approve", "takeover"]

REPORTED_UI_ACTIONS = (
    "acknowledged_supervisor_dialog",
    "dismissed_dialog",
    "completed_pending_step",
    "other",
)


@dataclass
class HitlOutcome:
    lock: ControlLock
    human_completed: bool
    mode: HitlMode
    value: str | None = None


def parse_reported_ui_action(raw: str) -> str | None:
    text = raw.strip().lower().replace(" ", "_")
    if not text:
        return None
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(REPORTED_UI_ACTIONS):
            return REPORTED_UI_ACTIONS[idx - 1]
        return None
    if text in REPORTED_UI_ACTIONS:
        return text
    return None


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
        # AUTO_RESUME is test-only for takeover/stuck HITL; it cannot satisfy
        # mode="approve". AUTO_COMPLETE is also test-only: it skips re-execution
        # (simulated human completion) and does not authorize the agent to execute
        # the policy-gated action. It is not verified real human activity.
        if auto_complete or (auto_resume and mode != "approve"):
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
            reported, note = self._prompt_reported_action()
            self._log_human_action(
                choice="resume",
                mode=mode,
                human_completed=False,
                before=before,
                reported_ui_action=reported,
                operator_note=note,
            )
            self.evidence.log("hitl_resume", mode=mode)
            self.session.return_to_agent()
            self._log_after(False)
            return HitlOutcome(self.session.lock, False, mode)
        if lowered == "abort":
            self._log_human_action(
                choice="abort",
                mode=mode,
                human_completed=False,
                before=before,
            )
            self.evidence.log("hitl_abort", mode=mode)
            self.session.pause()
            self._log_after(False)
            return HitlOutcome(self.session.lock, False, mode)
        verb, _, rest = answer.partition(" ")
        if verb.lower() in {"done", "complete"}:
            supplied = rest.strip() or None
            if require_value and not supplied:
                self._log_human_action(
                    choice="done_refused",
                    mode=mode,
                    human_completed=False,
                    before=before,
                )
                self.evidence.log("hitl_done_refused", mode=mode, reason="extract requires a value")
                self.session.return_to_agent()
                self._log_after(False)
                return HitlOutcome(self.session.lock, False, mode)
            reported, note = self._prompt_reported_action()
            self._log_human_action(
                choice="done",
                mode=mode,
                human_completed=True,
                before=before,
                reported_ui_action=reported,
                operator_note=note,
            )
            self.evidence.log("hitl_done", mode=mode)
            self.session.return_to_agent()
            self._log_after(True)
            return HitlOutcome(self.session.lock, True, mode, value=supplied)
        self._log_human_action(
            choice="abort",
            mode=mode,
            human_completed=False,
            before=before,
        )
        self.evidence.log("hitl_abort", mode=mode)
        self.session.pause()
        self._log_after(False)
        return HitlOutcome(self.session.lock, False, mode)

    def _prompt_reported_action(self) -> tuple[str, str | None]:
        while True:
            print("What did you do in the live UI?")
            print("  1 acknowledged_supervisor_dialog")
            print("  2 dismissed_dialog")
            print("  3 completed_pending_step")
            print("  4 other")
            try:
                raw = input().strip()
            except (EOFError, OSError):
                raw = ""
            parsed = parse_reported_ui_action(raw)
            if parsed:
                break
            print(
                "Enter 1-4 or a label: "
                "acknowledged_supervisor_dialog | dismissed_dialog | completed_pending_step | other"
            )
        try:
            note = input("Optional one-line note (Enter to skip): ").strip() or None
        except (EOFError, OSError):
            note = None
        if note:
            note = note[:200]
        return parsed, note

    def _log_human_action(
        self,
        *,
        choice: str,
        mode: HitlMode,
        human_completed: bool,
        before: dict[str, str | None],
        reported_ui_action: str | None = None,
        operator_note: str | None = None,
    ) -> None:
        after = self._snapshot_state()
        payload: dict[str, Any] = {
            "actor": "human",
            "source": "cli",
            "choice": choice,
            "operator_note": operator_note,
            "mode": mode,
            "lock": self.session.lock.value,
            "human_completed": human_completed,
            "url_before": before.get("url"),
            "heading_before": before.get("heading"),
            "url_after": after.get("url"),
            "heading_after": after.get("heading"),
            "page_changed": before.get("url") != after.get("url")
            or before.get("heading") != after.get("heading"),
        }
        if reported_ui_action is not None:
            payload["reported_ui_action"] = reported_ui_action
        self.evidence.log("human_action", **payload)

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
