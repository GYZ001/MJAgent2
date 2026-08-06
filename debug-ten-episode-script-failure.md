# Debug Session: ten-episode-script-failure
- **Status**: [OPEN]
- **Issue**: Script generation reaches baseline QA and then fails; the first ten episodes must succeed in one concurrent batch without sample-specific exceptions.
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: .dbg/trae-debug-log-ten-episode-script-failure.ndjson

## Reproduction Steps
1. Open the script workspace for the current project.
2. Clear all generated episode scripts.
3. Trigger script generation for episodes 1 through 10 concurrently.
4. Stop on the first failed episode and inspect the failed run before retrying.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Expected Signal | Evidence |
|----|------------|------------|--------|-----------------|----------|
| A | Baseline output and QA input contracts diverge | High | Low | QA receives missing, malformed, or differently named fields | Pending |
| B | Concurrent runs share or overwrite version/identity context | Medium | Medium | Trace IDs show another episode's artifact, revision, or identity | Pending |
| C | Model output is truncated or fails structured parsing without useful error propagation | High | Low | Raw response length/finish reason or parser exception precedes failure | Pending |
| D | QA state persistence races or is overwritten transactionally | Medium | Medium | State transitions are out of order or persisted status differs from in-memory result | Pending |
| E | Concurrent model calls hit rate, timeout, or context limits | Medium | Low | Provider error/status indicates 429, timeout, or token/context overflow | Pending |

## Instrumentation Design
- `app/domain/screenplay_ops.py`: record discovery entry/result/error shape.
- `app/portraits.py`: record each discovery request size and response shape without content.
- `app/production/screenplay_repair.py`: record post-baseline unresolved identity count and durable baseline state.

## Log Evidence
- Historical error `ERR-20260806-21b6fb`: the provider returned a non-JSON content-policy refusal during character discovery; `extract_json` raised `ValueError`.
- Historical error `ERR-20260806-95dbe2`: post-baseline incremental character discovery wrapped that refusal as `StageError`, causing the screenplay run to fail before `mark_baseline_generated`.
- Pre-fix NDJSON line 1: post-baseline discovery received 7,912 source characters and a 32,718-character draft.
- Pre-fix NDJSON lines 2, 4, and 6: one identity audit sent three requests, each repeating 7,912 source characters and 14,000 draft characters plus 33,008-45,000 future-chapter characters.
- Pre-fix NDJSON lines 3, 5, and 7: the same path can also return valid JSON, proving the failure is non-deterministic provider safety behavior coupled to an oversized input surface.
- Pre-fix NDJSON line 8: the successful retry produced 8 candidates and 4 general spelling-variant identity resolutions.

## Hypothesis Status
| ID | Status | Evidence |
|----|--------|----------|
| A | Rejected | The screenplay candidate was structurally parseable; failure occurred in a later character discovery call. |
| B | Rejected for this failure | No cross-episode artifact or revision overwrite was observed. |
| C | Confirmed | The failing response was refusal prose, not JSON; parser error obscured the response class. |
| D | Confirmed as a durability defect | Revision remained at `baseline_generation_count=0` because external discovery ran before production baseline persistence. |
| E | Confirmed | Character discovery multiplied one audit into three 55k-67k character prompts by repeating source/draft across future batches. |

## Verification Conclusion
- The post-fix identity prompt is smaller and the Baseline artifact is now persisted
  before any post-Baseline identity call.
- The first ten-episode verification did not reach that boundary. Episodes 1 and 2
  failed inside the Baseline Agent Loop after two full-document calls; episodes 3-10
  were cancelled by the fail-fast reproduction procedure.
- That run loaded `baseline_only=false`. The source was changed to
  `baseline_only=true` while the backend process was still running, without changing
  contract version `3.0.0`; therefore runtime and source evidence must not be mixed.

## Deep Audit Evidence
- Contract contradiction: the prompt says dialogue chains have no fixed turn limit,
  while deterministic validation blocks chains outside 1-8 turns.
- Duration contradiction: episodes 1-10 all target 50 seconds while source size ranges
  from 2,645 to 35,378 characters; source length is explicitly ignored when deriving
  the target, while the prompt forbids dropping any effective source event.
- Contract v3 generation does not include `narrative_plan` in the output prompt.
  All four observed v3 candidates have `narrative_plan=null`, although new production
  is documented as requiring the typed narrative authority.
- Missing `narrative_plan` selects the legacy score-only path. In the restarted
  `baseline_only=true` verification, episode 2 published Artifact
  `art_108085370e92` as approved T2 with Evaluation `eval_fb05aba66945` reporting
  `blocker_count=3`, `must_fix_count=3`, score 70, and `verdict=quality_risk`.
  Certificate `cert_cfeb5f1a6f4e` nevertheless records blockers=0 and
  must_fix_issues=0.
- The three published blockers are a 49-character single-shot dialogue,
  an invalid dialogue chain length, and one undelivered must-keep spine beat.
- Episode 1 reproduced the same fail-open publication after the backend restart:
  Artifact `art_665550c32bad` was published as ready with score 20,
  `blocker_count=8`, and `must_fix_count=8`; certificate
  `cert_9124a9fb1b3f` still records blockers=0 and must_fix_issues=0.
- That approved 50-second episode contains 21 spine beats, 9 scenes, 46 dialogue
  turns, 883 spoken characters, and 2,038 action-text characters. At the system's
  own 36 spoken characters per 10 seconds limit, dialogue alone requires roughly
  245 seconds before any action or scene establishment time.
- Repair regression is hidden: episode 1 iteration 2 degraded from a T1 candidate
  to T0 due to an invalid `source_coverage[100].disposition`, but the final error
  reported only blockers from the previous parseable candidate.
- Issue identity collisions are confirmed. Two distinct dialogue capacity issues and
  two distinct chain-length issues share the same `(code, path, rule_id)` because
  numeric indexes are removed from legacy message fingerprints.
- Batch cancellation is not atomic. Sequential cancel-and-wait releases queue slots
  one at a time, causing episodes 5-10 to start provider requests immediately before
  they are cancelled.
- Queue state is process-local. Queued workflow runs remain `CREATED` while episode
  rows are projected as `running`; there is no durable batch or queued state.
- Character discovery scans only the first 18,000 source characters. Episode 10 has
  35,385 characters, so current-episode identity coverage is incomplete by design.
- Future identity resolution rewrites current screenplay display labels and body text
  to future canonical names, conflating stable internal identity with audience-visible
  reveal state.
- Provider metadata is not reliably queryable: 993 of 6,008 rows currently contain
  invalid JSON because serialized metadata is truncated by character count.

## Current Assessment
- The original content-policy refusal and durability defect are confirmed but are not
  the deepest blockers.
- The current highest-severity defect is fail-open publication: omitting
  `narrative_plan` downgrades new contract-v3 output into the legacy score-only path,
  allowing blocker-bearing artifacts to receive completion certificates.
- Verification remains open. A ten-episode terminal-success run is not sufficient;
  every published episode must also prove zero production blockers and a correctly
  bound narrative authority.

## Implemented Fixes
- Bumped the screenplay contract to `4.0.0` and wired the complete
  `narrative_plan` contract and schema into the Baseline prompt.
- Removed the product duration maximum. User input is now a minimum pacing hint;
  the persisted duration expands from spoken capacity, spine beats, and scene count.
- Changed screenplay QA to a blocking `runtime_gate`. Completion certificates derive
  blocker and must-fix counts from their immutable Evaluations and reject nonzero
  counts; callers can no longer submit zero manually.
- New contract output cannot downgrade to the legacy path by omitting
  `narrative_plan`. Existing invalid certificates are quarantined during startup.
- All production issues now enter bounded local Patch. The final Agent Loop failure
  reports the newest structural regression before older candidate blockers.
- Issue identity preserves exact node paths and array indexes. Validation points emit
  typed codes for dialogue length/function/source evidence and source coverage links.
- Current-source character discovery scans every bounded batch, not only the first
  18,000 characters. Future canonical identity no longer rewrites the current
  audience-visible label.
- Bible JSON, version, and Artifact pointer advance through one CAS authority update.
- Batch generation now creates a durable `screenplay_batch` parent Run, exposes
  `queued` separately from `running`, supports live concurrency resize, and cancels
  all selected Tasks before awaiting any one of them.
- Run snapshots record pipeline, prompt, QA, provider, model, concurrency, and
  unbounded-duration policy versions.
- Provider metadata compaction always emits valid JSON. Recovery cache reuse requires
  an explicit durable semantic operation ID.

## Automated Verification
- Backend: `1763 passed, 4 skipped`.
- Frontend: `158 passed`.
- Frontend production build: passed.
- Added regressions for certificate-derived blockers, required narrative authority,
  unbounded duration, exact Issue fingerprints, complete identity-source batching,
  future-identity display preservation, valid metadata compaction, durable Batch, and
  two-phase cancellation.

## Post-Fix Runtime Verification
- Backend process loaded contract `screenplay@4.0.0`.
- Invalid v3 publications were removed from the active projection; episodes returned
  to a non-ready state while historical Artifacts and Evaluations remained auditable.
- Runtime provider metadata currently has zero invalid JSON rows.
- Active manual verification Runs:
  `run_0bdd4646c751` (episode 1) and `run_1a4643a470eb` (episode 2).
- Durable Batch Run: `run_253043b9a14d`.
- Queue evidence: two episodes are `running`; later episodes are explicitly `queued`.
- Episode 2 persisted Baseline `art_f34ddd706df9` with
  `baseline_generation_count=1`, `narrative_plan=object`, and model-candidate parent
  `art_9a92bf1d3934`. Its duration expanded from 50 to 170 seconds.
- Episode 2 did not publish with open gates. Run `run_1a4643a470eb` ended `PARTIAL`
  and preserved a working Artifact after deterministic/local repairs.
- Episode 1 received a provider policy-refusal response during structural bootstrap.
  Run `run_86ffa0c06bf6` ended `FAILED`; no screenplay certificate or ready projection
  was created.
- Batch `run_253043b9a14d` was cancelled atomically with all 28 children recorded as
  `CANCELLED`; no cancellation cascade started the remaining children.
- The next Prompt contains actual authorized chapter IDs (for example `6675` and `2`)
  and contains no `current-source-chapter` placeholder.
- Runtime provider metadata has zero invalid JSON rows.
- A subsequent episode-2 Baseline is still running; long model generation is allowed
  by the unbounded duration policy and has not been reported as success.
