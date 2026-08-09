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
| A | Synchronous dispatch loop blocks the event loop | High | Low | Confirmed: lines 2-9 |
| B | SQLite transaction remains open across await or a long loop | High | Medium | Rejected: all points report false |
| C | Concurrent workers contend on a shared connection or write lock | Medium | Medium | Rejected: line 1 has no active jobs |
| D | CPU-heavy projection, not SQLite locking, blocks requests | Low | Low | Confirmed as contributing work |

## Log Evidence
Instrumentation added:
- A: synchronous dispatch start/end and elapsed time
- B: worker enqueue start/end and elapsed time
- C: actionable batch size, active-job state, and transaction state

Pre-fix run ID: `pre-fix`.

Key evidence:
- Lines 2-5: shot 1 dispatch took 12109.9 ms; enqueue took 5256.6 ms.
- Lines 6-9: shot 2 dispatch took 12102.6 ms; enqueue took 5321.3 ms.
- Every point reports `db_in_transaction=false`.
- Concurrent `/docs` request timed out after 5 seconds.

## Verification Conclusion
The Supervisor executes the complete synchronous per-shot authority checks and
enqueue path on the asyncio event-loop thread. Each shot blocks the loop for
about 12 seconds, and the 137-shot first pass repeats that path without moving
the work to a worker thread. SQLite lock ownership is not the cause.

## Post-Fix Verification
Instrumentation retained with run ID `post-fix`; logs cleared before restart.
Expected comparison: dispatch duration may remain similar, but `/docs` must stay
responsive while dispatch runs because the work now executes via
`asyncio.to_thread`.

Observed iteration result:
- Supervisor dispatch no longer emitted on the event loop after restart.
- `/docs` initially returned in 2-65 ms.
- Once 14 media jobs entered `video_prompt_generate` / `job_queued`, five
  consecutive `/docs` requests timed out at 2 seconds.

New hypotheses:
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| E | Media worker calls synchronous job/prompt work on the event loop | High | Low | Pending |
| F | Worker thread or SQLite contention starves all HTTP work | Medium | Medium | Pending |
| G | Debug reporting or checkpoint persistence causes the second stall | Low | Low | Pending |

Iteration instrumentation:
- E: prompt preparation and payload construction before provider await
- F: worker synchronous setup before its first await
