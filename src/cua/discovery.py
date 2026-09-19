"""LLM observe → decide loop. Never writes the capability; the compiler does."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from cua.adapter import ObserveResult, SurfaceAdapter
from cua.models import ProposedAction
from cua.redact import redact_for_log


SYSTEM = """You operate a computer UI the way a person would (legacy web is common: no APIs, no test IDs).
You receive a goal, a compact live observation (url, heading, controls, redacted visible text, dialog), and the immediately previous attempted action with its outcome when one exists.
Return ONE JSON object:
  type: goto | click | type | dismiss | extract | done | stuck
  intent: short description of this action
  role: optional accessibility role (textbox, button)
  name: accessible or visible name of the control
  value: for type, the string to enter
  url: for goto only, and only on the assigned app
  extract_to: for extract, the output field name from the goal
  risk: reversible | irreversible
  done_reason / stuck_reason when applicable

Rules:
- Choose the next action from the goal, this observation, and the previous-action outcome if present. Do not assume a canned workflow.
- Prefer accessibility role + name for click and type. For extract, put the visible field label in name or text (never the full intent sentence) and set extract_to to the output field from the goal, then type=done after values are read. Do not use a canned script of clicks followed by extract.
- If last_outcome is extracted, the previous extract succeeded. You get extract_to, not the raw value; use the live observation and still emit type=done when the goal is complete.
- Do not use CSS selectors.
- Stay on the assigned app. Never invent off-app URLs.
- If a blocking dialog or overlay is visible, dismiss it before doing other work.
- If the observation already satisfies the goal, extract any requested values using field labels from the observation, then type=done.
- If you cannot proceed safely, type=stuck with a reason.
- Never click Transfer, Delete, Wire, Close account, or Submit payment.
- Never type passwords. Never include SSN, tokens, or other secrets in the JSON.
"""


class DiscoveryAgent:
    def __init__(self, adapter: SurfaceAdapter, goal: str, entry: str) -> None:
        self.adapter = adapter
        self.goal = goal
        self.entry = entry
        self.model_id = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def decide(
        self,
        obs: ObserveResult,
        step_index: int,
        *,
        last_action: ProposedAction | None = None,
        last_outcome: str | None = None,
    ) -> ProposedAction:
        if step_index == 0 and self.entry and not obs.url.startswith(self.entry.rstrip("/")):
            return ProposedAction(type="goto", url=self.entry, intent="open assigned entry")
        payload = self.adapter.observation_for_llm(obs)
        user = json.dumps(
            {
                "goal": self.goal,
                "step": step_index,
                "observation": payload,
                "last_action": _safe_last_action(last_action),
                "last_outcome": last_outcome,
            }
        )
        raw = self._complete(user)
        data = _parse_json(raw)
        return ProposedAction.model_validate(data)

    def _complete(self, user: str) -> str:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Discovery requires a live model. "
                "Replay can run without it."
            )
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        body: dict[str, Any] = {
            "model": self.model_id,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            if resp.is_error:
                raise RuntimeError(
                    f"OpenAI chat/completions {resp.status_code} for model {self.model_id!r}: {resp.text}"
                )
            return resp.json()["choices"][0]["message"]["content"]


def _safe_last_action(action: ProposedAction | None) -> dict[str, Any] | None:
    if action is None:
        return None
    dumped = action.model_dump(exclude_none=True)
    dumped.pop("extracted", None)
    return redact_for_log("action", dumped)


def _parse_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.replace("json", "", 1).strip()
    return json.loads(raw)
