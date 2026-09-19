"""Replay against the mock core: success + business outcomes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cua.hitl import HumanIntervention
from cua.mock_app import serve_in_thread
from cua.models import ResultKind
from cua.orchestrator import Orchestrator

from compiled_lookup import write_compiled_lookup


@pytest.fixture(scope="module")
def mock_server():
    os.environ["CUA_HEADLESS"] = "1"
    os.environ["CUA_HITL_AUTO_RESUME"] = "1"
    httpd = serve_in_thread(8765)
    yield
    httpd.shutdown()


@pytest.fixture
def lookup_cap_path(tmp_path):
    return write_compiled_lookup(tmp_path / "lookup.json")


def _replay_events(result) -> list[dict]:
    path = Path(result.evidence_ref) / "replay.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_replay_success(mock_server, lookup_cap_path):
    result = Orchestrator().invoke(lookup_cap_path, {"memberId": "12345"})
    assert result.kind is ResultKind.success
    assert result.outputs["savingsBalance"] == "1840.22"
    assert result.outputs["memberName"] == "Jane Doe"


def test_replay_search_notice_recovers_then_success(mock_server, lookup_cap_path):
    result = Orchestrator().invoke(lookup_cap_path, {"memberId": "12345"})
    assert result.kind is ResultKind.success
    assert result.outputs["savingsBalance"] == "1840.22"
    events = _replay_events(result)
    assert any(e.get("event") == "recover" and e.get("recover") == "dismiss" for e in events)
    assert any(e.get("event") == "handler" and e.get("then") == "recover" for e in events)
    assert not any(e.get("event") == "handler" and e.get("then") == "escalate" for e in events)
    assert any(e.get("event") == "dismiss" and e.get("step") == "s2_dismiss" for e in events)


def test_replay_not_found_is_business_outcome(mock_server, lookup_cap_path):
    result = Orchestrator().invoke(lookup_cap_path, {"memberId": "99999"})
    assert result.kind is ResultKind.business_outcome
    assert result.code == "member_not_found"
    assert not result.outputs


def test_replay_session_expired_is_hard_failure(mock_server, lookup_cap_path):
    result = Orchestrator().invoke(lookup_cap_path, {"memberId": "00000"})
    assert result.kind is ResultKind.hard_failure
    assert result.code == "session_expired"
    assert result.failed_step_id == "s4_click"
    assert not result.outputs
    shots = list(Path(result.evidence_ref).glob("*.png"))
    assert shots


def test_replay_compiled_trace(mock_server, tmp_path):
    from cua.compiler import compile_capability

    cap = compile_capability(
        run_id="compiled",
        model_id="test",
        entry="http://127.0.0.1:8765/",
        goal="Look up savings",
        recorded_steps=[
            {"type": "goto", "url": "http://127.0.0.1:8765/", "intent": "open"},
            {"type": "dismiss", "name": "OK", "role": "button", "intent": "OK"},
            {"type": "type", "name": "Member ID", "role": "textbox", "value": "12345", "intent": "member"},
            {"type": "click", "name": "Search", "role": "button", "intent": "Search"},
            {"type": "extract", "extract_to": "savingsBalance"},
            {"type": "extract", "extract_to": "memberName"},
        ],
        last_heading="Account summary",
        last_text="Savings",
    )
    path = tmp_path / "cap.json"
    path.write_text(cap.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.success
    assert result.outputs["savingsBalance"] == "1840.22"


def test_replay_permission_denied(mock_server, lookup_cap_path):
    result = Orchestrator().invoke(lookup_cap_path, {"memberId": "40301"})
    assert result.kind is ResultKind.business_outcome
    assert result.code == "permission_denied"


class _DismissHitlThenResume:
    """Test-only HITL: click the supervisor dialog OK on the live page, then resume."""

    def __init__(self, session, evidence):
        self._inner = HumanIntervention(session, evidence)
        self.session = session
        self.evidence = evidence

    def request(self, reason, **kwargs):
        page = self.session.page
        if page is not None:
            btn = page.locator(".overlay[data-hitl] button")
            if btn.count() and btn.first.is_visible():
                btn.first.click()
        return self._inner.request(reason, **kwargs)


def test_replay_hitl_member_escalates_resume_then_success(mock_server, lookup_cap_path, monkeypatch):
    os.environ.pop("CUA_HITL_AUTO_COMPLETE", None)
    os.environ["CUA_HITL_AUTO_RESUME"] = "1"
    monkeypatch.setattr("cua.orchestrator.HumanIntervention", _DismissHitlThenResume)
    result = Orchestrator().invoke(lookup_cap_path, {"memberId": "11111"})
    assert result.kind is ResultKind.success
    assert result.outputs["savingsBalance"] == "250.00"
    events = _replay_events(result)
    assert any(e.get("event") == "handler" and e.get("then") == "escalate" for e in events)
    assert any(e.get("event") == "hitl_request" for e in events)
    assert any(e.get("event") == "hitl_after" and e.get("lock") == "agent" for e in events)
    assert not any(e.get("event") == "handler" and e.get("then") == "recover" for e in events)
