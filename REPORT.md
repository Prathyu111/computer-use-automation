# REPORT.md

## 1. Architecture

One process, two modes. The orchestrator chooses **discover** (LLM) or **invoke** (replay). It never talks to the application directly.

Every mutation is: **proposed or recorded action → policy gate → session control lock → surface adapter**. Observations flow back from the adapter to discovery or replay. A compiler runs only after a successful discovery and writes a versioned capability JSON. Replay reads that file and does not call the model.

HITL has three entries: discovery stuck, replay blocked, policy approval required. The operator takes and returns **the same live browser session**. Evidence is a dotted sink (JSONL logs; screenshot on non-success), not on the act path.

Policy **deny** (off-allowlist) is a hard failure without calling the adapter. Recoverable UI (dismiss interstitial) is handled inside replay and is not a caller status.

Caller-facing `RunResult`: `success` | `business_outcome` | `hard_failure` | `escalated`.

## 2. Artifact schema

`capability/v1` is an RPC contract plus a linear procedure:

- Identity: `id`, `version`, `app` (vendor/product/surfaceKind), optional `tenant` overlay (schema-ready, not a platform).
- Contract: typed `inputs` / `outputs` with `sensitivity`; closed `outcomes` including `member_not_found`.
- Steps: semantic actions; each control is a **ranked locator bundle** (a11y → labeled control → structural → css last).
- Handlers: deterministic `when` / `then` (return outcome, recover, escalate, fail).
- Terminal `success` checkpoint; finishing the step list is not enough.
- Provenance without transcripts, secrets, or raw PII.

The discovery chat is evidence only. The compiler parameterizes member ids as `$input.memberId` and drops exploratory dead-ends. For this mock product the compiled shape is the lookup capability (goto, dismiss, type, search, extract) after a live run.

## 3. Determinism and error handling

Replay resolves locators in order until exactly one visible match, then waits for `domcontentloaded` after clicks. Checkpoints assert headings/text.

Runtime errors are classified by **declared handlers**, not by the model:

- Banner “No member found…” → `business_outcome` / `member_not_found`
- Permission copy → `permission_denied`
- Empty id → `validation_rejected`
- Known overlay → dismiss (best-effort) and continue
- Policy deny or unresolved locator → `hard_failure` with `stepId`, expected, observed, screenshot
- Unknown / irreversible without operator → escalate

UI drift is secondary: ranked locators plus a failed unique match stop the run instead of clicking the first of many.

## 4. Heterogeneity and multi-tenant

The capability speaks **semantic actions + locator intents**. Playwright lives only in the web adapter. A desktop adapter could implement the same `observe` / `act` / `resolve` surface using the OS accessibility tree without changing step language.

Multi-tenant: the base artifact is the vendor product. A tenant overlay (unused in code) may replace locators, interstitial copy, and resolved host **by step id**. It must not change inputs, outputs, or outcome codes — that is a new `version`. Drift is a checkpoint failure after overlay locators; fix the overlay or the broken step, do not re-record per institution.

## 5. Escalation and handoff

Stuck (repeated locator miss, unknown dialog, max steps) or irreversible policy raises an intervention ticket: reason, step, url, screenshot. The session lock becomes `human`; the adapter will not act. A headed window is the live session. The mock operator path is CLI `resume` / `abort`. `resume` returns the lock to `agent`; `abort` leaves `paused` and the run is `escalated`. Tests set `CUA_HITL_AUTO_RESUME=1`.

## 6. Safety

`policy/allowlist.yaml` lists origins and action types. Names matching Transfer / Delete / Wire / Close account / Submit payment require HITL even if the LLM marks them reversible. Off-origin `goto` is denied. Logs redact identifier-like values and secret field names. Capabilities store placeholders, not live member ids. Auth is assumed present and not stored. Limits: overlay screenshots may still show on-screen PII; we do not OCR-mask pixels in v1.

## 7. Cuts

Not built: queues, desktop adapter, tenant overlay runtime, co-browse console, approval catalog, LLM fallback on replay, code generation. Next: overlay apply-by-step-id, bounded single-step recovery, capability catalog endpoint for a calling agent.
