"""Unit tests for typed tenant overlay apply (no Tenant B UI, no canonical writes)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.models import (
    Capability,
    Checkpoint,
    LocatorStrategy,
    OverlaySurface,
    StepOverride,
    Target,
    TenantOverlay,
)
from cua.specialization import SpecializationError, apply_overlay

CANONICAL = Path("capabilities/local.mock_core.lookup_savings_balance.v1.0.0.json")

S3 = "s3_type"
S4 = "s4_click"
S5 = "s5_extract"


def _load_canonical() -> Capability:
    return Capability.model_validate_json(CANONICAL.read_text(encoding="utf-8"))


def _control_target(*, intent: str, role: str, name: str) -> Target:
    return Target(
        intent=intent,
        strategies=[
            LocatorStrategy(kind="a11y", role=role, name=name),
            LocatorStrategy(kind="labeled_control", label=name),
        ],
    )


def _extract_target(*, intent: str, label: str) -> Target:
    return Target(
        intent=intent,
        strategies=[
            LocatorStrategy(kind="labeled_readonly", label=label),
            LocatorStrategy(kind="structural", row_key=label, target_cell=1),
        ],
    )


def _tenant_b_overlay() -> TenantOverlay:
    return TenantOverlay(
        schema_version="tenant_overlay/v1",
        id="local.mock_core.tenant_b",
        base_capability_id="local.mock_core.lookup_savings_balance",
        base_version="1.0.0",
        app_compat="v1",
        surface=OverlaySurface(
            entry_url="http://127.0.0.1:8765/tenant-b/",
            frame_scope={"kind": "iframe", "selector": 'iframe[name="example-frame"]'},
        ),
        step_overrides={
            S3: StepOverride(
                target=_control_target(
                    intent="Enter the member ID to look up",
                    role="textbox",
                    name="Customer Number",
                ),
            ),
            S4: StepOverride(
                target=_control_target(
                    intent="Click the Search button to look up the member",
                    role="button",
                    name="Find Member",
                ),
                handler_when_text={"validation_rejected": "Customer Number is required"},
            ),
            S5: StepOverride(
                target=_extract_target(
                    intent="Extract the current savings balance",
                    label="Share Savings",
                ),
            ),
        },
        success=Checkpoint(
            description="Account details with share savings row",
            heading_contains="Account Details",
            text_contains="Share Savings",
        ),
    )


def test_canonical_loads_with_tenant_null():
    cap = _load_canonical()
    assert cap.tenant is None
    assert cap.id == "local.mock_core.lookup_savings_balance"


def test_apply_overlay_does_not_mutate_base():
    base = _load_canonical()
    before = base.model_dump(mode="json")
    overlay = _tenant_b_overlay()
    applied = apply_overlay(base, overlay)
    assert base.model_dump(mode="json") == before
    assert applied is not base
    assert applied.tenant is not overlay


def test_invalid_base_id_rejected():
    base = _load_canonical()
    overlay = _tenant_b_overlay()
    overlay.base_capability_id = "other.capability"
    with pytest.raises(SpecializationError) as exc:
        apply_overlay(base, overlay)
    assert exc.value.code == "capability_drift"


def test_invalid_base_version_rejected():
    base = _load_canonical()
    overlay = _tenant_b_overlay()
    overlay.base_version = "9.9.9"
    with pytest.raises(SpecializationError) as exc:
        apply_overlay(base, overlay)
    assert exc.value.code == "capability_drift"


def test_invalid_app_compat_rejected():
    base = _load_canonical()
    overlay = _tenant_b_overlay()
    overlay.app_compat = "v9"
    with pytest.raises(SpecializationError) as exc:
        apply_overlay(base, overlay)
    assert exc.value.code == "capability_drift"


def test_unknown_step_override_rejected():
    base = _load_canonical()
    overlay = _tenant_b_overlay()
    overlay.step_overrides["s99_new"] = StepOverride(url="http://127.0.0.1:8765/nope")
    with pytest.raises(SpecializationError) as exc:
        apply_overlay(base, overlay)
    assert exc.value.code == "capability_drift"


def test_missing_pins_rejected():
    base = _load_canonical()
    overlay = TenantOverlay(id="no-pins")
    with pytest.raises(SpecializationError) as exc:
        apply_overlay(base, overlay)
    assert exc.value.code == "capability_drift"


def test_apply_remaps_presentation_tokens():
    base = _load_canonical()
    applied = apply_overlay(base, _tenant_b_overlay())
    s3 = next(s for s in applied.steps if s.id == S3)
    s4 = next(s for s in applied.steps if s.id == S4)
    s5 = next(s for s in applied.steps if s.id == S5)
    names_labels_s3 = {(st.name, st.label) for st in s3.target.strategies}
    assert ("Customer Number", None) in names_labels_s3 or any(
        st.name == "Customer Number" or st.label == "Customer Number" for st in s3.target.strategies
    )
    assert any(st.name == "Find Member" or st.label == "Find Member" for st in s4.target.strategies)
    assert any(st.label == "Share Savings" or st.row_key == "Share Savings" for st in s5.target.strategies)
    assert applied.success.heading_contains == "Account Details"
    assert applied.success.text_contains == "Share Savings"
    assert applied.entry == "http://127.0.0.1:8765/tenant-b/"
    s1 = next(s for s in applied.steps if s.id == "s1_goto_entry")
    assert s1.url == "http://127.0.0.1:8765/tenant-b/"


def test_inputs_outputs_outcomes_provenance_identical():
    base = _load_canonical()
    before = base.model_dump(mode="json")
    applied = apply_overlay(base, _tenant_b_overlay())
    dumped = applied.model_dump(mode="json")
    for key in ("id", "version", "inputs", "outputs", "outcomes", "provenance", "allowlist_ref"):
        assert dumped[key] == before[key]
    assert [s["id"] for s in dumped["steps"]] == [s["id"] for s in before["steps"]]
    assert [s["action"] for s in dumped["steps"]] == [s["action"] for s in before["steps"]]
    assert [s["risk"] for s in dumped["steps"]] == [s["risk"] for s in before["steps"]]


def test_handler_when_text_remaps_validation_rejected_only():
    base = _load_canonical()
    before_s4 = next(s for s in base.steps if s.id == S4)
    before_handlers = [(h.then.code, h.then.kind, h.then.action, h.when.text) for h in before_s4.handlers]
    applied = apply_overlay(base, _tenant_b_overlay())
    s4 = next(s for s in applied.steps if s.id == S4)
    after_handlers = [(h.then.code, h.then.kind, h.then.action, h.when.text) for h in s4.handlers]
    assert len(after_handlers) == len(before_handlers)
    for (b_code, b_kind, b_action, b_text), (a_code, a_kind, a_action, a_text) in zip(
        before_handlers, after_handlers
    ):
        assert a_code == b_code
        assert a_kind == b_kind
        assert a_action == b_action
        if b_code == "validation_rejected":
            assert a_text == "Customer Number is required"
            assert b_text != a_text
        else:
            assert a_text == b_text


def test_handler_when_text_unknown_code_rejected():
    base = _load_canonical()
    overlay = _tenant_b_overlay()
    overlay.step_overrides[S4].handler_when_text["not_an_outcome"] = "nope"
    with pytest.raises(SpecializationError) as exc:
        apply_overlay(base, overlay)
    assert exc.value.code == "capability_drift"


def test_step_override_rejects_extra_action_field():
    with pytest.raises(ValidationError):
        StepOverride.model_validate({"action": "click"})


def test_step_override_rejects_handler_outcome_semantics():
    with pytest.raises(ValidationError):
        StepOverride.model_validate({"then": {"action": "return_outcome", "code": "ok"}})
    with pytest.raises(ValidationError):
        StepOverride.model_validate(
            {"handler_then": {"action": "return_outcome", "code": "ok"}}
        )


def test_tenant_overlay_rejects_unknown_top_level_field():
    with pytest.raises(ValidationError):
        TenantOverlay.model_validate({"inputs": []})
    with pytest.raises(ValidationError):
        TenantOverlay.model_validate({"foo": "bar"})


def test_overlay_surface_rejects_unknown_field():
    with pytest.raises(ValidationError):
        OverlaySurface.model_validate({"entry_url": "http://127.0.0.1:8765/", "allowlist": []})
