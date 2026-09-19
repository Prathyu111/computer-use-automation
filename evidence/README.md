# Evidence

Raw discovery and replay runs are retained locally under `evidence/discover-*` and `evidence/replay-*`. Those folders are not published.

`evidence/public/` is a **curated, redacted subset** of those locally retained raw runs. Run IDs are preserved. It is not a complete dump of every local run. Reviewers should use this tree only.

All member identifiers, names, balances, and account information used by the local mock are **synthetic test data** and do not represent real customers.

## Seven published runs

| Directory | Demonstrates |
| --- | --- |
| `public/discover-f83a7518/` | Genuine LLM discovery (`gpt-4o-mini`): `discovery.jsonl` + sanitized `capability.json` |
| `public/replay-1c9f10a2/` | Clean deterministic success |
| `public/replay-c4d0861b/` | `member_not_found` business outcome |
| `public/replay-2503214d/` | Recoverable Search condition → success |
| `public/replay-95e754bb/` | `session_expired` hard failure |
| `public/replay-a46f4e20/` | Current same-session HITL → success with operator-reported `human_action` (structured logs; screenshots omitted) |
| `public/replay-df9e8123/` | Historical same-session HITL → success (no `human_action`; structured logs; screenshots omitted) |

Replay logs contain no LLM decision loop. `result.json` `evidence_ref` values still name the original local `evidence/replay-*` paths; the published copies live under `evidence/public/`.

## Screenshot policy

Screenshots are not automatically pixel-redacted. Public inclusion is conservative: omit frames where a member identifier or similar value is visible.

- Included: `public/replay-95e754bb/hard_failure.png` (session expired; Member ID field empty).
- Omitted: `business_outcome.png` from replay-c4d0861b (identifier visible). Replay still screenshots non-success locally.
- Omitted: `hitl_before.png` and `hitl_after.png` from replay-a46f4e20 and replay-df9e8123 (identifier visible). Those files remain in the raw local runs.
- Current HITL public proof is `replay-a46f4e20`: JSONL sequence (escalate → `hitl_request` lock=human → `human_action` actor=human → `hitl_resume` → `hitl_after` lock=agent → retry → extract → success) plus `intervention_request.json`. `replay-df9e8123` is the historical snapshot without `human_action`. Individual browser clicks during takeover are not recorded.

## Public discovery snapshot vs canonical capability

`public/discover-f83a7518/capability.json` is a sanitized snapshot **derived from** successful discovery run `discover-f83a7518` (description redacted; original local file unchanged). Do **not** overwrite this file with the current canonical JSON. Do not treat it as the reviewed execution artifact.

`capabilities/local.mock_core.lookup_savings_balance.v1.0.0.json` is the reviewed/normalized capability after compiler normalization and deterministic error-handler specialization. Replay in this slice uses that file. Required caller output is `savingsBalance` only.

## Public tree

```
evidence/public/discover-f83a7518/capability.json
evidence/public/discover-f83a7518/discovery.jsonl
evidence/public/replay-1c9f10a2/result.json
evidence/public/replay-1c9f10a2/replay.jsonl
evidence/public/replay-c4d0861b/result.json
evidence/public/replay-c4d0861b/replay.jsonl
evidence/public/replay-2503214d/result.json
evidence/public/replay-2503214d/replay.jsonl
evidence/public/replay-95e754bb/result.json
evidence/public/replay-95e754bb/replay.jsonl
evidence/public/replay-95e754bb/hard_failure.png
evidence/public/replay-a46f4e20/result.json
evidence/public/replay-a46f4e20/replay.jsonl
evidence/public/replay-a46f4e20/intervention_request.json
evidence/public/replay-df9e8123/result.json
evidence/public/replay-df9e8123/replay.jsonl
evidence/public/replay-df9e8123/intervention_request.json
```
