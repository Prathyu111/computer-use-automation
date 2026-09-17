"""Replay against the mock core: success + business outcomes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cua.mock_app import serve_in_thread
from cua.models import ResultKind
from cua.orchestrator import Orchestrator

CAP = Path("capabilities/local.mock_core.lookup_savings_balance.v1.0.0.json")


@pytest.fixture(scope="module")
def mock_server():
    os.environ["CUA_HEADLESS"] = "1"
    os.environ["CUA_HITL_AUTO_RESUME"] = "1"
    httpd = serve_in_thread(8765)
    yield
    httpd.shutdown()


def test_replay_success(mock_server):
    result = Orchestrator().invoke(CAP, {"memberId": "12345"})
    assert result.kind is ResultKind.success
    assert result.outputs["savingsBalance"] == "1840.22"
    assert result.outputs["memberName"] == "Jane Doe"


def test_replay_not_found_is_business_outcome(mock_server):
    result = Orchestrator().invoke(CAP, {"memberId": "99999"})
    assert result.kind is ResultKind.business_outcome
    assert result.code == "member_not_found"


def test_replay_permission_denied(mock_server):
    result = Orchestrator().invoke(CAP, {"memberId": "40301"})
    assert result.kind is ResultKind.business_outcome
    assert result.code == "permission_denied"
