# Debug Session: contextual-speaker-contract
- **Status**: [OPEN]
- **Issue**: 前五集剧本生成在人物身份预检阶段停止；`full_script_text.speaker` 中的「路人乙」既未进入人物谱，也未形成完整的 `identity_contracts + voice_bible` 可见/声音政策。预期是所有来源中实际出现的一次性或功能性说话人均通过本集权威身份合同泛化建模，不依赖白名单或黑名单。
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: .dbg/trae-debug-log-contextual-speaker-contract.ndjson

## Reproduction Steps
1. 启动当前项目服务与剧本生产任务。
2. 生成或恢复前五集剧本。
3. 观察人物身份预检对 `full_script_text.speaker` 的处理结果。
4. 使用 `sessionId=contextual-speaker-contract`、`runId=pre-fix` 收集证据。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Expected Signal | Evidence |
|----|------------|------------|--------|-----------------|----------|
| A | 人物预检已识别「路人乙」，但身份落实结果未写回 working Artifact | High | Low | 预检结果含该名字，落实后的合同/声音表不含 | Confirmed：日志第 2 行识别为 functional，但 resolutions 为空 |
| B | 身份合同存在，但 `voice_ids` 未精确连接 `voice_bible.speaker_id` | High | Low | 合同或声音表单边存在，解析器仍判定 voice 未定义 | Rejected：日志第 1 行显示合同和 voice_bible 均无「路人乙」 |
| C | 说话人抽取晚于身份落实，新增 speaker 未进入本轮预检输入 | Medium | Medium | 预检输入缺少该名字，最终扫描首次出现 | Rejected：日志第 2 行已进入恢复审计候选 |
| D | 恢复/发布路径读取旧 working Artifact，覆盖了身份落实后的版本 | Medium | Medium | 落实后含合同，发布前重新读取时消失或版本变化 | Confirmed secondary：恢复审计改变 authority fingerprint 后 supersede `rev_4e830c1ca661`，新建 `rev_14f215f0a390` 并重跑 Baseline |
| E | 名称规范化导致同一身份在抽取、合同和声音表之间不一致 | Low | Low | 三处 token 仅在空格、标点、别名或规范名上不同 | Confirmed：结构化 speaker 是「路人乙（小晶的声音）」，渲染正文投影退化为「路人乙」 |

## Instrumentation Design
1. 身份预检出口：记录候选身份和持久化前的姓名决议。
2. Baseline 后身份审计出口：记录 working Artifact、归一化变更、合同和声音身份。
3. 最终未解决身份扫描：记录名字、来源位置及解析器权威集合。
4. 剧本阶段阻断：记录最终 working Artifact 版本与身份错误。

## Log Evidence
1. `.dbg/trae-debug-log-contextual-speaker-contract.ndjson:1`：第 5 集未解析
   `路人乙@full_script_text.speaker`；合同仅有 `钟成`、`小晶`，声音表也仅有二者。
2. `.dbg/trae-debug-log-contextual-speaker-contract.ndjson:2`：恢复审计候选含
   `路人乙(functional, existing:路人乙)`，但 `resolutions=[]`。
3. `art_ff74c406a983` 的结构化对白 speaker 实际为
   `路人乙（小晶的声音）`；身份投影同时加入渲染正文解析出的有损 token `路人乙`。
4. 恢复请求原本指向 `rev_4e830c1ca661`，审计后 authority fingerprint 改变，
   该 revision 被 supersede，并新建零 Baseline 的 `rev_14f215f0a390`。

## Verification Conclusion
Pending pre-fix and post-fix comparison.
