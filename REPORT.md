# REPORT.md

## 1. Architecture

One process, two modes. The orchestrator chooses **discover** (LLM) or **invoke** (replay). It never talks to the application directly.

Every mutation is: **proposed or recorded action → policy gate → session control lock → surface adapter**. Observations flow back from the adapter to discovery or replay. A compiler runs only after a successful discovery and writes a versioned capability JSON. Replay reads that file and does not call the model.

This slice demonstrates that architecture for one **lookup-savings-balance** family on a local mock core. The compiler is specialized to that family (parameterized member id, ranked locators from the working role+name, declared Search handlers). It is not a general “compile any app” product.

HITL entry points: discovery `stuck` and `act_failed`; replay **handler escalate**; policy **irreversible** (`require_hitl`) in either mode if classified. Unresolved locator on replay is `hard_failure`, not HITL. Exhausting discovery `--max-steps` raises `CompileError`, not HITL. The operator takes and returns **the same live browser session**. Evidence is a dotted sink (JSONL; screenshot on non-success plus HITL before/after locally), not on the act path.

Policy **deny** (off-allowlist) is a hard failure without calling the adapter. Recoverable UI (dismiss interstitial) is handled inside replay and is not a caller status (`replay-2503214d`).

Caller-facing `RunResult`: `success` | `business_outcome` | `hard_failure` | `escalated`. Required success output is `savingsBalance` only.

## 2. Artifact schema

`capability/v1` is an RPC contract plus a linear procedure:

- Identity: `id`, `version`, `app` (vendor/product/surfaceKind), optional `tenant` overlay (schema-ready, not a platform).
- Contract: typed `inputs` / `outputs` with `sensitivity`; closed `outcomes` including `member_not_found`.
- Steps: semantic actions; each control is a **ranked locator bundle** (a11y → labeled control → structural → css last).
- Handlers: deterministic `when` / `then` (return outcome, recover, escalate, fail).
- Terminal `success` checkpoint; finishing the step list is not enough.
- Provenance without transcripts, secrets, or raw PII.

The discovery chat is evidence only. The compiler reads the **recorded actions**, drops repeated dead-end clicks, parameterizes digit member ids as `$input.memberId`, attaches ranked locators from the role+name that actually worked, and adds declared banner handlers on Search. It refuses to write a capability if the trace never typed a member id or never reached extractable outputs.

Search runtime handlers (member/permission/validation banners, Session expired hard failure, the one-shot “Please acknowledge this notice” recover/dismiss, and the synthetic HITL dialog escalate) are **compiler-owned reviewed specializations**, not LLM-observed. Genuine `evidence/public/discover-f83a7518` never saw those UIs. Provenance on the **canonical** file keeps that discovery run and `model_id=gpt-4o-mini`; `provenance.notes` records `error_handlers: compiler_specialization`. Separate from that: compiler *normalization* of the recorded trace (duplicate extract collapse, `extract_to` aliasing, description redaction, outputs from extracts).

**Public snapshot vs canonical capability** (do not conflate):

- `evidence/public/discover-f83a7518/capability.json` is a **sanitized snapshot** of the capability written from successful discovery run `discover-f83a7518` (description redacted). Do **not** overwrite it with the current canonical JSON. Its provenance is the discovery compile (`model_id=gpt-4o-mini`); it is not re-stamped with later specialization notes.
- `capabilities/local.mock_core.lookup_savings_balance.v1.0.0.json` is the **reviewed/normalized** artifact after compiler normalization and deterministic error-handler specialization. Replay demos use this file. Required output is `savingsBalance` only (the public snapshot still lists a discovery-era `memberName` field).

## 3. Determinism and error handling

Replay tries **ranked locator strategies** in order. A strategy is accepted only when the Playwright locator `count == 1`, then `wait_for(state="visible")` (and editable/clickable when required). That is not a complete accessibility-tree walk, and it is not “exactly one visible among many.” A unique-match miss is `hard_failure` (screenshot on non-success). Checkpoints assert headings/text. Clicks wait for `domcontentloaded`.

Runtime errors are classified by **declared handlers**, not by the model:

- Banner “No member found…” → `business_outcome` / `member_not_found` — `evidence/public/replay-c4d0861b`
- Permission copy → `permission_denied`
- Empty id → `validation_rejected`
- Search one-shot notice (`memberId=12345`) → recover/dismiss via `_recover`, then continue — `evidence/public/replay-2503214d`
- Search HITL dialog (`memberId=11111`, **Supervisor approval required**) → `escalate` → same-session HITL → human dismisses → `resume` retries Search → success — `evidence/public/replay-df9e8123`. Distinct from the auto-recover notice; not classified as recover.
- Banner “Session expired” (`memberId=00000`) → `hard_failure` / `session_expired` — `evidence/public/replay-95e754bb` (not a business outcome; no extract)
- Known GET `/` overlay → dismiss step `s2_dismiss` (not the Search recover path)
- Policy deny or unresolved locator → `hard_failure` with `stepId`, expected, observed, screenshot
- Unknown / irreversible without operator → escalate (policy HITL)

Clean success without that Search recover path: `evidence/public/replay-1c9f10a2`. Genuine discovery: `evidence/public/discover-f83a7518`.

## 4. Heterogeneity and multi-tenant

The capability speaks **semantic actions + locator intents**. Playwright lives only in the web adapter. A desktop adapter could implement the same `observe` / `act` / `resolve` surface using the OS accessibility tree without changing step language.

Multi-tenant: the base artifact is the vendor product. A tenant overlay (unused in code) may replace locators, interstitial copy, and resolved host **by step id**. It must not change inputs, outputs, or outcome codes — that is a new `version`. Drift is a checkpoint failure after overlay locators; fix the overlay or the broken step, do not re-record per institution.

## 5. Escalation and handoff

Replay HITL in this slice is the Search handler `then.action: escalate` (and policy irreversible if a step is classified that way). Discovery may HITL on LLM `stuck` or `act_failed`. The session lock becomes `human`; the adapter will not act. A headed window is the **same live session**. CLI: `resume` / `done` / `abort`. `resume` returns the lock to `agent` and replay retries the blocked step; `done` skips re-execution (`extract` still requires a value); `abort` leaves `paused` and the run is `escalated`.

**Synthetic HITL demo (not a Transfer / goal change):** `memberId=11111`. First Search shows a blocking alertdialog. After the human clicks OK, `resume` retries Search; 11111 is in `MEMBERS`, so lookup succeeds. `memberId=12345` still only gets the auto-recover notice.

**Published HITL proof** is the structured sequence in `evidence/public/replay-df9e8123`: handler `escalate` → `hitl_request` (`lock=human`) → `hitl_resume` → `hitl_after` (`lock=agent`) → retry Search → extract → `success`. Also `intervention_request.json`. **Limitation:** individual human browser clicks are not recorded (no event spy). Raw `hitl_before.png` / `hitl_after.png` are retained locally and **omitted from the curated package** because screenshots are not automatically pixel-redacted. Do not cite gitignored local evidence as the public proof.

Do **not** set `CUA_HITL_AUTO_RESUME` for a headed product demo. Tests may auto-resume **after** a test-only helper clicks OK on the same Playwright page.

## 6. Safety

`policy/allowlist.yaml` lists origins and action types. Names matching Transfer / Delete / Wire / Close account / Submit payment require HITL even if the LLM marks them reversible. Off-origin `goto` is denied. Logs redact identifier-like values and secret field names. Capabilities store placeholders, not live member ids. Auth is assumed present and not stored.

Screenshots: replay captures a PNG on **non-success** (`business_outcome`, `hard_failure`, `escalated`); HITL also captures before/after locally. Pixels are not OCR-masked. `evidence/public/` therefore includes only `replay-95e754bb/hard_failure.png` (session expired; Member ID empty) and omits identifier-visible frames. Mock member data is synthetic.

## 7. Cuts

Not built: queues, desktop adapter, tenant overlay runtime, co-browse console, approval catalog, LLM fallback on replay, code generation, automatic pixel redaction.

Bounded single-step recovery **is built** and shown by `evidence/public/replay-2503214d` (Search notice dismiss, then success). Next: overlay apply-by-step-id, capability catalog endpoint for a calling agent.
