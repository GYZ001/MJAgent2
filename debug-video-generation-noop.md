# Debug Session: video-generation-noop
- **Status**: [OPEN]
- **Issue**: Clicking Generate Video or New Video Version in the generation studio appears to do nothing; observability shows PARTIAL with VIDEO_PLAN_INVALID.
- **Debug Server**: Pending startup
- **Log File**: .dbg/trae-debug-log-video-generation-noop.ndjson

## Reproduction Steps
1. Open the generation studio for project 少年阿宾, episode 2.
2. Select a pending shot.
3. Click Generate Video or New Video Version.
4. Observe that no visible task starts and the latest whole-episode task is PARTIAL / VIDEO_PLAN_INVALID.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Expected Signal |
|----|------------|------------|--------|-----------------|
| A | The click handler exits early because the UI believes generation is already active or blocked. | High | Low | Click-entry event exists but no API-start event, with blocking state recorded |
| B | The request is sent, but VIDEO_PLAN_INVALID is swallowed or rendered outside the visible page. | High | Low | API error/partial response exists without a corresponding visible error state |
| C | Dynamic capability or shot-plan validation invalidates all pending shots. | High | Medium | Plan validation reports concrete missing/stale capability or input fields |
| D | Stale task ownership makes duplicate suppression reject the new run. | Medium | Medium | Active task/run IDs exist without a live task or valid lease |
| E | The UI-selected episode differs from the episode/run submitted to the backend. | Medium | Low | Click and request events contain different episode IDs |

## Log Evidence
Pending instrumentation and browser reproduction.

## Verification Conclusion
Pending.
