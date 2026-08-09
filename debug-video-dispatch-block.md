# Debug Session: video-dispatch-block
- **Status**: [OPEN]
- **Issue**: Video Supervisor dispatch makes backend HTTP requests hang while jobs are created.
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: .dbg/trae-debug-log-video-dispatch-block.ndjson

## Reproduction Steps
1. Confirm episode `ep_0893abc3451e`.
2. Start full-episode video generation from the generation page.
3. Wait for the episode video plan to finish and jobs to enter dispatch.
4. Request `/docs` or refresh the generation page while job count increases.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | Synchronous dispatch loop blocks the event loop | High | Low | Pending |
| B | SQLite transaction remains open across await or a long loop | High | Medium | Pending |
| C | Concurrent workers contend on a shared connection or write lock | Medium | Medium | Pending |
| D | CPU-heavy projection, not SQLite locking, blocks requests | Low | Low | Pending |

## Log Evidence
Pending.

## Verification Conclusion
Pending.
