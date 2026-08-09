# Debug Session: storyboard-limit-stop
- **Status**: [OPEN]
- **Issue**: 分镜任务的单镜 Agent Loop 已显示 `authority_blockers_exhausted（4轮）`，但总任务仍处于运行状态并继续处理，超过限制后没有自动停止。
- **Debug Server**: Pending
- **Log File**: `.dbg/trae-debug-log-storyboard-limit-stop.ndjson`

## Reproduction Steps
1. 启动分镜生成。
2. 当前 24 镜已通过，处理第 25 镜时持续触发 `DIALOGUE_FRAMING_INVALID`。
3. 单镜 Agent Loop 达到 4 轮并返回 `authority_blockers_exhausted`。
4. 观察总任务仍显示“分镜任务进行中，当前处理第 25 镜”。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Expected Signal | Evidence |
|----|------------|------------|--------|-----------------|----------|
| A | 单镜 Agent Loop 耗尽后被 Supervisor 再次路由修复 | High | Low | 同一 shot/issue 在 loop exit 后出现新的 attempt | Pending |
| B | Supervisor 与 Agent Loop 使用不同计数，未消费耗尽信号 | High | Medium | authority exit 已存在，但 activation/repair budget 仍允许循环 | Pending |
| C | 后端已停止，仅 UI/episode 活动指针陈旧 | Medium | Low | Run terminal 且无活跃 Task，但 episode 仍显示 active | Pending |
| D | 旧协程失去 Run 所有权后仍继续执行 | Medium | Medium | terminal/owner 变化后仍有 provider call 或 checkpoint 写入 | Pending |
| E | 暂停控制仅在模型调用边界消费 | Medium | Low | control 已写入但长调用未返回，当前 provider call 持续运行 | Pending |

## Log Evidence
Pending.

## Verification Conclusion
Pending pre-fix evidence.
