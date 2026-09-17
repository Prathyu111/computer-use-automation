"""Capability artifact and run-result contracts.

The artifact is a callable capability, not an LLM transcript.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Sensitivity(str, Enum):
    none = "none"
    identifier = "identifier"
    secret = "secret"
    pii = "pii"


class RiskClass(str, Enum):
    reversible = "reversible"
    irreversible = "irreversible"


class ControlLock(str, Enum):
    agent = "agent"
    paused = "paused"
    human = "human"


class ResultKind(str, Enum):
    success = "success"
    business_outcome = "business_outcome"
    hard_failure = "hard_failure"
    escalated = "escalated"


class LocatorStrategy(BaseModel):
    kind: Literal["a11y", "labeled_control", "name_in_scope", "structural", "css"]
    role: str | None = None
    name: str | None = None
    label: str | None = None
    text: str | None = None
    scope: str | None = None
    css: str | None = None
    # structural: row whose cell 0 equals a value, then click/extract cell N
    row_key_cell: int | None = None
    row_key: str | None = None
    target_cell: int | None = None


class Target(BaseModel):
    intent: str
    match: Literal["one"] = "one"
    strategies: list[LocatorStrategy] = Field(min_length=1)


class FieldSpec(BaseModel):
    name: str
    type: str = "string"
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.none
    description: str = ""
    example_placeholder: str | None = None


class OutcomeSpec(BaseModel):
    kind: ResultKind
    code: str
    description: str = ""


class WhenCondition(BaseModel):
    kind: Literal[
        "text_matches",
        "target_present",
        "target_missing",
        "dialog",
        "checkpoint_failed",
        "timeout",
    ]
    text: str | None = None
    scope: str | None = None
    dialog_role: str | None = None
    target: Target | None = None


class HandlerThen(BaseModel):
    action: Literal["return_outcome", "recover", "escalate", "fail"]
    code: str | None = None
    kind: ResultKind | None = None
    recover: Literal["dismiss", "retry"] | None = None
    reason: str | None = None


class Handler(BaseModel):
    when: WhenCondition
    then: HandlerThen


class Checkpoint(BaseModel):
    description: str
    heading_contains: str | None = None
    text_contains: str | None = None
    url_contains: str | None = None


class Step(BaseModel):
    id: str
    action: Literal[
        "goto",
        "click",
        "type",
        "clear_and_type",
        "select",
        "press_key",
        "dismiss",
        "extract",
        "wait_until",
    ]
    target: Target | None = None
    value_from: str | None = None  # $input.memberId or literal
    extract_to: str | None = None
    url: str | None = None
    key: str | None = None
    risk: RiskClass = RiskClass.reversible
    on_irreversible: Literal["require_hitl", "block"] = "require_hitl"
    checkpoint: Checkpoint | None = None
    handlers: list[Handler] = Field(default_factory=list)
    timeout_ms: int = 8000


class AppIdentity(BaseModel):
    vendor: str = "local"
    product: str = "mock_core"
    surface_kind: Literal["web", "desktop"] = "web"
    compat: str = "v1"


class TenantOverlay(BaseModel):
    id: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class Provenance(BaseModel):
    discovery_run_id: str | None = None
    model_id: str | None = None
    created_at: str | None = None


class Capability(BaseModel):
    schema_version: str = "capability/v1"
    id: str
    version: str = "1.0.0"
    title: str
    description: str
    status: Literal["draft", "approved"] = "draft"
    app: AppIdentity = Field(default_factory=AppIdentity)
    tenant: TenantOverlay | None = None
    inputs: list[FieldSpec]
    outputs: list[FieldSpec]
    outcomes: list[OutcomeSpec]
    entry: str
    auth: Literal["assumed_present"] = "assumed_present"
    allowlist_ref: str = "policy/allowlist.yaml"
    irreversible_step_ids: list[str] = Field(default_factory=list)
    steps: list[Step]
    success: Checkpoint
    provenance: Provenance = Field(default_factory=Provenance)


class ProposedAction(BaseModel):
    """Structured action from the discovery LLM (not stored as the artifact)."""

    type: Literal[
        "goto",
        "click",
        "type",
        "dismiss",
        "extract",
        "done",
        "stuck",
    ]
    intent: str = ""
    role: str | None = None
    name: str | None = None
    text: str | None = None
    value: str | None = None
    url: str | None = None
    extract_to: str | None = None
    risk: RiskClass = RiskClass.reversible
    done_reason: str | None = None
    stuck_reason: str | None = None


class PolicyDecision(BaseModel):
    allowed: bool
    require_hitl: bool = False
    reason: str = ""


class RunResult(BaseModel):
    kind: ResultKind
    code: str
    capability_id: str | None = None
    capability_version: str | None = None
    run_id: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    failed_step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence_ref: str | None = None
    control: ControlLock = ControlLock.agent
    message: str = ""
