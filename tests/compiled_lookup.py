"""In-test lookup capability compiled from recorded steps.

Replay tests must not depend on the on-disk genuine discovery artifact,
which can lag compiler output mapping.
"""

from __future__ import annotations

from pathlib import Path

from cua.compiler import compile_capability
from cua.models import Capability

_RECORDED_STEPS = [
    {
        "type": "dismiss",
        "name": "OK",
        "role": "button",
        "intent": "Dismiss the system notification dialog",
    },
    {
        "type": "type",
        "name": "Member ID",
        "role": "textbox",
        "intent": "Enter the member ID to look up",
        "value": "12345",
    },
    {
        "type": "click",
        "name": "Search",
        "role": "button",
        "intent": "Click the Search button to look up the member",
    },
    {
        "type": "extract",
        "extract_to": "savingsBalance",
        "name": "Savings",
        "intent": "Extract the current savings balance",
    },
    {
        "type": "extract",
        "extract_to": "memberName",
        "name": "Name",
        "intent": "Extract the member name",
    },
]


def compiled_lookup_capability() -> Capability:
    return compile_capability(
        run_id="test-fixture",
        model_id="test",
        entry="http://127.0.0.1:8765/",
        goal="Look up a member and return the current savings balance",
        recorded_steps=_RECORDED_STEPS,
        last_heading="Account summary",
        last_text="Savings",
    )


def write_compiled_lookup(path: Path, cap: Capability | None = None) -> Path:
    artifact = cap if cap is not None else compiled_lookup_capability()
    path.write_text(artifact.model_dump_json(), encoding="utf-8")
    return path
