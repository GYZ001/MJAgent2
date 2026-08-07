# Debug Session: screenplay-structured-output-failure
- **Status**: [OPEN]
- **Issue**: 剧本生成保留了工作副本，但因模型未返回可验证的结构化结果而停止。
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: .dbg/trae-debug-log-screenplay-structured-output-failure.ndjson

## Reproduction Steps
1. 在剧本台选择必保留台词。
2. 点击“首次生成剧本”。
3. 等待人物识别和剧本 Baseline。
4. 页面显示“剧本流程未完成”，错误详情提示结构化结果失败。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | HTTP 成功响应不包含任务 JSON | High | Low | Pending |
| B | 返回近似 JSON，但本地提取/修复逻辑误判 | High | Low | Pending |
| C | expected_json 网关判定错误触发无效重试 | Medium | Medium | Pending |
| D | 长剧本响应达到输出上限后被截断 | High | Low | Pending |
| E | 重试或 operation 复用绑定了错误响应 | Medium | Medium | Pending |

## Log Evidence
- 数据库 `provider_calls.id=59870`：HTTP 200，`finish_reason=length`，请求
  `max_tokens=900`，`completion_tokens=901`，其中 `reasoning_tokens=865`。
- 可见正文只生成 71 字，停在 `reason` 字符串中间。
- `.dbg/trae-debug-log-screenplay-structured-output-failure.ndjson:1`：
  `startsWithObject=true`、`endsWithObject=false`，本地复现同一
  `Unterminated string`。
- 本次 Run 在 `character_discovery` 失败，尚未进入剧本 Baseline。
- Post-fix `.dbg/trae-debug-log-screenplay-structured-output-failure.ndjson`：
  合法人物卡响应 `startsWithObject=true`、`endsWithObject=true`；
  “钟五”在人物卡模型前被跳过，`cardCallsAvoided=1`。
- 使用原失败 Run 的 current/future 两段模型响应在数据库副本重放：
  `checked=0`、`errors=[]`，未调用 `ensure_character_card`。

## Verification Conclusion
- A Rejected：响应包含任务 JSON 开头，不是无 JSON。
- B Rejected：`extract_json` 正确拒绝了被截断的字符串，未误判完整结果。
- C Rejected：该调用未设置 `expected_json`，网关判定未参与本次失败。
- D Confirmed：推理占满 900 token 预算，JSON 正文被截断。
- E Rejected：只有一次 `assess_new_character` 调用，未发生 operation 复用。

## Code Review Findings
- P1：任一场景包失败时清空全部并发成功候选，造成后续场景重复调用模型。
  已改为只移除失败场景，并把逐镜回退限制在失败场景窗口。
- P1：程序化台词装配默认使用 `spoken_dialogue`，可能把画外说话人强制入画。
  已按 `characters_visible/visible_entity_ids` 决定 `spoken_dialogue` 或
  `offscreen_voice`，画外说话人不进入可见角色列表。

## Fix Summary
- mentioned-only 的陌生具名身份不建人物卡；真正出镜/开口时再建卡。
- 人物卡结构化输出预算从 900 提升至 4096，并声明 `expected_json=true`。
- 解析失败改为明确的 `ContentGenerationError`，不再伪装成系统内部异常。
- 场景批量回退保留成功候选，避免重复付费调用。
- 画外音保持声音合同，不改变人物可见性。

## Verification Conclusion
- Pre-fix：人物卡请求上限 900，`reasoning_tokens=865`，
  `finish_reason=length`，可见 JSON 71 字且未闭合，Run 在
  `character_discovery` 失败。
- Post-fix：合法人物卡使用 4096 预算并返回闭合 JSON；
  原失败响应在数据库副本重放时，“钟五”直接标记
  `mentioned_only`，人物卡模型调用减少 1 次，`errors=[]`。
- 场景批量失败回退测试证明：SC002 失败时 SC003 成功候选保留，
  逐镜模型只调用 shot 2。
- 画外音测试证明：说话人保持 `offscreen_voice`，不会进入
  `characters/characters_visible`。
- 专项回归：204 passed；Ruff/py_compile/diff check 通过。
- 全量回归：1766 passed，13 failed，4 skipped。失败数与修改前远端
  基线一致，本次未新增失败。
