# computer-use-automation

Vertical slice of a **discover once, compile, replay without a model** architecture: policy, same-session human handoff, and distinct caller outcomes. The compiler is specialized to this **lookup-savings-balance** capability family on a local mock core (no APIs; documented scenario/member IDs are synthetic mock identifiers, while real PII, credentials, secrets, and machine-local sensitive information are excluded or redacted) — not arbitrary-app compilation.

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

Put an `OPENAI_API_KEY` in `.env` for **discovery only**. Replay does not need a key.

## Reviewer evidence (curated)

Published proof lives under `evidence/public/` (see `evidence/README.md`). Nine runs:

| Run | What it shows |
| --- | --- |
| `discover-f83a7518` | Genuine LLM discovery (`gpt-4o-mini`); sanitized capability snapshot — not the canonical file |
| `replay-1c9f10a2` | Clean deterministic success (`savingsBalance` only) |
| `replay-c4d0861b` | `business_outcome` / `member_not_found` |
| `replay-2503214d` | Recoverable Search notice → dismiss → success |
| `replay-95e754bb` | `hard_failure` / `session_expired` |
| `replay-a46f4e20` | Current same-session HITL → success with operator-reported `human_action` (screenshots omitted) |
| `replay-df9e8123` | Historical same-session HITL → success (no `human_action`; screenshots omitted) |
| `replay-b3c17d08` | Headed-browser Tenant A success: canonical capability, no overlay |
| `replay-a37cd7d5` | Headed-browser Tenant B success: same canonical capability + reviewed overlay (not LLM-discovered) |

Replay has no LLM decision loop. Error/recovery handlers on Search are reviewed compiler specializations, not LLM-discovered. Tenant B locators/entry/frame are a reviewed `TenantOverlay`, not a second discovery. REPORT.md records the path: genuine discovery → exploratory trace → compiler normalization → reviewed specialization → canonical capability → deterministic replay.

## Demo path (one process)

Do **not** start `python -m cua mock-server` in a second terminal. `--start-mock` starts the mock in-process; running both fights over port 8765.

Happy path returns `success` with required output **`savingsBalance` only**. Unknown member is `business_outcome` / `member_not_found`, not a crash.

Watch the UI (optional except HITL):

```powershell
$env:CUA_HEADLESS="0"
```

Success / bounded recovery (`memberId=12345` — one-shot Search notice, then extract). Tenant A uses the canonical artifact directly:

```powershell
python -m cua invoke --start-mock --input memberId=12345
```

Business outcome (`memberId=99999`):

```powershell
python -m cua invoke --start-mock --input memberId=99999
```

Hard failure (`memberId=00000`, session expired):

```powershell
python -m cua invoke --start-mock --input memberId=00000
```

Manual HITL (`memberId=11111`; same live browser/page). Unset auto-resume, click **OK** on the supervisor dialog, then type `resume` (or `done` / `abort`). For interactive `resume`/`done`, report what you did in the live UI (number or label); clicks are not captured from the DOM.

```powershell
Remove-Item Env:CUA_HITL_AUTO_RESUME -ErrorAction SilentlyContinue
$env:CUA_HEADLESS="0"
python -m cua invoke --start-mock --input memberId=11111
```

`CUA_HITL_AUTO_RESUME=1` is a test-only skip for takeover/stuck HITL; it never satisfies policy-required approval (`mode=approve` for risky/irreversible actions). `CUA_HITL_AUTO_COMPLETE=1` is also test-only: it skips re-execution (simulated human completion) and does not authorize the agent to execute the policy-gated action. It is not verified real human activity. Neither env claims a human actor. An operator-reported UI action is logged as `human_action` while `lock=human`; CLI verbs remain `resume` / `done` / `abort`. Individual browser clicks are not recorded.

## Cross-tenant specialization (Tenant B)

Same logical capability: `local.mock_core.lookup_savings_balance` v1.0.0. Discovery provenance stays `discover-f83a7518`. Tenant A replays that file as-is. Tenant B replays **that same file** plus a reviewed sidecar overlay (`specializations/local.mock_core.lookup_savings_balance.tenant_b.json`). Tenant B was **not** LLM-discovered.

Tenant B is the same mock product with a different presentation: iframe (`legacyCore`), legacy table layout, **Customer Number**, **Find Member**, **Account Details**, **Share Savings**. `SurfaceAdapter` binds `frame_scope`; `ReplayEngine` stays tenant-blind. Neither replay calls an LLM.

The overlay is presentation-only and schema-closed (`extra="forbid"`). It may change entry URL (allowlisted origin), frame scope, locators/targets, presentation checkpoint text, and handler *match* text. It cannot change action types, step order, inputs/outputs, risk/policy, handler outcome (`then`) semantics, or provenance.

If a required iframe cannot be bound, the adapter does not fall back to the parent DOM. Unsupported specialized-surface assumptions fail closed as `hard_failure` / `surface_mismatch` (observed surface failed the **pinned overlay contract**; that is not a vendor-version claim). Negative-control and drift cases live in `tests/test_tenant_b_replay.py`, not as curated manual runs.

This is one reviewed overlay for this mock Tenant B surface — not arbitrary tenant adaptation, automatic rediscovery, desktop, coordinate/screenshot automation, or generic legacy-app support.

```powershell
python -m cua invoke --start-mock --input memberId=12345 --overlay specializations/local.mock_core.lookup_savings_balance.tenant_b.json
```

## Discovery (LLM)

Requires `OPENAI_API_KEY`. `--start-mock` is supported (same in-process mock; do not also run `mock-server`):

```powershell
python -m cua discover --start-mock --goal "Look up member 12345 and read the current savings balance" --target http://127.0.0.1:8765/
```

Writes `capabilities/` plus a local `evidence/discover-*` run. Reviewers should use `evidence/public/discover-f83a7518/`, not a fresh overwrite of the public snapshot.

## Tests

```powershell
pytest -q
```

## Layout

- `src/cua/` — orchestrator, policy, session, adapter, discovery, compiler, replay, HITL, evidence, overlay apply
- `policy/allowlist.yaml` — origins and irreversible name patterns
- `capabilities/local.mock_core.lookup_savings_balance.v1.0.0.json` — canonical reviewed capability
- `specializations/local.mock_core.lookup_savings_balance.tenant_b.json` — reviewed Tenant B overlay (presentation only)
- `evidence/public/` — curated reviewer package; `evidence/README.md` explains it
- `REPORT.md` — design write-up

Secrets stay in `.env`. Artifacts store placeholders, not live member identifiers. Mock balances and names are synthetic.
