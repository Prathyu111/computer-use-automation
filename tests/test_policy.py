"""Policy and schema unit tests (no browser)."""

from pathlib import Path

from cua.models import Capability, ProposedAction, RiskClass
from cua.policy import PolicyGate
from cua.redact import redact_value
from cua.models import Sensitivity

POLICY = PolicyGate(Path("policy/allowlist.yaml"))


def test_blocks_off_allowlist_origin():
    action = ProposedAction(type="goto", url="https://evil.example/", intent="leave")
    decision = POLICY.check_proposed(action, "http://127.0.0.1:8765/")
    assert decision.allowed is False


def test_transfer_requires_hitl():
    action = ProposedAction(type="click", name="Transfer", intent="Transfer", risk=RiskClass.reversible)
    decision = POLICY.check_proposed(action, "http://127.0.0.1:8765/lookup")
    assert decision.allowed is True
    assert decision.require_hitl is True


def test_seed_capability_validates():
    path = Path("capabilities/local.mock_core.lookup_savings_balance.v1.0.0.json")
    cap = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    assert cap.id.endswith("lookup_savings_balance")
    assert any(o.code == "member_not_found" for o in cap.outcomes)


def test_redact_identifier():
    assert redact_value("memberId", "12345", Sensitivity.identifier) == "1…5"
    assert redact_value("ssn", "111223333", Sensitivity.secret) == "[REDACTED]"
