# Debug Session: ten-episode-script-failure
- **Status**: [OPEN]
- **Issue**: Script generation reaches baseline QA and then fails; the first ten episodes must succeed in one concurrent batch without sample-specific exceptions.
- **Debug Server**: Pending startup
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
- Pending global call-chain analysis.

## Log Evidence
- Pending pre-fix reproduction.

## Verification Conclusion
- Pending pre-fix and post-fix comparison.
