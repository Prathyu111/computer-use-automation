"""Compile a successful discovery trace into a versioned capability artifact."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cua.models import (
    Capability,
    Checkpoint,
    FieldSpec,
    Handler,
    HandlerThen,
    LocatorStrategy,
    OutcomeSpec,
    Provenance,
    ResultKind,
    Sensitivity,
    Step,
    Target,
    WhenCondition,
)


def compile_lookup_capability(
    *,
    run_id: str,
    model_id: str | None,
    entry: str,
    recorded_steps: list[dict[str, Any]],
    parameterized_member: bool,
) -> Capability:
    """Build the mock-core lookup capability from a successful run.

    Exploratory dead-ends are dropped; member-id literals become $input.memberId.
    """
    steps: list[Step] = []
    steps.append(
        Step(
            id="s1_goto_entry",
            action="goto",
            url=entry,
            checkpoint=Checkpoint(description="Lookup form", heading_contains="Member lookup"),
        )
    )
    steps.append(
        Step(
            id="s2_dismiss_interstitial",
            action="dismiss",
            target=Target(
                intent="System notification OK",
                strategies=[
                    LocatorStrategy(kind="a11y", role="button", name="OK"),
                    LocatorStrategy(kind="css", css=".overlay button"),
                ],
            ),
            handlers=[
                Handler(
                    when=WhenCondition(kind="target_missing"),
                    then=HandlerThen(action="recover", recover="retry", reason="no interstitial"),
                )
            ],
        )
    )
    steps.append(
        Step(
            id="s3_type_member_id",
            action="clear_and_type",
            target=Target(
                intent="Member ID field",
                strategies=[
                    LocatorStrategy(kind="labeled_control", label="Member ID"),
                    LocatorStrategy(kind="a11y", role="textbox", name="Member ID"),
                ],
            ),
            value_from="$input.memberId",
        )
    )
    steps.append(
        Step(
            id="s4_search",
            action="click",
            target=Target(
                intent="Search",
                strategies=[
                    LocatorStrategy(kind="a11y", role="button", name="Search"),
                    LocatorStrategy(kind="labeled_control", label="Search"),
                ],
            ),
            handlers=[
                Handler(
                    when=WhenCondition(kind="text_matches", text="No member found"),
                    then=HandlerThen(
                        action="return_outcome",
                        kind=ResultKind.business_outcome,
                        code="member_not_found",
                    ),
                ),
                Handler(
                    when=WhenCondition(kind="text_matches", text="You do not have permission"),
                    then=HandlerThen(
                        action="return_outcome",
                        kind=ResultKind.business_outcome,
                        code="permission_denied",
                    ),
                ),
                Handler(
                    when=WhenCondition(kind="text_matches", text="Member ID is required"),
                    then=HandlerThen(
                        action="return_outcome",
                        kind=ResultKind.business_outcome,
                        code="validation_rejected",
                    ),
                ),
                Handler(
                    when=WhenCondition(kind="text_matches", text="Session expired"),
                    then=HandlerThen(
                        action="return_outcome",
                        kind=ResultKind.hard_failure,
                        code="session_expired",
                    ),
                ),
            ],
            checkpoint=Checkpoint(description="Account summary or known outcome"),
        )
    )
    steps.append(
        Step(
            id="s5_extract_balance",
            action="extract",
            target=Target(
                intent="Current savings balance",
                strategies=[
                    LocatorStrategy(
                        kind="structural",
                        row_key="Savings",
                        target_cell=1,
                    ),
                    LocatorStrategy(kind="name_in_scope", text="Savings"),
                ],
            ),
            extract_to="savingsBalance",
        )
    )
    steps.append(
        Step(
            id="s6_extract_name",
            action="extract",
            target=Target(
                intent="Member name",
                strategies=[
                    LocatorStrategy(kind="structural", row_key="Name", target_cell=1),
                ],
            ),
            extract_to="memberName",
        )
    )

    _ = recorded_steps, parameterized_member  # used for provenance only in v1

    return Capability(
        id="local.mock_core.lookup_savings_balance",
        version="1.0.0",
        title="Look up member savings balance",
        description=(
            "Open member lookup, search by member ID, return current savings balance. "
            "Unknown members are a business outcome, not a crash."
        ),
        inputs=[
            FieldSpec(
                name="memberId",
                type="string",
                sensitivity=Sensitivity.identifier,
                description="Member identifier supplied per invocation",
                example_placeholder="<memberId>",
            )
        ],
        outputs=[
            FieldSpec(
                name="savingsBalance",
                type="money",
                sensitivity=Sensitivity.none,
                description="Current savings balance as displayed",
            ),
            FieldSpec(
                name="memberName",
                type="string",
                sensitivity=Sensitivity.pii,
                description="Member display name; returned to caller, not stored in logs as raw PII",
            ),
        ],
        outcomes=[
            OutcomeSpec(kind=ResultKind.success, code="success", description="Balance extracted"),
            OutcomeSpec(
                kind=ResultKind.business_outcome,
                code="member_not_found",
                description="No member matches the id",
            ),
            OutcomeSpec(
                kind=ResultKind.business_outcome,
                code="permission_denied",
                description="Operator is not allowed to view this member",
            ),
            OutcomeSpec(
                kind=ResultKind.business_outcome,
                code="validation_rejected",
                description="Lookup form validation failed",
            ),
            OutcomeSpec(kind=ResultKind.hard_failure, code="failed", description="Automation error"),
            OutcomeSpec(kind=ResultKind.escalated, code="escalated", description="Human took control"),
        ],
        entry=entry,
        steps=steps,
        success=Checkpoint(
            description="Account summary with savings row",
            heading_contains="Account summary",
            text_contains="Savings",
        ),
        provenance=Provenance(
            discovery_run_id=run_id,
            model_id=model_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
