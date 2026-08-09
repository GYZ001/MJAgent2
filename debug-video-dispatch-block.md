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
| E | Media worker calls synchronous job/prompt work on the event loop | High | Low | Rejected: prompt prep/payload are 4-6 ms |
| F | Worker thread or SQLite contention starves all HTTP work | Medium | Medium | Rejected: setup is 2-30 ms, no transaction |
| G | Debug reporting or checkpoint persistence causes the second stall | Low | Low | Rejected: reporting active while HTTP remains responsive |

Iteration instrumentation:
- E: prompt preparation and payload construction before provider await
- F: worker synchronous setup before its first await

Post-fix evidence:
- Lines 26-33: shot 1 dispatch still took 14985.3 ms.
- Lines 34-47: shot 2 dispatch still took 23606.9 ms.
- Lines 39-42: prompt preparation took 4.8 ms and payload construction 6.3 ms.
- During those dispatches, five `/docs` requests returned HTTP 200 in
  0.0007-0.072 seconds.

Conclusion: the same expensive dispatch workload now runs off-loop via
`asyncio.to_thread`; server responsiveness is restored without weakening
authority, budget, or ordering checks.

Worker-iteration evidence:
- Worker setup stayed between 2 and 112 ms.
- Prompt payload construction stayed between 2 and 6 ms.
- Provider prompt awaits lasted 45-75 seconds without blocking HTTP.
- Health requests remained HTTP 200 in 0.001-0.057 seconds while Supervisor
  dispatch and media prompt generation overlapped.

The apparent second stall came from the old pre-fix process continuing its
event-loop dispatch before shutdown. No additional worker offload is required.

Later progress-stall evidence:
- Dispatch shot 5 entered `worker.enqueue_shot` and did not return for more
  than ten minutes.
- macOS process sampling showed the main event-loop thread sleeping inside
  SQLite `btreeBeginTrans -> sqliteDefaultBusyCallback`.
- HTTP responsiveness can therefore still be lost when the offloaded enqueue
  thread and event-loop media workers contend for a write transaction.

Next instrumentation will mark enqueue compile, trace, persistence, and budget
boundaries to identify the lock owner without changing transaction behavior.

| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| H | Enqueue holds a write transaction across trace/version preparation | High | Low | Pending |
| I | Deterministic prompt compilation itself is the long operation | Low | Low | Pending |
| J | Budget reservation opens the conflicting transaction | Medium | Low | Pending |

Enqueue instrumentation marks:
- authority/preflight completion
- deterministic Prompt compile start/end
- version INSERT and media trace boundaries
- persistence commit start/end
- budget reservation start/end
