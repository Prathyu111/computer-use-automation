"""CLI: mock-server, discover, invoke."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cua.orchestrator import Orchestrator


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cua", description="Computer-use capability system")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mock = sub.add_parser("mock-server", help="Run the local mock core-banking UI")
    p_mock.add_argument("--port", type=int, default=8765)

    p_disc = sub.add_parser("discover", help="LLM-driven discovery → capability JSON")
    p_disc.add_argument("--goal", required=True)
    p_disc.add_argument("--target", default="http://127.0.0.1:8765/")
    p_disc.add_argument("--max-steps", type=int, default=12)

    p_disc.add_argument("--start-mock", action="store_true", help="Start mock core in-process")

    p_inv = sub.add_parser("invoke", help="Deterministic replay (no LLM)")
    p_inv.add_argument(
        "--capability",
        default="capabilities/local.mock_core.lookup_savings_balance.v1.0.0.json",
    )
    p_inv.add_argument(
        "--input",
        action="append",
        default=[],
        help="key=value (repeatable), e.g. --input memberId=12345",
    )
    p_inv.add_argument("--start-mock", action="store_true", help="Start mock core in-process")
    p_inv.add_argument(
        "--overlay",
        default=None,
        help="Optional TenantOverlay JSON sidecar (presentation/surface only)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "mock-server":
        from cua.mock_app import serve

        serve(args.port)
        return
    if getattr(args, "start_mock", False):
        from cua.mock_app import serve_in_thread

        serve_in_thread()
    orch = Orchestrator()
    if args.cmd == "discover":
        path = orch.discover(args.goal, args.target, max_steps=args.max_steps)
        print(f"Wrote capability {path}")
        return
    if args.cmd == "invoke":
        params = {}
        for item in args.input:
            if "=" not in item:
                raise SystemExit(f"invalid --input {item!r}, expected key=value")
            k, v = item.split("=", 1)
            params[k] = v
        overlay_path = Path(args.overlay) if args.overlay else None
        result = orch.invoke(Path(args.capability), params, overlay_path=overlay_path)
        print(json.dumps(result.model_dump(), indent=2))
        if result.kind.value in {"hard_failure"}:
            sys.exit(1)


if __name__ == "__main__":
    main()
