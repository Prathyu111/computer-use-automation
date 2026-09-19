"""Structured run evidence with redaction. Screenshots only on non-success."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.redact import redact_for_log

_ROOT = Path(__file__).resolve().parents[2]


class EvidenceSink:
    def __init__(self, run_id: str, kind: str) -> None:
        self.run_id = run_id
        self.kind = kind
        self.dir = _ROOT / "evidence" / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / f"{kind}.jsonl"
        self.events: list[dict[str, Any]] = []

    def log(self, event: str, **payload: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **self._redact_payload(payload),
        }
        self.events.append(record)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def write_json(self, name: str, data: Any) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    def screenshot_path(self, label: str) -> Path:
        return self.dir / f"{label}.png"

    def _redact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in payload.items():
            out[key] = redact_for_log(key, value)
        return out
