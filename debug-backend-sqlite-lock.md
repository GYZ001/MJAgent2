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
| A | A connection keeps a SQLite write transaction open across an `await`. | High | Medium | Pending instrumentation |
| B | An exception path leaves a write transaction or connection open. | High | Medium | Pending instrumentation |
| C | Monitor event writes race with video completion writes. | Medium | Low | Pending instrumentation |
| D | A long SQLite busy timeout blocks the asyncio event-loop thread. | High | Low | Native process sample strongly supports; pending instrumentation |
| E | Scheduled backend cycling causes the sustained outage. | Low | Low | Existing process timestamps reject it as the primary trigger |

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

## Verification Conclusion
Pending pre-fix instrumentation and reproduction.
