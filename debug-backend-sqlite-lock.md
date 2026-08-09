# Debug Session: backend-sqlite-lock
- **Status**: [OPEN]
- **Issue**: The backend keeps listening on 127.0.0.1:8230 but stops responding to every HTTP request.
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: .dbg/trae-debug-log-backend-sqlite-lock.ndjson

## Reproduction Steps
1. Start the project with `scripts/dev.sh start`.
2. Trigger storyboard and video completion work from the UI.
3. Keep the task and observability pages open.
4. Request `/docs` or `/api/system/health`; the connection is accepted but no response is returned.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | A connection keeps a SQLite write transaction open beyond its intended operation. | High | Medium | Confirmed: no-op stall reconciliation starts a write transaction and skips commit |
| B | An exception path leaves a write transaction or connection open. | High | Medium | Rejected for this incident: the leak is on the normal zero-row branch |
| C | Monitor event writes race with video completion writes. | Medium | Low | Rejected: pure board polling reproduces the delay without monitor writes |
| D | A long SQLite busy timeout blocks the asyncio event-loop thread. | High | Low | Confirmed by native sample and pre-fix debug lines 1-6 |
| E | Scheduled backend cycling causes the sustained outage. | Low | Low | Rejected: outage occurred before the scheduled restart deadline |

## Log Evidence
- Pre-instrumentation health probe timed out after 5 seconds with zero response bytes.
- A 5-second native process sample found the main event-loop thread in
  `sqlite3_step -> btreeBeginTrans -> sqliteDefaultBusyCallback` for all 4361 samples.
- The backend log contains an earlier `sqlite3.OperationalError: database is locked`
  from a storyboard background task.
- The current backend was started at 22:27:14 and had not reached its scheduled
  30-minute restart when it became unresponsive.
- Pre-fix instrumentation is attached in `app/db.py` and reports transaction
  boundaries, bounded write statement shapes, thread identity, and Python stacks.
- Clean pre-fix reproduction produced six events. Lines 1 and 3 show the main
  event loop holding an `UPDATE jobs` transaction inside the media stall
  reconciler. Lines 2, 4, and 5 show board-detail worker threads waiting on
  `BEGIN IMMEDIATE`; line 6 shows one board-detail request then holding that lock.
- In the same window, the health route took 2.608 seconds and the three board
  detail requests took 2.84-2.98 seconds.
- The exact normal-path leak is `reconcile_stalled_video_jobs`: its first UPDATE
  affected zero rows, but commit was conditional on a positive row count.

## Verification Conclusion
Root cause confirmed. Pending minimal fix and post-fix comparison.
