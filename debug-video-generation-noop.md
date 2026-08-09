# Debug Session: video-generation-noop
- **Status**: [OPEN]
- **Issue**: Clicking Generate Video or New Video Version in the generation studio appears to do nothing; observability shows PARTIAL with VIDEO_PLAN_INVALID.
- **Debug Server**: http://127.0.0.1:7778/event
- **Log File**: .dbg/trae-debug-log-video-generation-noop.ndjson

## Reproduction Steps
1. Open the generation studio for project 少年阿宾, episode 2.
2. Select a pending shot.
3. Click Generate Video or New Video Version.
4. Observe that no visible task starts and the latest whole-episode task is PARTIAL / VIDEO_PLAN_INVALID.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Expected Signal |
|----|------------|------------|--------|-----------------|
| A | The click handler exits early because the UI believes generation is already active or blocked. | High | Low | Rejected: pre-fix line 1 reached submission with action=generate |
| B | The request is sent, but VIDEO_PLAN_INVALID is swallowed or rendered outside the visible page. | High | Low | Confirmed: line 2 returned accepted; async PARTIAL remains hidden behind stale generating display |
| C | Dynamic capability or shot-plan validation invalidates all pending shots. | High | Medium | Confirmed: line 4 reports AI_PLAN_SCHEMA_INVALID on SH019 required_assets[1].role |
| D | Stale task ownership makes duplicate suppression reject the new run. | Medium | Medium | Rejected: a fresh run and grant were created; task ended normally as PARTIAL |
| E | The UI-selected episode differs from the episode/run submitted to the backend. | Medium | Low | Rejected: line 1 and line 2 both use ep_66fe3940b561 |

## Log Evidence
- Pre-fix line 1: the confirmation handler entered with the expected episode,
  production eligibility true, no live supervisor task, and action=generate.
- Pre-fix line 2: the API returned accepted for run_c3ee8a47a828.
- Pre-fix line 3: Supervisor had no current reusable plan or grant binding.
- Pre-fix line 4: plan generation raised AI_PLAN_SCHEMA_INVALID at global index
  18 / SH019 because required_assets[1].role was `reference_image`.
- The run converged to PARTIAL / VIDEO_PLAN_INVALID with cost 0.0.
- Browser reproduction shows the page returning to "视频生成中" with all 44
  shots still pending and no persistent error explanation.

## Verification Conclusion
Root cause confirmed. Minimal fix applied:
- Added a typed PlannerShotAnalysis boundary. AI output owns relational facts only;
  executable mode, dependencies, and required assets are compiler-owned.
- Changed the planner contract to v2 so invalid v1 provider responses are not reused.
- Removed dead AI-authored asset revision validation from the execution compiler.
- Replaced phase-enumeration polling with task/active-job truth.
- Added a persistent PARTIAL outcome banner in the generation studio.

Focused verification: 75 backend tests and 31 frontend tests pass; TypeScript
typecheck passes. Pending post-fix browser and runtime-log comparison.

## Load Regression Evidence
- With 33 media jobs, the backend reached about 87% CPU and 12 consecutive
  health probes exceeded 5 seconds.
- The native sample placed about 77% of main-thread samples in synchronous
  SQLite execute/fetch paths.
- A prior SQL debug probe was attached to every connection and captured a
  Python stack for every write; the process reached roughly 106 threads and
  82 database handles.
- Chrome had 23 application tabs. Hidden tabs continued polling complete
  board/wall payloads; the target wall contains 44 shots and 83 versions.
- Project call observability scanned all 20k provider_calls rows. The table is
  about 528 MiB and the query took 6.9-11 seconds.

## Load Fixes
- SQL tracing now requires explicit `MJ_DEBUG_SQL_TRACE=1`.
- No-op pipeline stage writes no longer increment revisions or write WAL.
- Durable dispatch and full coverage-ledger rebuilds run off the uvicorn loop.
- Per-shot first-pass dispatch no longer rebuilds the whole episode ledger.
- Hidden tabs stop periodic polling after one initial load; wall detail polling
  while active is reduced from 2 seconds to 8 seconds.
- Provider calls persist project ownership and use a project/id index before
  Python aggregation.

Post-fix measurements:
- 50/50 health probes succeeded; most completed in 1-3 ms.
- Threads fell to 13 and database handles to 18.
- Project call query warm latency fell to about 30 ms.
- Planner revision 3 published valid with 44 shots; SH019 has exactly one
  compiler-produced `first_frame` input.
