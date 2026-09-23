"""Increment 2 proofs: Tenant B overlay replay without rediscovery."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cua.mock_app import serve_in_thread
from cua.models import Capability, LocatorStrategy, ResultKind, Target, TenantOverlay
from cua.orchestrator import Orchestrator
from cua.specialization import apply_overlay

CANONICAL = Path("capabilities/local.mock_core.lookup_savings_balance.v1.0.0.json")
OVERLAY = Path("specializations/local.mock_core.lookup_savings_balance.tenant_b.json")
REPLAY_SRC = Path("src/cua/replay.py")


@pytest.fixture(scope="module")
def mock_server():
    os.environ["CUA_HEADLESS"] = "1"
    os.environ["CUA_HITL_AUTO_RESUME"] = "1"
    httpd = serve_in_thread(8765)
    yield
    httpd.shutdown()


def _replay_events(result) -> list[dict]:
    path = Path(result.evidence_ref) / "replay.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_tenant_a_canonical_still_succeeds(mock_server):
    result = Orchestrator().invoke(CANONICAL, {"memberId": "12345"})
    assert result.kind is ResultKind.success
    assert result.outputs["savingsBalance"] == "1840.22"
    assert result.capability_id == "local.mock_core.lookup_savings_balance"


def test_b_canonical_on_tenant_b_without_overlay_fails_closed(mock_server, tmp_path):
    cap = Capability.model_validate_json(CANONICAL.read_text(encoding="utf-8"))
    cap.entry = "http://127.0.0.1:8765/tenant-b/"
    for step in cap.steps:
        if step.id == "s1_goto_entry":
            step.url = cap.entry
    path = tmp_path / "canonical_on_tenant_b.json"
    path.write_text(cap.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.hard_failure
    assert result.code == "failed"
    assert result.kind is not ResultKind.business_outcome
    assert result.kind is not ResultKind.success
    assert "savingsBalance" not in result.outputs
    assert result.failed_step_id != "s5_extract"


def test_c_canonical_plus_overlay_succeeds_without_llm(mock_server, monkeypatch):
    def _no_llm(*_args, **_kwargs):
        raise AssertionError("LLM/discovery must not run on overlay replay")

    monkeypatch.setattr("cua.discovery.DiscoveryAgent.decide", _no_llm)
    result = Orchestrator().invoke(
        CANONICAL, {"memberId": "12345"}, overlay_path=OVERLAY
    )
    assert result.kind is ResultKind.success
    assert result.outputs["savingsBalance"] == "1840.22"
    assert result.capability_id == "local.mock_core.lookup_savings_balance"
    events = _replay_events(result)
    start = next(e for e in events if e.get("event") == "start")
    assert start.get("overlay_id") == "local.mock_core.lookup_savings_balance.tenant_b"
    assert "discover" not in json.dumps(events)


def test_d_incompatible_tenant_b_is_surface_mismatch_not_extract(mock_server, tmp_path):
    base = Capability.model_validate_json(CANONICAL.read_text(encoding="utf-8"))
    overlay = TenantOverlay.model_validate_json(OVERLAY.read_text(encoding="utf-8"))
    assert overlay.surface is not None
    overlay.surface.entry_url = "http://127.0.0.1:8765/tenant-b/drift/"
    applied = apply_overlay(base, overlay)
    path = tmp_path / "tenant_b_drift.json"
    path.write_text(applied.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.hard_failure
    assert result.code == "surface_mismatch"
    assert result.kind is not ResultKind.business_outcome
    assert result.failed_step_id == "s4_click"
    assert "savingsBalance" not in result.outputs


def test_e_loaded_canonical_unchanged_after_apply_and_replay(mock_server):
    disk_before = CANONICAL.read_text(encoding="utf-8")
    base = Capability.model_validate_json(disk_before)
    before = base.model_dump(mode="json")
    overlay = TenantOverlay.model_validate_json(OVERLAY.read_text(encoding="utf-8"))
    applied = apply_overlay(base, overlay)
    assert applied.tenant is not None
    result = Orchestrator().invoke(
        CANONICAL, {"memberId": "12345"}, overlay_path=OVERLAY
    )
    assert result.kind is ResultKind.success
    assert base.model_dump(mode="json") == before
    assert base.tenant is None
    s3 = next(s for s in base.steps if s.id == "s3_type")
    assert any(st.name == "Member ID" or st.label == "Member ID" for st in s3.target.strategies)
    assert not any(
        st.name == "Customer Number" or st.label == "Customer Number" for st in s3.target.strategies
    )
    assert base.success.heading_contains == "Account summary"
    assert base.provenance.discovery_run_id == "discover-f83a7518"
    assert base.id == "local.mock_core.lookup_savings_balance"
    assert base.version == "1.0.0"
    assert CANONICAL.read_text(encoding="utf-8") == disk_before


def test_tenant_a_locator_miss_is_failed_not_surface_mismatch(mock_server, tmp_path):
    cap = Capability.model_validate_json(CANONICAL.read_text(encoding="utf-8"))
    for step in cap.steps:
        if step.id == "s4_click":
            step.target = Target(
                intent="Missing Search",
                strategies=[LocatorStrategy(kind="a11y", role="button", name="No Such Control")],
            )
    path = tmp_path / "tenant_a_locator_miss.json"
    path.write_text(cap.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.hard_failure
    assert result.code == "failed"
    assert result.kind is not ResultKind.business_outcome
    assert result.failed_step_id == "s4_click"
    assert "savingsBalance" not in result.outputs


def test_required_frame_scope_miss_is_surface_mismatch(mock_server, tmp_path):
    base = Capability.model_validate_json(CANONICAL.read_text(encoding="utf-8"))
    overlay = TenantOverlay.model_validate_json(OVERLAY.read_text(encoding="utf-8"))
    assert overlay.surface is not None
    assert overlay.surface.frame_scope is not None
    overlay.surface.frame_scope.selector = 'iframe[name="example-frame"]'
    applied = apply_overlay(base, overlay)
    path = tmp_path / "tenant_b_unbound_frame.json"
    path.write_text(applied.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.hard_failure
    assert result.code == "surface_mismatch"
    assert result.kind is not ResultKind.business_outcome
    assert "savingsBalance" not in result.outputs


def test_specialized_checkpoint_miss_is_surface_mismatch(mock_server, tmp_path):
    base = Capability.model_validate_json(CANONICAL.read_text(encoding="utf-8"))
    overlay = TenantOverlay.model_validate_json(OVERLAY.read_text(encoding="utf-8"))
    assert overlay.surface is not None
    overlay.surface.entry_url = "http://127.0.0.1:8765/tenant-b/checkpoint-drift/"
    applied = apply_overlay(base, overlay)
    path = tmp_path / "tenant_b_checkpoint_drift.json"
    path.write_text(applied.model_dump_json(), encoding="utf-8")
    result = Orchestrator().invoke(path, {"memberId": "12345"})
    assert result.kind is ResultKind.hard_failure
    assert result.code == "surface_mismatch"
    assert result.kind is not ResultKind.business_outcome
    assert result.failed_step_id is None
    assert "savingsBalance" not in result.outputs


def test_replay_engine_is_tenant_blind():
    src = REPLAY_SRC.read_text(encoding="utf-8")
    assert "tenant_b" not in src
    assert "legacyCore" not in src
    assert "frame_locator" not in src
    assert "content_frame" not in src
    assert "iframe" not in src.lower()
    assert "cap.tenant" not in src
    assert "_contract_failure_code" not in src
    assert "_resolve_failure_code" not in src
