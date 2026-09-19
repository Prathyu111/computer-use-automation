"""HITL CLI reporting: operator-stated UI action, not inferred clicks."""

from __future__ import annotations

import os

from cua.evidence import EvidenceSink
from cua.hitl import HumanIntervention
from cua.session import Session


def _request_with_inputs(monkeypatch, answers, *, run_id: str, require_value: bool = False):
    os.environ.pop("CUA_HITL_AUTO_RESUME", None)
    os.environ.pop("CUA_HITL_AUTO_COMPLETE", None)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: next(it))
    session = Session(headless=True)
    evidence = EvidenceSink(run_id, "hitl")
    outcome = HumanIntervention(session, evidence).request(
        "unit", mode="takeover", require_value=require_value
    )
    return outcome, evidence, session


def test_hitl_resume_without_label_reprompts_then_logs(monkeypatch):
    outcome, evidence, session = _request_with_inputs(
        monkeypatch,
        ["resume", "nope", "acknowledged_supervisor_dialog", "clicked OK"],
        run_id="replay-hitl-unit-reprompt",
    )
    assert outcome.human_completed is False
    assert session.lock.value == "agent"
    ha = [e for e in evidence.events if e["event"] == "human_action"]
    assert len(ha) == 1
    assert ha[0]["actor"] == "human"
    assert ha[0]["choice"] == "resume"
    assert ha[0]["reported_ui_action"] == "acknowledged_supervisor_dialog"
    assert ha[0]["operator_note"] == "clicked OK"
    assert ha[0]["lock"] == "human"
    assert any(e["event"] == "hitl_resume" for e in evidence.events)


def test_hitl_abort_without_label_ok(monkeypatch):
    outcome, evidence, session = _request_with_inputs(
        monkeypatch,
        ["abort"],
        run_id="replay-hitl-unit-abort",
    )
    assert outcome.human_completed is False
    assert session.lock.value == "paused"
    ha = [e for e in evidence.events if e["event"] == "human_action"]
    assert len(ha) == 1
    assert ha[0]["actor"] == "human"
    assert ha[0]["choice"] == "abort"
    assert ha[0]["lock"] == "human"
    assert "reported_ui_action" not in ha[0]
    assert any(e["event"] == "hitl_abort" for e in evidence.events)


def test_hitl_done_logs_reported_ui_action(monkeypatch):
    outcome, evidence, session = _request_with_inputs(
        monkeypatch,
        ["done", "completed_pending_step", "ok"],
        run_id="replay-hitl-unit-done",
        require_value=False,
    )
    assert outcome.human_completed is True
    assert session.lock.value == "agent"
    ha = [e for e in evidence.events if e["event"] == "human_action"]
    assert len(ha) == 1
    assert ha[0]["actor"] == "human"
    assert ha[0]["choice"] == "done"
    assert ha[0]["reported_ui_action"] == "completed_pending_step"
    assert ha[0]["lock"] == "human"
    assert ha[0]["operator_note"] == "ok"
    assert any(e["event"] == "hitl_done" for e in evidence.events)


def test_hitl_operator_note_truncated_to_200(monkeypatch):
    long_note = "n" * 250
    _, evidence, _ = _request_with_inputs(
        monkeypatch,
        ["resume", "other", long_note],
        run_id="replay-hitl-unit-note-len",
    )
    ha = [e for e in evidence.events if e["event"] == "human_action"]
    assert len(ha) == 1
    assert ha[0]["operator_note"] == "n" * 200
    assert len(ha[0]["operator_note"]) == 200


def test_hitl_done_refused_omits_reported_ui_action(monkeypatch):
    outcome, evidence, session = _request_with_inputs(
        monkeypatch,
        ["done"],
        run_id="replay-hitl-unit-done-refused",
        require_value=True,
    )
    assert outcome.human_completed is False
    assert session.lock.value == "agent"
    ha = [e for e in evidence.events if e["event"] == "human_action"]
    assert len(ha) == 1
    assert ha[0]["choice"] == "done_refused"
    assert ha[0]["actor"] == "human"
    assert ha[0]["lock"] == "human"
    assert "reported_ui_action" not in ha[0]
    assert any(e["event"] == "hitl_done_refused" for e in evidence.events)


def test_hitl_auto_resume_does_not_log_actor_human():
    os.environ["CUA_HITL_AUTO_RESUME"] = "1"
    os.environ.pop("CUA_HITL_AUTO_COMPLETE", None)
    session = Session(headless=True)
    evidence = EvidenceSink("replay-hitl-unit-auto", "hitl")
    try:
        HumanIntervention(session, evidence).request("unit")
    finally:
        os.environ.pop("CUA_HITL_AUTO_RESUME", None)
        os.environ["CUA_HITL_AUTO_RESUME"] = "1"
    assert not any(e.get("event") == "human_action" for e in evidence.events)
    assert not any(e.get("actor") == "human" for e in evidence.events)
    assert any(e.get("event") == "hitl_auto" for e in evidence.events)
