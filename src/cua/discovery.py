"""LLM observe → decide loop. Never writes the capability; the compiler does."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from cua.adapter import ObserveResult, SurfaceAdapter
from cua.models import ProposedAction


SYSTEM = """You operate a legacy bank back-office UI (no APIs, no test IDs).
You receive a compact observation (url, heading, controls, visible text, dialog).
Return ONE JSON object with keys:
  type: goto | click | type | dismiss | extract | done | stuck
  intent: short description
  role: optional a11y role (textbox, button)
  name: accessible or visible name
  value: for type, the exact string to enter
  url: for goto only
  extract_to: for extract (savingsBalance or memberName)
  risk: reversible | irreversible
  done_reason / stuck_reason when applicable

Rules:
- Stay on the assigned app. Never invent URLs outside it.
- Prefer role+name. Do not use CSS.
- If a system notification overlay is visible, dismiss it first (type=dismiss).
- For lookup: type the member id into Member ID, click Search.
- If you see Account summary and a Savings row, extract savingsBalance then memberName, then type=done.
- If you see "No member found", you may type=done with done_reason member_not_found.
- If you cannot proceed safely, type=stuck with a reason.
- Never click Transfer, Delete, Wire, or Submit payment.
- Never include passwords or full SSN.
"""


class DiscoveryAgent:
    def __init__(self, adapter: SurfaceAdapter, goal: str, entry: str) -> None:
        self.adapter = adapter
        self.goal = goal
        self.entry = entry
        self.model_id = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def decide(self, obs: ObserveResult, step_index: int) -> ProposedAction:
        if step_index == 0 and "8765" not in obs.url and self.entry:
            return ProposedAction(type="goto", url=self.entry, intent="open entry")
        payload = self.adapter.observation_for_llm(obs)
        user = json.dumps({"goal": self.goal, "step": step_index, "observation": payload})
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
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]


def _parse_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.replace("json", "", 1).strip()
    return json.loads(raw)
