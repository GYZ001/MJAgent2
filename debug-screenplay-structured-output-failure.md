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

## Verification Conclusion
- A Rejected：响应包含任务 JSON 开头，不是无 JSON。
- B Rejected：`extract_json` 正确拒绝了被截断的字符串，未误判完整结果。
- C Rejected：该调用未设置 `expected_json`，网关判定未参与本次失败。
- D Confirmed：推理占满 900 token 预算，JSON 正文被截断。
- E Rejected：只有一次 `assess_new_character` 调用，未发生 operation 复用。
