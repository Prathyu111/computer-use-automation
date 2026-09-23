"""Focused tests for the assignment vertical-slice correctness pass."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cua.adapter import ObserveResult, SurfaceAdapter
from cua.compiler import CompileError
from cua.discovery import DiscoveryAgent
from cua.mock_app import serve_in_thread
from cua.models import (
    Capability,
    Checkpoint,
    LocatorStrategy,
    ProposedAction,
    ResultKind,
    RiskClass,
    Step,
    Target,
)
from cua.orchestrator import Orchestrator
from cua.redact import redact_for_log, redact_text
from cua.replay import coerce_output
from cua.session import Session
from compiled_lookup import compiled_lookup_capability, write_compiled_lookup

CAP = Path("capabilities/local.mock_core.lookup_savings_balance.v1.0.0.json")


@pytest.fixture(scope="module")
def mock_server():
    os.environ["CUA_HEADLESS"] = "1"
    os.environ["CUA_HITL_AUTO_RESUME"] = "1"
    os.environ.pop("CUA_HITL_AUTO_COMPLETE", None)
    httpd = serve_in_thread(8765)
    yield
    httpd.shutdown()


class _ScriptedAgent:
    def __init__(self, adapter, goal, entry, actions):
        self.adapter = adapter
        self.goal = goal
        self.entry = entry
        self.model_id = "scripted-test-model"
        self._actions = list(actions)

    def decide(self, obs, step_index, last_action=None, last_outcome=None, **_kwargs):
        if step_index >= len(self._actions):
            return ProposedAction(type="stuck", stuck_reason="exhausted script")
        return self._actions[step_index]


def test_discover_success_compiles(mock_server, monkeypatch, tmp_path):
    actions = [
        ProposedAction(type="dismiss", name="OK", role="button", intent="dismiss"),
        ProposedAction(type="type", name="Member ID", role="textbox", value="12345", intent="member"),
        ProposedAction(type="click", name="Search", role="button", intent="Search"),
        ProposedAction(type="done", done_reason="goal met"),
    ]

    def factory(adapter, goal, entry):
        return _ScriptedAgent(adapter, goal, entry, actions)

    monkeypatch.setattr("cua.orchestrator.DiscoveryAgent", factory)
    original = CAP.read_text(encoding="utf-8")
    try:
        path = Orchestrator().discover("Look up savings", "http://127.0.0.1:8765/")
        cap = Capability.model_validate_json(path.read_text(encoding="utf-8"))
        assert cap.provenance.model_id == "scripted-test-model"
        assert any(s.value_from == "$input.memberId" for s in cap.steps)
    finally:
        CAP.write_text(original, encoding="utf-8")


def test_discover_stuck_does_not_compile(mock_server, monkeypatch):
    os.environ.pop("CUA_HITL_AUTO_RESUME", None)
    os.environ.pop("CUA_HITL_AUTO_COMPLETE", None)

    def factory(adapter, goal, entry):
        return _ScriptedAgent(
            adapter,
            goal,
            entry,
            [ProposedAction(type="stuck", stuck_reason="cannot proceed")],
        )

    monkeypatch.setattr("cua.orchestrator.DiscoveryAgent", factory)
    with pytest.raises(CompileError, match="did not complete"):
        Orchestrator().discover("Look up savings", "http://127.0.0.1:8765/", max_steps=3)
    os.environ["CUA_HITL_AUTO_RESUME"] = "1"


def test_discover_exhausted_does_not_compile(mock_server, monkeypatch):
    def factory(adapter, goal, entry):
        return _ScriptedAgent(
            adapter,
            goal,
            entry,
            [ProposedAction(type="dismiss", name="OK", role="button", intent="dismiss")] * 5,
        )

    monkeypatch.setattr("cua.orchestrator.DiscoveryAgent", factory)
    with pytest.raises(CompileError, match="exhausted"):
        Orchestrator().discover("Look up savings", "http://127.0.0.1:8765/", max_steps=2)


def test_replay_business_vs_hard_failure(mock_server, tmp_path):
    orch = Orchestrator()
    lookup = write_compiled_lookup(tmp_path / "lookup.json")
    business = orch.invoke(lookup, {"memberId": "99999"})
    assert business.kind is ResultKind.business_outcome
    cap = compiled_lookup_capability()
    broken = cap.model_copy(deep=True)
    broken.steps = [
        s
        if s.extract_to != "savingsBalance"
        else s.model_copy(
            update={
                "target": Target(
                    intent="Missing",
                    strategies=[LocatorStrategy(kind="a11y", role="button", name="No Such Control")],
                )
            }
        )
        for s in broken.steps
    ]
    path = tmp_path / "broken.json"
    path.write_text(broken.model_dump_json(), encoding="utf-8")
    hard = orch.invoke(path, {"memberId": "12345"})
    assert hard.kind is ResultKind.hard_failure
    assert hard.code == "failed"


def test_checkpoint_url_is_enforced(mock_server, tmp_path):
    cap = compiled_lookup_capability()
    cap.steps[0] = cap.steps[0].model_copy(
        update={"checkpoint": Checkpoint(description="must stay on mock", url_contains="not-this-host")}
    )
    path = tmp_path / "cp.json"
    path.write_text(cap.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.hard_failure
    assert result.failed_step_id == cap.steps[0].id


def test_ambiguous_locator_rejected(mock_server):
    os.environ.setdefault("CUA_BROWSER_CHANNEL", "chrome")
    session = Session(headless=True)
    try:
        page = session.start("http://127.0.0.1:8765/")
        page.set_content("<button>Search</button><button>Search</button>")
        adapter = SurfaceAdapter(page)
        with pytest.raises(LookupError, match="expected 1 match"):
            adapter.resolve(
                Target(
                    intent="Search",
                    strategies=[LocatorStrategy(kind="a11y", role="button", name="Search")],
                )
            )
    finally:
        session.close()


def test_typed_output_validation():
    assert coerce_output("money", "1,840.22") == "1840.22"
    with pytest.raises(ValueError):
        coerce_output("money", "not-a-price")


def test_policy_denial_on_invoke(mock_server, tmp_path):
    cap = compiled_lookup_capability()
    cap.steps[0] = cap.steps[0].model_copy(update={"url": "https://evil.example/"})
    path = tmp_path / "deny.json"
    path.write_text(cap.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.hard_failure
    assert "allowlist" in (result.expected or result.message).lower() or "allowlist" in (result.observed or "").lower()


def test_auto_resume_does_not_execute_policy_required_transfer(mock_server, tmp_path, monkeypatch):
    os.environ.pop("CUA_HITL_AUTO_COMPLETE", None)
    os.environ["CUA_HITL_AUTO_RESUME"] = "1"
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: "abort")
    clicked: list[str] = []
    orig = SurfaceAdapter.act_step

    def wrapped(self, action, target=None, **kwargs):
        if action == "click" and target is not None:
            clicked.append(target.intent or "")
        return orig(self, action, target, **kwargs)

    monkeypatch.setattr(SurfaceAdapter, "act_step", wrapped)
    cap = compiled_lookup_capability()
    cap.steps.append(
        Step(
            id="s_xfer",
            action="click",
            risk=RiskClass.irreversible,
            on_irreversible="require_hitl",
            target=Target(
                intent="Transfer",
                strategies=[LocatorStrategy(kind="a11y", role="button", name="Transfer")],
            ),
        )
    )
    cap.irreversible_step_ids = ["s_xfer"]
    path = tmp_path / "xfer_auto_resume.json"
    path.write_text(cap.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.escalated
    assert "Transfer" not in clicked
    events = [
        json.loads(line)
        for line in (Path(result.evidence_ref) / "replay.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "hitl_request" and e.get("mode") == "approve" for e in events)
    assert any(e.get("event") == "hitl_abort" for e in events)
    assert not any(e.get("event") == "hitl_auto" for e in events)
    assert not any(
        e.get("event") == "hitl_auto" and e.get("reason") == "CUA_HITL_AUTO_RESUME" for e in events
    )


def test_hitl_complete_skips_reexecution(mock_server, tmp_path):
    os.environ["CUA_HITL_AUTO_COMPLETE"] = "1"
    cap = compiled_lookup_capability()
    cap.steps.append(
        Step(
            id="s_xfer",
            action="click",
            risk=RiskClass.irreversible,
            on_irreversible="require_hitl",
            target=Target(
                intent="Transfer",
                strategies=[LocatorStrategy(kind="a11y", role="button", name="Transfer")],
            ),
        )
    )
    cap.irreversible_step_ids = ["s_xfer"]
    path = tmp_path / "hitl.json"
    path.write_text(cap.model_dump_json(), encoding="utf-8")
    try:
        result = Orchestrator().invoke(path, {"memberId": "12345"})
        assert result.kind is ResultKind.success
    finally:
        os.environ.pop("CUA_HITL_AUTO_COMPLETE", None)


def test_redaction_of_identifiers_and_nested_decide():
    assert "[REDACTED]" in redact_text("member 12345 is here")
    dumped = redact_for_log("action", {"type": "type", "value": "12345"})
    assert dumped["value"] != "12345"


def test_type_fills_input_not_table_cell(mock_server):
    """Reproduce: unique visible text in a <td> next to an input; type must fill the input."""
    session = Session(headless=True)
    try:
        page = session.start("http://127.0.0.1:8765/")
        page.set_content(
            """
            <table>
              <tr>
                <td>Member ID</td>
                <td><input id="the-field" /></td>
              </tr>
            </table>
            """
        )
        adapter = SurfaceAdapter(page)
        adapter.act_proposed(
            ProposedAction(
                type="type",
                role="textbox",
                name="Member ID",
                intent="Enter member ID to look up",
                value="12345",
            )
        )
        assert page.locator("#the-field").input_value() == "12345"
        assert page.locator("td", has_text="Member ID").inner_text() == "Member ID"
    finally:
        session.close()


def test_recovery_exhaustion(mock_server, tmp_path):
    from cua.models import Handler, HandlerThen, WhenCondition

    cap = compiled_lookup_capability()
    click = next(
        s
        for s in cap.steps
        if s.action == "click" and s.target and "search" in s.target.intent.lower()
    )
    click.target = Target(
        intent="No Button",
        strategies=[LocatorStrategy(kind="a11y", role="button", name="Definitely Missing")],
    )
    click.handlers = [
        Handler(
            when=WhenCondition(kind="target_missing"),
            then=HandlerThen(action="recover", recover="retry"),
        )
    ]
    path = tmp_path / "retry.json"
    path.write_text(cap.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.hard_failure
    assert result.expected == "recovery exhausted"


class _RecordingAgent(_ScriptedAgent):
    def __init__(self, adapter, goal, entry, actions, feedback):
        super().__init__(adapter, goal, entry, actions)
        self.feedback = feedback

    def decide(self, obs, step_index, last_action=None, last_outcome=None, **kwargs):
        self.feedback.append(
            {
                "step_index": step_index,
                "last_action": last_action,
                "last_outcome": last_outcome,
                "obs": obs,
            }
        )
        return super().decide(
            obs, step_index, last_action=last_action, last_outcome=last_outcome, **kwargs
        )


def test_observation_reports_filled_without_raw_identifier(mock_server):
    session = Session(headless=True)
    try:
        page = session.start("http://127.0.0.1:8765/")
        page.set_content(
            """
            <label for="member">Member ID</label>
            <input id="member" aria-label="Member ID" value="12345" />
            <button>Search</button>
            """
        )
        adapter = SurfaceAdapter(page)
        obs = adapter.observe()
        payload = adapter.observation_for_llm(obs)
        textboxes = [c for c in payload["controls"] if c.get("role") == "textbox"]
        assert textboxes
        assert textboxes[0].get("filled") is True
        assert "12345" not in json.dumps(payload["controls"])
        assert "12345" not in json.dumps(obs.controls)
    finally:
        session.close()


def test_next_decide_receives_previous_executed_outcome(mock_server, monkeypatch):
    feedback: list[dict] = []
    actions = [
        ProposedAction(type="dismiss", name="OK", role="button", intent="dismiss"),
        ProposedAction(type="type", name="Member ID", role="textbox", value="12345", intent="member"),
        ProposedAction(type="click", name="Search", role="button", intent="Search"),
        ProposedAction(type="done", done_reason="goal met"),
    ]

    def factory(adapter, goal, entry):
        return _RecordingAgent(adapter, goal, entry, actions, feedback)

    monkeypatch.setattr("cua.orchestrator.DiscoveryAgent", factory)
    original = CAP.read_text(encoding="utf-8")
    try:
        Orchestrator().discover("Look up savings", "http://127.0.0.1:8765/")
    finally:
        CAP.write_text(original, encoding="utf-8")

    assert feedback[0]["last_outcome"] is None
    assert feedback[0]["last_action"] is None
    executed = [c for c in feedback[1:] if c["last_outcome"] == "executed"]
    assert executed
    assert executed[0]["last_action"].type == "dismiss"
    typed = [c for c in feedback if c["last_action"] is not None and c["last_action"].type == "type"]
    assert typed
    assert typed[0]["last_outcome"] == "executed"


def test_human_completed_hitl_is_distinct_outcome(mock_server, monkeypatch):
    os.environ["CUA_HITL_AUTO_COMPLETE"] = "1"
    feedback: list[dict] = []
    actions = [
        ProposedAction(type="click", name="Transfer", intent="Transfer"),
        ProposedAction(type="done", done_reason="human finished irreversible step"),
    ]

    def factory(adapter, goal, entry):
        return _RecordingAgent(adapter, goal, entry, actions, feedback)

    monkeypatch.setattr("cua.orchestrator.DiscoveryAgent", factory)
    try:
        with pytest.raises(CompileError):
            Orchestrator().discover("Look up savings", "http://127.0.0.1:8765/", max_steps=3)
    finally:
        os.environ.pop("CUA_HITL_AUTO_COMPLETE", None)

    assert any(c["last_outcome"] == "human_completed" for c in feedback)
    hitl_calls = [c for c in feedback if c["last_outcome"] == "human_completed"]
    assert hitl_calls[0]["last_action"].type == "click"
    assert hitl_calls[0]["last_action"].name == "Transfer"
    assert all(c["last_outcome"] != "executed" for c in hitl_calls)


def test_sensitive_values_redacted_from_model_and_evidence_payloads(monkeypatch):
    dumped = redact_for_log("action", {"type": "type", "value": "12345", "name": "Member ID"})
    assert dumped["value"] != "12345"
    assert "12345" not in json.dumps(dumped)

    captured: dict[str, str] = {}

    class _StubAdapter:
        def observation_for_llm(self, obs):
            return {
                "url": obs.url,
                "title": obs.title,
                "heading": obs.heading,
                "dialog": None,
                "controls": obs.controls,
                "visible_text": "",
            }

    agent = DiscoveryAgent(_StubAdapter(), goal="Look up", entry="http://127.0.0.1:8765/")

    def fake_complete(user):
        captured["user"] = user
        return json.dumps({"type": "done", "done_reason": "ok"})

    agent._complete = fake_complete
    obs = ObserveResult(
        url="http://127.0.0.1:8765/",
        title="",
        heading="",
        body_text="",
        controls=[{"role": "textbox", "name": "Member ID", "filled": True}],
        dialog=None,
    )
    agent.decide(
        obs,
        1,
        last_action=ProposedAction(
            type="type", name="Member ID", role="textbox", value="12345", intent="member"
        ),
        last_outcome="executed",
    )
    user = captured["user"]
    assert "12345" not in user
    payload = json.loads(user)
    assert payload["last_outcome"] == "executed"
    assert payload["last_action"]["type"] == "type"
    assert payload["last_action"]["value"] != "12345"
    assert payload["observation"]["controls"][0]["filled"] is True
    assert "12345" not in json.dumps(payload["observation"])


def test_extract_uses_name_not_intent_sentence(mock_server):
    session = Session(headless=True)
    try:
        page = session.start("http://127.0.0.1:8765/")
        page.set_content(
            """
            <table>
              <tr><td>Checking</td><td>10.00</td></tr>
              <tr><td>Savings</td><td>1840.22</td></tr>
            </table>
            """
        )
        adapter = SurfaceAdapter(page)
        extracted = adapter.act_proposed(
            ProposedAction(
                type="extract",
                name="Savings",
                extract_to="savingsBalance",
                intent="Read the current Savings balance from the account summary after search",
            )
        )
        assert extracted == "1840.22"
    finally:
        session.close()


def test_extract_without_name_or_text_does_not_use_intent(mock_server):
    session = Session(headless=True)
    try:
        page = session.start("http://127.0.0.1:8765/")
        page.set_content(
            """
            <table>
              <tr><td>Savings</td><td>1840.22</td></tr>
            </table>
            """
        )
        adapter = SurfaceAdapter(page)
        intent = "Read the current Savings balance from the account summary after search"
        with pytest.raises(LookupError) as excinfo:
            adapter.act_proposed(
                ProposedAction(
                    type="extract",
                    name=None,
                    text=None,
                    extract_to="savingsBalance",
                    intent=intent,
                )
            )
        assert intent not in str(excinfo.value)
    finally:
        session.close()


def test_observation_includes_redacted_labeled_pairs(mock_server):
    session = Session(headless=True)
    try:
        page = session.start("http://127.0.0.1:8765/")
        page.set_content(
            """
            <table>
              <tr><td>Reference</td><td>12345</td></tr>
              <tr><td>Status</td><td>Open</td></tr>
            </table>
            """
        )
        adapter = SurfaceAdapter(page)
        obs = adapter.observe()
        payload = adapter.observation_for_llm(obs)
        labels = {p["label"] for p in payload["labeled_fields"]}
        assert "Reference" in labels
        assert "Status" in labels
        dumped = json.dumps(payload["labeled_fields"])
        assert "12345" not in dumped
        ref = next(p for p in payload["labeled_fields"] if p["label"] == "Reference")
        assert ref["value"]
        assert ref["value"] != "12345"
    finally:
        session.close()


def test_hitl_done_without_value_is_not_successful_extract(mock_server, monkeypatch):
    os.environ["CUA_HITL_AUTO_COMPLETE"] = "1"
    os.environ.pop("CUA_HITL_VALUE", None)
    feedback: list[dict] = []
    actions = [
        ProposedAction(
            type="extract",
            name="Savings",
            extract_to="savingsBalance",
            intent="read Savings from the summary",
        ),
        ProposedAction(type="done", done_reason="goal met"),
    ]

    def factory(adapter, goal, entry):
        return _RecordingAgent(adapter, goal, entry, actions, feedback)

    monkeypatch.setattr("cua.orchestrator.DiscoveryAgent", factory)
    try:
        with pytest.raises(CompileError):
            Orchestrator().discover("Look up savings", "http://127.0.0.1:8765/", max_steps=3)
    finally:
        os.environ.pop("CUA_HITL_AUTO_COMPLETE", None)

    extract_feedback = [
        c for c in feedback if c["last_action"] is not None and c["last_action"].type == "extract"
    ]
    assert extract_feedback
    assert all(c["last_outcome"] != "human_completed" for c in extract_feedback)
    assert extract_feedback[0]["last_outcome"] == "act_failed"


def test_successful_extract_feeds_next_decide_without_raw_value(mock_server, monkeypatch):
    from cua.compiler import compile_capability as real_compile

    feedback: list[dict] = []
    recorded_holder: dict[str, list] = {}
    dest = "outputField"
    extract_action = ProposedAction(
        type="extract",
        name="Name",
        extract_to=dest,
        intent="read the labeled name field from the current view",
    )
    actions = [
        ProposedAction(type="dismiss", name="OK", role="button", intent="dismiss"),
        ProposedAction(type="type", name="Member ID", role="textbox", value="12345", intent="member"),
        ProposedAction(type="click", name="Search", role="button", intent="Search"),
        extract_action,
        ProposedAction(type="done", done_reason="goal met"),
    ]

    def factory(adapter, goal, entry):
        return _RecordingAgent(adapter, goal, entry, actions, feedback)

    def capturing_compile(**kwargs):
        recorded_holder["steps"] = kwargs["recorded_steps"]
        return real_compile(**kwargs)

    monkeypatch.setattr("cua.orchestrator.DiscoveryAgent", factory)
    monkeypatch.setattr("cua.orchestrator.compile_capability", capturing_compile)
    original = CAP.read_text(encoding="utf-8")
    try:
        Orchestrator().discover("Look up savings", "http://127.0.0.1:8765/")
    finally:
        CAP.write_text(original, encoding="utf-8")

    after_extract = [
        c
        for c in feedback
        if c["last_action"] is not None and c["last_action"].type == "extract"
    ]
    assert after_extract
    nxt = after_extract[0]
    assert nxt["last_outcome"] == "extracted"
    assert nxt["last_action"].extract_to == dest
    dumped = nxt["last_action"].model_dump()
    assert "extracted" not in dumped
    raw = next(
        rec.get("extracted")
        for rec in recorded_holder["steps"]
        if rec.get("type") == "extract" and rec.get("extract_to") == dest
    )
    assert raw
    assert dumped.get("value") != raw
    blob = json.dumps(dumped)
    assert raw not in blob

    captured: dict[str, str] = {}

    class _StubAdapter:
        def observation_for_llm(self, obs):
            return {"url": obs.url, "controls": [], "visible_text": ""}

    agent = DiscoveryAgent(_StubAdapter(), goal="Look up", entry="http://127.0.0.1:8765/")

    def fake_complete(user):
        captured["user"] = user
        return json.dumps({"type": "done", "done_reason": "ok"})

    agent._complete = fake_complete
    obs = ObserveResult(
        url="http://127.0.0.1:8765/",
        title="",
        heading="",
        body_text="",
        controls=[],
        dialog=None,
    )
    agent.decide(obs, 4, last_action=extract_action, last_outcome="extracted")
    payload = json.loads(captured["user"])
    assert payload["last_outcome"] == "extracted"
    assert payload["last_action"]["type"] == "extract"
    assert payload["last_action"]["extract_to"] == dest
    assert "extracted" not in payload["last_action"]
    assert raw not in captured["user"]


def test_lookup_error_reports_strategy_label_not_intent(mock_server):
    session = Session(headless=True)
    intent = "Read the current labeled field from the account summary after search"
    try:
        page = session.start("http://127.0.0.1:8765/")
        page.set_content("<table><tr><td>Status</td><td>Open</td></tr></table>")
        adapter = SurfaceAdapter(page)
        with pytest.raises(LookupError) as excinfo:
            adapter.resolve(
                Target(
                    intent=intent,
                    strategies=[LocatorStrategy(kind="labeled_readonly", label="MissingField")],
                )
            )
        msg = str(excinfo.value)
        assert "MissingField" in msg
        assert intent not in msg
    finally:
        session.close()
