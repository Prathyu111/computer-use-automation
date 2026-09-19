"""Compile a successful discovery trace into a versioned capability artifact.

The LLM transcript is not the product. This module turns recorded actions into
parameterized steps, ranked locators, and declared outcome handlers.
"""

from __future__ import annotations

import re
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
from cua.redact import redact_text

_ID_VALUE = re.compile(r"^\d{3,12}$")
_CAMEL_OR_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")

_FAMILY_OUTPUTS: list[FieldSpec] = [
    FieldSpec(
        name="savingsBalance",
        type="money",
        description="Current savings balance as displayed",
    ),
    FieldSpec(
        name="memberName",
        type="string",
        sensitivity=Sensitivity.pii,
        description="Member display name; returned to caller, not stored in logs as raw PII",
    ),
]

# Compiler-owned runtime specializations (not LLM-observed). Family banner
# handlers plus Search recover/HITL escalate; attached whenever a Search/submit
# click is compiled.
_SEARCH_HANDLERS = [
    Handler(
        when=WhenCondition(kind="dialog", text="Supervisor approval required"),
        then=HandlerThen(
            action="escalate",
            reason="Supervisor approval required",
        ),
    ),
    Handler(
        when=WhenCondition(kind="dialog", text="Please acknowledge this notice"),
        then=HandlerThen(action="recover", recover="dismiss"),
    ),
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
]


class CompileError(ValueError):
    pass


def compile_capability(
    *,
    run_id: str,
    model_id: str | None,
    entry: str,
    goal: str,
    recorded_steps: list[dict[str, Any]],
    last_heading: str = "",
    last_text: str = "",
) -> Capability:
    acting = [r for r in recorded_steps if r.get("type") not in {None, "done", "stuck"}]
    acting = _drop_dead_ends(acting)
    if not acting:
        raise CompileError("discovery recorded no reusable actions")

    steps: list[Step] = []
    saw_member_input = False
    typed_replacements: list[tuple[str, str]] = []

    if not any(r.get("type") == "goto" for r in acting):
        steps.append(
            Step(
                id="s1_goto_entry",
                action="goto",
                url=entry,
                checkpoint=Checkpoint(description="Open entry", url_contains=_host(entry)),
            )
        )

    for rec in acting:
        n = len(steps) + 1
        kind = rec.get("type")
        if kind == "goto":
            steps.append(
                Step(
                    id=f"s{n}_goto",
                    action="goto",
                    url=rec.get("url") or entry,
                    checkpoint=Checkpoint(description="Navigate", url_contains=_host(rec.get("url") or entry)),
                )
            )
            continue
        if kind == "dismiss":
            steps.append(
                Step(
                    id=f"s{n}_dismiss",
                    action="dismiss",
                    target=_target_from_record(rec, default_intent="Dismiss overlay", default_role="button"),
                )
            )
            continue
        if kind == "type":
            raw = str(rec.get("value") or "")
            value_from = "$input.memberId" if _ID_VALUE.match(raw) or "member" in (rec.get("intent") or "").lower() else raw
            if value_from == "$input.memberId":
                saw_member_input = True
                if raw:
                    typed_replacements.append((raw, "<memberId>"))
            elif raw:
                typed_replacements.append((raw, ""))
            steps.append(
                Step(
                    id=f"s{n}_type",
                    action="clear_and_type",
                    target=_target_from_record(rec, default_intent="Member ID field", default_role="textbox"),
                    value_from=value_from,
                )
            )
            continue
        if kind == "click":
            search = _is_search(rec)
            steps.append(
                Step(
                    id=f"s{n}_click",
                    action="click",
                    target=_target_from_record(rec, default_intent="Search", default_role="button"),
                    handlers=list(_SEARCH_HANDLERS) if search else [],
                    checkpoint=Checkpoint(description="After click") if search else None,
                )
            )
            continue
        if kind == "extract":
            dest = _canonical_output_name(str(rec.get("extract_to") or "savingsBalance"))
            if _is_duplicate_extract(steps[-1] if steps else None, dest, rec):
                continue
            steps.append(
                Step(
                    id=f"s{n}_extract",
                    action="extract",
                    target=_extract_target(dest, rec),
                    extract_to=dest,
                )
            )

    if "account summary" in last_heading.lower() and not any(s.extract_to for s in steps):
        n = len(steps)
        steps.append(
            Step(
                id=f"s{n+1}_extract_balance",
                action="extract",
                target=_extract_target("savingsBalance", {}),
                extract_to="savingsBalance",
            )
        )

    if not any(s.extract_to for s in steps):
        raise CompileError("discovery did not reach a state we can extract outputs from")

    if not saw_member_input:
        raise CompileError("discovery did not type a parameterizable member id")

    success = Checkpoint(
        description="Account summary with savings row",
        heading_contains="Account summary" if "account summary" in (last_heading or "").lower() else last_heading[:80] or "Account summary",
        text_contains="Savings" if "savings" in (last_text or "").lower() else None,
    )

    return Capability(
        id="local.mock_core.lookup_savings_balance",
        version="1.0.0",
        title="Look up member savings balance",
        description=_reusable_description(goal, typed_replacements),
        inputs=[
            FieldSpec(
                name="memberId",
                type="string",
                sensitivity=Sensitivity.identifier,
                description="Member identifier supplied per invocation",
                example_placeholder="<memberId>",
            )
        ],
        outputs=_outputs_from_steps(steps),
        outcomes=[
            OutcomeSpec(kind=ResultKind.success, code="success", description="Balance extracted"),
            OutcomeSpec(kind=ResultKind.business_outcome, code="member_not_found", description="No member matches the id"),
            OutcomeSpec(kind=ResultKind.business_outcome, code="permission_denied", description="Operator is not allowed to view this member"),
            OutcomeSpec(kind=ResultKind.business_outcome, code="validation_rejected", description="Lookup form validation failed"),
            OutcomeSpec(kind=ResultKind.hard_failure, code="session_expired", description="Session is no longer valid"),
            OutcomeSpec(kind=ResultKind.hard_failure, code="failed", description="Automation error"),
            OutcomeSpec(kind=ResultKind.escalated, code="escalated", description="Human took control"),
        ],
        entry=entry,
        steps=steps,
        success=success,
        provenance=Provenance(
            discovery_run_id=run_id,
            model_id=model_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            notes="error_handlers: compiler_specialization",
        ),
    )


def _drop_dead_ends(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop explicit exploratory clicks and consecutive duplicate clicks. Keep setup navigation."""
    explore = re.compile(r"explor|wrong menu|dead.?end", re.I)
    out: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("type") == "click":
            blob = " ".join(str(rec.get(k) or "") for k in ("intent", "name", "text"))
            if explore.search(blob):
                continue
            if (
                out
                and out[-1].get("type") == "click"
                and out[-1].get("name") == rec.get("name")
            ):
                continue
        out.append(rec)
    return out


def _is_search(rec: dict[str, Any]) -> bool:
    blob = " ".join(
        str(rec.get(k) or "") for k in ("intent", "name", "text")
    ).lower()
    return "search" in blob or "submit" in blob or "lookup" in blob


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else url


def _target_from_record(rec: dict[str, Any], *, default_intent: str, default_role: str) -> Target:
    name = rec.get("name") or rec.get("text") or rec.get("intent") or default_intent
    role = rec.get("role") or default_role
    intent = rec.get("intent") or name
    strategies = [
        LocatorStrategy(kind="a11y", role=role, name=name),
        LocatorStrategy(kind="labeled_control", label=name),
    ]
    return Target(intent=intent, strategies=strategies)


def _extract_target(dest: str, rec: dict[str, Any]) -> Target:
    label = rec.get("name") or rec.get("text")
    if label:
        label_s = str(label)
        return Target(
            intent=rec.get("intent") or label_s,
            strategies=[
                LocatorStrategy(kind="labeled_readonly", label=label_s),
                LocatorStrategy(kind="structural", row_key=label_s, target_cell=1),
            ],
        )
    if dest == "memberName":
        return Target(
            intent=rec.get("intent") or "Member name",
            strategies=[LocatorStrategy(kind="structural", row_key="Name", target_cell=1)],
        )
    return Target(
        intent=rec.get("intent") or "Current savings balance",
        strategies=[
            LocatorStrategy(kind="structural", row_key="Savings", target_cell=1),
            LocatorStrategy(kind="name_in_scope", text="Savings"),
        ],
    )


def _name_tokens(name: str) -> set[str]:
    return {p.lower() for p in _CAMEL_OR_WORD.findall(name or "") if p}


def _canonical_output_name(dest: str) -> str:
    dest_tokens = _name_tokens(dest)
    if not dest_tokens:
        return dest
    best = dest
    best_score = 0
    for spec in _FAMILY_OUTPUTS:
        spec_tokens = _name_tokens(spec.name)
        if not spec_tokens:
            continue
        if spec_tokens <= dest_tokens or dest_tokens <= spec_tokens:
            score = len(spec_tokens & dest_tokens)
            if score > best_score:
                best = spec.name
                best_score = score
    return best


def _locator_identity_from_record(rec: dict[str, Any]) -> set[str]:
    return {
        str(rec[k]).strip().lower()
        for k in ("name", "text", "label")
        if rec.get(k)
    }


def _locator_identity_from_target(target: Target | None) -> set[str]:
    if target is None:
        return set()
    keys: set[str] = set()
    for strat in target.strategies:
        for value in (strat.name, strat.label, strat.text, strat.row_key):
            if value:
                keys.add(str(value).strip().lower())
    return keys


def _is_duplicate_extract(previous: Step | None, dest: str, rec: dict[str, Any]) -> bool:
    if previous is None or previous.action != "extract":
        return False
    if previous.extract_to == dest:
        return True
    incoming = _locator_identity_from_record(rec)
    existing = _locator_identity_from_target(previous.target)
    return bool(incoming and existing and incoming & existing)


def _outputs_from_steps(steps: list[Step]) -> list[FieldSpec]:
    catalog = {spec.name: spec for spec in _FAMILY_OUTPUTS}
    seen: set[str] = set()
    outputs: list[FieldSpec] = []
    for step in steps:
        name = step.extract_to
        if not name or name in seen:
            continue
        seen.add(name)
        spec = catalog.get(name)
        outputs.append(spec.model_copy() if spec else FieldSpec(name=name, type="string", description=f"Discovered output {name}"))
    return outputs


def _reusable_description(goal: str, typed_replacements: list[tuple[str, str]]) -> str:
    text = goal.strip() or "Look up a member and read savings balance."
    for raw, placeholder in typed_replacements:
        if not raw:
            continue
        text = re.sub(re.escape(raw), placeholder, text)
    text = redact_text(text)
    return re.sub(r"\s+", " ", text).strip()
