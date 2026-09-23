"""Apply a tenant overlay onto a capability without mutating the base.

The overlay is presentation/surface specialization only. It must not change
capability identity, inputs/outputs, action types, step order, risk, policy,
handler then kind/code, provenance, or allowlist.

Apply-time pin mismatch or unknown step id is `capability_drift` (this module).
Replay-time locator/checkpoint misses against the applied overlay are an
observed failure of the pinned overlay contract, not a vendor-version claim:
`hard_failure` with code `surface_mismatch` (not a ResultKind).
"""

from __future__ import annotations

from cua.models import Capability, Handler, TenantOverlay


class SpecializationError(ValueError):
    """Typed apply failure. `code` is capability_drift for pin/unknown-step."""

    def __init__(self, message: str, code: str = "capability_drift") -> None:
        super().__init__(message)
        self.code = code


def apply_overlay(base: Capability, overlay: TenantOverlay) -> Capability:
    """Return a deep copy of `base` with overlay surface tokens applied.

    Never mutates `base`. Sets in-memory `tenant` to a copy of the overlay
    for later evidence; does not write disk.
    """
    _require_pins(base, overlay)
    step_ids = {step.id for step in base.steps}
    unknown = [sid for sid in overlay.step_overrides if sid not in step_ids]
    if unknown:
        raise SpecializationError(
            f"unknown step override ids (cannot add steps): {unknown}",
            code="capability_drift",
        )

    cap = base.model_copy(deep=True)

    if overlay.surface and overlay.surface.entry_url:
        cap.entry = overlay.surface.entry_url
        s1 = next((s for s in cap.steps if s.id == "s1_goto_entry" or s.id.startswith("s1")), None)
        if s1 is not None:
            s1.url = overlay.surface.entry_url

    for sid, ov in overlay.step_overrides.items():
        step = next(s for s in cap.steps if s.id == sid)
        if ov.url is not None:
            step.url = ov.url
        if ov.target is not None:
            step.target = ov.target.model_copy(deep=True)
        if ov.checkpoint is not None:
            step.checkpoint = ov.checkpoint.model_copy(deep=True)
        if ov.handler_when_text:
            _remap_handler_when_text(step.id, step.handlers, ov.handler_when_text)

    if overlay.success is not None:
        cap.success = overlay.success.model_copy(deep=True)

    cap.tenant = overlay.model_copy(deep=True)
    return cap


def _require_pins(base: Capability, overlay: TenantOverlay) -> None:
    missing = [
        name
        for name, value in (
            ("base_capability_id", overlay.base_capability_id),
            ("base_version", overlay.base_version),
            ("app_compat", overlay.app_compat),
        )
        if not value
    ]
    if missing:
        raise SpecializationError(
            f"overlay pin fields required at apply: {missing}",
            code="capability_drift",
        )
    if overlay.base_capability_id != base.id:
        raise SpecializationError(
            "overlay base_capability_id does not match capability id",
            code="capability_drift",
        )
    if overlay.base_version != base.version:
        raise SpecializationError(
            "overlay base_version does not match capability version",
            code="capability_drift",
        )
    if overlay.app_compat != base.app.compat:
        raise SpecializationError(
            "overlay app_compat does not match capability app.compat",
            code="capability_drift",
        )


def _remap_handler_when_text(step_id: str, handlers: list[Handler], remaps: dict[str, str]) -> None:
    for code, text in remaps.items():
        matched = [h for h in handlers if h.then.code == code]
        if not matched:
            raise SpecializationError(
                f"handler_when_text code {code!r} has no existing then.code on {step_id}",
                code="capability_drift",
            )
        for handler in matched:
            handler.when.text = text
