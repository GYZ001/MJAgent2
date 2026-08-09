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

Latest evidence:
- H is rejected for the enqueue path: lines 187-196 show preflight and Prompt
  compilation completed with `db_in_transaction=false`; the stall begins on the
  first `shot_versions` write because another connection already owns SQLite's
  writer lock.
- I is rejected: lines 194-195 show deterministic Prompt compilation completed
  in about 2 ms after the preceding authority reads.
- J is rejected as the lock owner: lines 69-70, 94-95, 132-133 and 165-166 show
  budget reservation completing with no open transaction.
- A fresh process sample while line 196 was stalled shows both the event-loop
  thread and the Supervisor offload thread sleeping in
  `sqlite3_step -> btreeBeginTrans -> sqliteDefaultBusyCallback`.

Confirmed root cause:
- `_await_with_job_lease_heartbeat` runs input preparation in a child
  `asyncio.Task`, but passes the parent media worker's SQLite connection into
  that child.
- `_persist_reference_progress` first calls `set_pipeline_stage(..., conn=conn)`,
  opening a write transaction on the parent task connection.
- It then calls `_set_version()` without the supplied connection. `get_conn()`
  resolves a different connection for the child task, whose write waits for the
  parent connection. The parent task is awaiting the child and cannot reach the
  following `conn.commit()`, creating a deterministic self-deadlock.

| ID | Hypothesis | Status | Evidence |
|----|------------|--------|----------|
| H | Enqueue owns the long writer transaction | Rejected | Log lines 187-196 |
| I | Prompt compilation owns the stall | Rejected | Log lines 194-195 |
| J | Budget reservation owns the stall | Rejected | Log lines 69-70, 94-95, 132-133, 165-166 |
| K | Parent/child task connections split one reference-progress transaction | Confirmed | `run_job.py` reference progress ordering plus process sample |

Post-fix evidence for K:
- The restarted process completed enqueue version/trace/commit/budget boundaries
  on log lines 5-11 while generation-page polling continued to return HTTP 200.
- The same process completed Supervisor dispatch for shots 1 and 2 on lines
  54-75 without entering SQLite's indefinite busy loop.

New fresh-run issue:
- Lines 61 and 73 show `reused=true` for shots 1 and 2.
- The fresh owner only received succeeded preflight shell jobs; the exact-match
  media jobs remained `paused` under their previous Supervisor owner.
- Coverage filters active jobs by current `owner_run_id`, so the fresh run saw
  no active jobs and ended without resuming or submitting those versions.

| ID | Hypothesis | Status | Evidence |
|----|------------|--------|----------|
| L | Paused idempotent reuse closes the fresh preflight shell without transferring execution ownership | Confirmed | Post-fix log lines 54-75 and durable job rows |

Post-fix evidence for L:
- Fresh run `run_d3084479bb38` transferred paused shot-1 job
  `job_58f753338545` to the current owner and resumed it.
- That job reached provider `accepted` with a durable task ID while the
  successful provider-create ledger count increased by exactly one.

New watchdog issue:
- Dispatch reached shot 5 but that single dispatch took 68.2 seconds.
- `SUPERVISOR_HEARTBEAT_STALE_S` is 60 seconds, while dispatch refreshed
  liveness only before and after synchronous work.
- The watchdog therefore replaced the still-running Supervisor and closed its
  jobs with `SUPERVISOR_HEARTBEAT_STALE`.

| ID | Hypothesis | Status | Evidence |
|----|------------|--------|----------|
| M | Long offloaded dispatch lacks an in-flight heartbeat and is falsely taken over by the watchdog | Confirmed | 68.2-second dispatch versus 60-second stale threshold |

Watchdog fix verification plan:
- Refresh the active run every 20 seconds while a dispatch worker thread runs.
- Re-read owner and heartbeat immediately before watchdog takeover.
- On cancellation, keep the heartbeat alive until the worker thread exits.
- Recover an exact abandoned provider handle on the next explicit fresh run
  instead of creating a duplicate provider operation.
