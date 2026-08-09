# Debug Session: content-gen-validation
- **Status**: [OPEN]
- **Issue**: “内容生成”未通过格式或业务校验，错误码 `GEN · ERR-20260809-b19c2d`。需要定位具体失败阶段、阻断 Issue 及修复重试未收敛的根本原因。
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: `.dbg/trae-debug-log-content-gen-validation.ndjson`

## Reproduction Steps
1. 在任务中心或对应内容生成页面定位错误码 `GEN · ERR-20260809-b19c2d`。
2. 查看该任务关联的生成 Run、修复尝试、原始 IR、规范化 IR 和校验 Issue。
3. 如现有持久化证据不足，启动 `pre-fix` 调试并重试一次内容生成。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Expected Signal | Evidence |
|----|------------|------------|--------|-----------------|----------|
| A | 模型原始输出不满足 IR/JSON 结构，解析阶段失败 | Medium | Low | 解析异常、缺字段或截断响应 | Pending |
| B | 规范化后仍存在 `must_fix/runtime_blocking` Issue，发布门禁拒绝通过 | High | Low | 校验结果包含阻断 Issue 及具体路径 | Pending |
| C | 修复循环对同一问题未收敛并耗尽重试上限 | High | Low | 多次 repair attempt 保留相同 Issue，最终 retries exhausted | Pending |
| D | IR/revision 持久化或指针状态不一致导致误判失败 | Medium | Medium | raw/normalized IR、revision、current pointer 不一致 | Pending |
| E | 上游流式响应中断、无心跳或超时被汇总成校验错误 | Low | Medium | 流中断、字符数停滞、超时或 provider 异常早于校验失败 | Pending |

## Log Evidence
- 外层错误 `ERR-20260809-b19c2d` 包装内层
  `ERR-20260809-848136`；内层异常为
  `OUTLINE_ACTION_DIALOGUE_RELATION_MISMATCH`。
- 失败 Run `run_ca1e4dd557c3` 从开始到失败约 1 秒，关联
  `provider_calls=0`，失败发生在本地确定性分镜大纲编译。
- 预修复 NDJSON 第 1 行：`KL13` 的规范化关系文本为 `啊啊`，长度 2。
- 第 9 行：`A-8` 对 `KL13` 是合法 `exact` 匹配。
- 第 11、12、14、18 行：同一个 `KL13` 对 `A-10/A-11/A-13/A-17`
  均以 `substring` 模式误命中更长对白；`KL11=好哥哥` 也存在同类误命中。
- 第 143 行：离线复放已发布剧本后稳定产生与生产异常完全相同的 5 个事件级阻断项。

## Hypothesis Status
| ID | Status | Evidence |
|----|--------|----------|
| A | Rejected | 已发布剧本和分镜投影均可解析；失败点是解析后的关系校验。 |
| B | Confirmed as symptom | 5 个确定性阻断 Issue 拒绝大纲，但 Issue 由错误的关系匹配制造。 |
| C | Rejected | 新 Run 首轮本地编译即失败，没有进入模型修复循环。 |
| D | Rejected | Run、Revision 和 screenplay Artifact 均绑定 `ep_66fe3940b561`，无指针漂移。 |
| E | Rejected | 本次 Run 没有 provider call，1 秒内本地失败。 |

## Verification Conclusion
根因是 `_action_key_line_ids()` 将长度仅 2 的规范化台词片段也允许作为任意
更长引号内容的子串命中。该规则原意是支持一条长对白被切成多个连续 key line，
但缺少“多个连续片段必须完整重组原引号”的约束，导致短语气词和常见短句污染
多个事件的台词权属。

## Fix
- 完整相等的说话人/台词关系继续直接命中。
- 长对白分片只有在同说话人、目录中连续、至少两个片段，并且顺序拼接后完整
  等于动作引号原文时才命中。
- 单个任意子串不再取得事件台词权属；没有添加内容白名单或样本专例。

## Post-Fix Evidence
- 同一生产 Artifact 离线复放：分镜大纲由 45 镜规范化为 44 镜，事件级
  `OUTLINE_ACTION_DIALOGUE_RELATION_MISMATCH` 从 5 个降为 0。
- 后修复 NDJSON 第 9 行：`A-8 -> KL13` 仍以 `exact` 合法命中。
- 第 11 行：长对白仍保留 `KL15 + KL16` 的
  `contiguous_fragments` 完整重组。
- 第 12、14、18 行：`A-11/A-13/A-17` 不再混入短子串 `KL11/KL13`。
- 第 143 行：最终 `error_count=0`。
- 新增回归测试先稳定复现 `['KL01', 'KL02']` 污染，修复后只保留
  正确的 `['KL02']`。
- `tests/test_narrative_outline_projection.py`：23 passed。
- 当前 `8230` 后端于 2026-08-09 10:25:33 重启，已加载修复。

调试状态保持 `[OPEN]`，等待用户在《少年阿宾》第 2 集执行一次分镜重试并确认。
