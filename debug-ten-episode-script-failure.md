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
- Pending pre-fix and post-fix comparison.
