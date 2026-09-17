# computer-use-automation

Discover a back-office UI flow with an LLM once, compile a **versioned capability**, then **replay it with no model** — with policy, same-session human handoff, and declared business outcomes.

This is a take-home vertical slice: one local mock core (no APIs, no test IDs), not a bank integration.

## Architecture (control flow)

Every mutation is `proposed/recorded action → Policy → Session lock → Surface adapter`. The orchestrator only chooses **discover** vs **invoke**.

## Setup

Python 3.11+. From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
python -m playwright install chromium
```

If the Chromium download fails (corporate TLS), replay still works with installed Chrome (`CUA_BROWSER_CHANNEL=chrome`, the default).

```powershell
copy .env.example .env
```

Put an `OPENAI_API_KEY` in `.env` for discovery. Replay does not need a key.

## Demo path

Terminal 1 — mock core:

```powershell
python -m cua mock-server
```

Terminal 2 — deterministic replay (no LLM):

```powershell
python -m cua invoke --start-mock --input memberId=12345
python -m cua invoke --start-mock --input memberId=99999
```

Happy path returns `success` with `savingsBalance` / `memberName`. Unknown member returns `business_outcome` / `member_not_found` (not a crash).

LLM discovery (writes `capabilities/` + `evidence/`):

```powershell
python -m cua discover --goal "Look up member 12345 and read the current savings balance" --target http://127.0.0.1:8765/
```

Headed HITL (automation pauses on irreversible actions; type `resume` or `abort`):

```powershell
$env:CUA_HEADLESS="0"
python -m cua invoke --input memberId=12345
```

`CUA_HITL_AUTO_RESUME=1` skips the interactive pause (used in tests).

## Tests

```powershell
pytest -q
```

## Layout

- `src/cua/` — orchestrator, policy, session, adapter, discovery, compiler, replay, HITL, evidence
- `policy/allowlist.yaml` — origins and irreversible name patterns
- `capabilities/` — versioned capability JSON
- `evidence/` — per-run logs and failure screenshots
- `REPORT.md` — design write-up

Secrets stay in `.env`. Artifacts store placeholders, not live member identifiers.
