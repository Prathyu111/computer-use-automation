"""Policy gate: allowlist + irreversible detection. Called before every act."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

from cua.models import PolicyDecision, ProposedAction, RiskClass, Step

_ROOT = Path(__file__).resolve().parents[2]


class PolicyGate:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _ROOT / "policy" / "allowlist.yaml"
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.allowed_origins = set(raw.get("allowed_origins") or [])
        self.allowed_actions = set(raw.get("allowed_actions") or [])
        self.irreversible_patterns = [
            p.lower() for p in (raw.get("irreversible_name_patterns") or [])
        ]

    def origin_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return origin in self.allowed_origins

    def is_irreversible_name(self, name: str | None) -> bool:
        if not name:
            return False
        lowered = name.lower()
        return any(p in lowered for p in self.irreversible_patterns)

    def check_proposed(self, action: ProposedAction, current_url: str) -> PolicyDecision:
        if action.type in {"done", "stuck"}:
            return PolicyDecision(allowed=True, reason="non-acting")
        if action.type not in self.allowed_actions:
            return PolicyDecision(
                allowed=False, reason=f"action type '{action.type}' is not allowlisted"
            )
        if action.type == "goto" and action.url and not self.origin_allowed(action.url):
            return PolicyDecision(allowed=False, reason=f"origin not allowlisted: {action.url}")
        if not self.origin_allowed(current_url) and action.type != "goto":
            return PolicyDecision(
                allowed=False, reason=f"current origin not allowlisted: {current_url}"
            )
        name = action.name or action.text or action.intent
        irreversible = action.risk is RiskClass.irreversible or self.is_irreversible_name(name)
        if irreversible:
            return PolicyDecision(
                allowed=True,
                require_hitl=True,
                reason=f"irreversible action requires HITL: {name}",
            )
        return PolicyDecision(allowed=True, reason="allowlisted")

    def check_step(
        self,
        step: Step,
        current_url: str,
        irreversible_ids: set[str] | None = None,
    ) -> PolicyDecision:
        if step.action not in self.allowed_actions:
            return PolicyDecision(
                allowed=False, reason=f"action type '{step.action}' is not allowlisted"
            )
        if step.action == "goto" and step.url and not self.origin_allowed(step.url):
            return PolicyDecision(allowed=False, reason=f"origin not allowlisted: {step.url}")
        if not self.origin_allowed(current_url) and step.action != "goto":
            return PolicyDecision(
                allowed=False, reason=f"current origin not allowlisted: {current_url}"
            )
        name = None
        if step.target:
            name = step.target.intent
            if step.target.strategies:
                name = step.target.strategies[0].name or step.target.strategies[0].label or name
        irreversible = (
            step.risk is RiskClass.irreversible
            or bool(irreversible_ids and step.id in irreversible_ids)
            or self.is_irreversible_name(name)
        )
        if not irreversible:
            return PolicyDecision(allowed=True, reason="allowlisted")
        if step.on_irreversible == "block":
            return PolicyDecision(
                allowed=False, reason=f"irreversible step {step.id} is blocked"
            )
        return PolicyDecision(
            allowed=True,
            require_hitl=True,
            reason=f"irreversible step {step.id} requires HITL",
        )
