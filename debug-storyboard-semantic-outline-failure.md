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

## Full-coverage Regression
- 用户复核旧 12 镜后确认仍有叙事省略：饭后卧室未生成、从家到学校割裂、
  下药过程缺失。
- 运行与离线投影证据确认：剧本文字包含这些场次，直接丢失点是旧
  `normalize_narrative_storyboard_outline()` 每个 event 只保留第一镜；
  `must_keep spine` 提示又被误用成内容白名单。
- 修复后新大纲为 35 镜、9 个顺序场次：客厅、饭后卧室、办公室、闪回、
  返回办公室、学校走廊、办公室对白、周六卧室、高义家。
- 当前权威 Run：`run_5649a2edebc8`，正式落库 15 镜，所有镜头
  `source_excerpt` 均有效；饭后卧室为第 4~5 镜，学校走廊为第 14~15 镜。
- SC07 场景包因历史大纲中的 `KL04` 重复 owner 局部失败，已自动进入
  第 16~23 镜逐镜回退；当前第 16 镜第 2 轮修复在供应商调用中。
- 新增全局 key-line owner 唯一门禁会阻止后续大纲再次批准相同问题；
  当前运行只处理失败窗口，不清空已通过的 15 镜。

## Full-coverage Final Verification
- 运行时再次确认旧 Run 在模型 await 后失去所有权，仍可进入
  `_pause_with_unpublished_storyboard()` 污染 episode 状态；新增所有模型/路由
  await 返回后的所有权检查及终态暂停二次围栏，竞态回归测试已转绿。
- 历史大纲迁移已确定性修复：
  - 引号内对白不再被动作拆分器截断；
  - `KL04` 仅由第 18 镜交付；
  - 第 19、21 镜改为白洁无台词反应；
  - 未提交的迁移前 repair candidate 被废弃，不覆盖正式镜头。
- 正式结果共 35 镜、9 个顺序场次：
  - 第 4~5 镜：饭后卧室；
  - 第 14~15 镜：学校走廊；
  - 第 16~23 镜：校长办公室完整对白与反应；
  - 第 24~25 镜：周六卧室准备与出门；
  - 第 26~35 镜：进门、递总结、两杯咖啡、头晕倒下、确认昏迷、
    后续动作与拍照。
- 所有 35 镜均有不少于 8 字的连续 `source_excerpt`，人物关系称谓已规范为
  `路人甲`；场景覆盖、关键台词 owner、导演字段和完整 storyboard 校验均为
  0 错误。
- 冷观众报告 `art_668ffcdf1529` 与最终重签报告均为 `decision=pass`，
  AP-1/AP-2 共 5 个 target delta 全部 `satisfied`，低分位路径通过。
- 最终 Run `run_7a2110f8d54c` 为
  `WAITING_HUMAN_CALIBRATION`；这是一次观看校准门，不是生成失败。
- 本调试会话继续保持 `[OPEN]`，待用户观看并确认后再清理调试文件与插桩。

## Confirmation Gate Verification
- 用户点击确认时复现两类误判：
  - 确认门按去重后的 `scene_name` 判断顺序，把办公室、卧室和高义家的合法
    复访误报为“场景倒退”；
  - 35/35 镜已完成，但一次观看校准与完成证书尚未收口，误显示为非完整终态。
- 场次门禁现按保留重复项的剧本场次序列做子序列覆盖；嵌套子场景可额外出现，
  但剧本中重复场次必须分别按顺序命中。
- 导演字段、连续性和身份规范化结果已同步回批准大纲，后续
  `finalize_evidence` 不再反复重签镜头 Artifact。
- 最新稳定审读报告：`art_8fa8a68581e6`；AI 一次观看模拟权威：
  `art_38cedb5c0937`，最低逐目标分 0.9，高于 0.8 门槛。
- 最终发布 Run：`run_f8583ca2633e`，状态 `SUCCEEDED`；
  Storyboard Artifact：`art_b246e6c234d3`；
  Completion Certificate：`cert_66b6e29e7c1d`。
- 确认预览结果：35/35 镜、最终镜有效、hard errors=0、warnings=0；
  人工确认已提交，episode 状态为 `confirmed`，未自动产生视频费用。
