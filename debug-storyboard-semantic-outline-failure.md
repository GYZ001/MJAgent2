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
| A | 已发布剧本 narrative_plan 自身存在状态图硬错误 | High | Low | Confirmed |
| B | 分镜大纲模型重写程序权威状态字段并制造错误 | Medium | Medium | Rejected as root cause |
| C | 剧本规范化器把事实同时标成初始事实和事件产物 | High | Medium | Confirmed |
| D | Supervisor 把上游剧本错误误路由成分镜修复 | High | Medium | Confirmed |
| E | 场景批量改造导致本次失败 | Low | Low | Rejected as terminal root cause |

## Log Evidence
- `.dbg/trae-debug-log-storyboard-semantic-outline-failure.ndjson:4`：
  分镜开始前发布剧本已有 12 个 narrative graph 错误，且没有正式分镜。
- 同日志第 3 行：进入分镜语义修复路由的 11 类问题码与发布剧本问题码
  11/11 完全重合。
- 发布剧本 `art_48e7d3ccd578` 的 QA Evaluation 为
  `score_only/runtime_blocking=false`，记录 18 个 blocker 后仍发布。
- 剧本图中 `F-1` 同时位于 `initial_state_fact_ids`、
  `E-1.precondition_fact_ids` 和 `E-1.effects_add`。
- Run 共调用：大纲模型 2 次、场景包模型 6 次、逐镜模型 4 次、
  语义诊断模型 2 次；正式 shots=0。
- 场景包失败还包含局部程序合同误差：对白镜可见角色过宽、
  graph `E-1` 未映射 legacy `E1`、同场相邻镜仍为 `scene_change`。
- 日志第 1~2 行复放 SC001 时，当前场景任务本身只剩
  action_desc/shot_size 两项创作字段错误，证明场景批量机制不是
  12 个上游状态图错误的来源。

## Verification Conclusion
- A Confirmed：发布剧本在进入分镜前已有 12 个确定性图错误。
- B Rejected as root cause：大纲投影继承错误状态，但没有创造这 12 个错误。
- C Confirmed：`F-1` 被同时建模为初始事实和 E-1 新产物。
- D Confirmed：分镜修复路由接收并尝试修复不可变上游问题。
- E Rejected as terminal root cause：场景包有局部错误，但终态失败的
  上游问题在任何场景/逐镜候选中都会重复出现。

## Post-fix Verification
- 全新核心生成 Run：`run_977d27062357`。
- 大纲 1 次、场景包 4 次，共 5 次文本模型调用；4 个场景包全部首轮通过。
- 12 镜全部原子提交；未调用逐镜回退，未调用语义诊断。
- 旧失败基线：14 次文本模型调用、0 镜；新结果：5 次、12 镜。
- 发布剧本完整 QA 仍保留 12 项 score-only 审计问题，但分镜运行时
  `runtimeErrorCount=0`，没有隐藏或删除审计证据。
- 冷观众比较器修正为按自然语言语义匹配内部 DQ/XP/XD 目标；
  通过 report：`art_31b15b47e0a0`。
- 最终权威 Run：`run_628d85e3a155`，
  `WAITING_HUMAN_CALIBRATION`；该 Run 复用冻结审读证据，文本模型调用 0 次。
- 当前业务状态：`screenplay_status=ready`、正式 shots=12、
  `narrative_status=needs_review`，仅等待真人一次观看校准。
- 专项回归：176 passed；Ruff、py_compile、git diff check 通过。
- Run 所有权围栏已下沉到 Supervisor 与 `save_checkpoint()`；
  旧/取消协程不得再覆盖 episode 或 production revision checkpoint。
- 本调试会话保持 `[OPEN]`，待真人校准完成并由用户确认后再清理插桩和日志。
