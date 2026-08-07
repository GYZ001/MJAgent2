# Debug Session: storyboard-semantic-outline-failure
- **Status**: [OPEN]
- **Issue**: 第一集分镜在大纲语义候选阶段进入 WAITING_HUMAN，未生成正式镜头。
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: .dbg/trae-debug-log-storyboard-semantic-outline-failure.ndjson

## Reproduction Steps
1. 第一集剧本已发布且 screenplay_status=ready。
2. 启动第一集分镜生成。
3. Supervisor 生成/修复大纲。
4. Run 进入 WAITING_HUMAN，failure_code=SEMANTIC_OUTLINE_CANDIDATE_REJECTED。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | 已发布剧本 narrative_plan 自身存在状态图硬错误 | High | Low | Pending |
| B | 分镜大纲模型重写程序权威状态字段并制造错误 | Medium | Medium | Pending |
| C | 剧本规范化器把事实同时标成初始事实和事件产物 | High | Medium | Pending |
| D | Supervisor 把上游剧本错误误路由成分镜修复 | High | Medium | Pending |
| E | 场景批量改造导致本次失败 | Low | Low | Pending |

## Log Evidence
Pending.

## Verification Conclusion
Pending.
