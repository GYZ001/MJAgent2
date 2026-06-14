# HiAgent 网关集成规范

> 本文档的事实分两类：
> - ✅ **已验证**：来自 1.0 项目真实运行记录（`data/store.json` 的 provider_configs / provider_calls / model_health_checks）或已跑通的代码路径
> - ⚠️ **待验证**：基于火山方舟 v3 协议的合理推断，必须在 M0 集成验证日实测后回填本文档

## 1. 网关与模型清单

| 项 | 值 | 状态 |
|---|---|---|
| 网关 Base URL | `https://hia.volcenginepaas.com/api/aigw/v1` | ✅ 已验证（健康检查 HTTP 200） |
| 鉴权方式 | `Authorization: Bearer <API_KEY>`（OpenAI 兼容） | ✅ 已验证 |
| Seedance 视频模型 ID | `d7jf6nd5boeaebtfbdqg`（1.0 配置名"HiAgent Seedance 视频"；用户已升级部署 Seedance 2.0，**模型 ID 需确认是否变更**） | ⚠️ ID 待确认 |
| Seedream 图像模型 ID | `d7ute7ppcc7n89uuqqp0` | ✅ 已验证（健康检查通过） |
| 文本/VLM 模型 ID | `d7ev7il5boeaebtf4sgg`（1.0 中同时承担剧本/分镜/角色抽取/VLM QA） | ✅ 已验证（真实调用成功） |
| 文本备选 | `deepseek-v4-pro`（1.0 中 llm_text 实际调用过） | ✅ 已验证 |

凭证管理：`.env` 文件（gitignore），变量名 `HIAGENT_API_KEY`、`HIAGENT_BASE_URL`、`MODEL_TEXT`、`MODEL_VIDEO`、`MODEL_IMAGE`、`MODEL_VLM`。代码中禁止出现任何密钥字面量。

## 2. 实测延迟数据（1.0 运行记录，超时设置依据）

| 调用 | 实测延迟 | 2.0 超时设置 | 重试 |
|---|---|---|---|
| chat/completions（单章片段抽取，deepseek-v4-pro） | 21.8s / 22.8s | **180s**（长输出的整集分镜可能远超片段抽取） | 2 次，指数退避 1.5s 起 |
| VLM 调用（d7ev7il5boeaebtf4sgg） | 56.9s / 66.5s | **300s** | 1 次 |
| Seedance 任务创建 | 0.12s（健康检查） | 30s | 2 次 |
| Seedance 任务完成（固定生成 10s 视频） | 未实测 | 轮询间隔 10s，总预算 **15min** | 超预算判失败，可手动重提 |

> 1.0 的核心教训：timeout=15s + 实测 22s = 100% 假失败 → 触发静默兜底 → 垃圾输出。**任何超时值必须 ≥ 实测 P99 的 3 倍。**

## 3. 各端点规范

### 3.1 文本生成（剧本/分镜/摘要）✅ 路径已验证

```
POST {base}/chat/completions
{
  "model": "<MODEL_TEXT>",
  "messages": [{"role": "system", ...}, {"role": "user", ...}],
  "temperature": 0.7,            // 创作阶段；修复回路用 0.2
  "max_tokens": 8192,            // ⚠️ 网关上限待验证；整集分镜 JSON 约 4~6k tokens
  "response_format": {"type": "json_object"}   // ⚠️ 网关是否透传待验证；不支持则靠 prompt 约束 + 代码侧提取 JSON
}
```

客户端实现要求：
- httpx 异步客户端，`timeout=Timeout(connect=10, read=180, write=30, pool=10)`
- 响应必须先过"JSON 提取器"（剥 markdown 代码块围栏、截取首尾花括号）再进 Pydantic 校验——不假设模型听话
- 每次调用写 `provider_calls` 日志（模型、延迟、状态码、token 用量若返回）

### 3.2 视频生成（Seedance 2.0）

**任务创建** —— 路径与请求体骨架 ✅ 已验证（1.0 探针真实返回 200）：

```
POST {base}/contents/generations/tasks
{
  "model": "<MODEL_VIDEO>",
  "content": [
    {"type": "text", "text": "<编译后的prompt> --ratio 9:16 --dur 10"},
    // 当前视频链路只传首/尾关键图，不与 reference_image 混用：
    {"type": "image_url", "image_url": {"url": "<本镜首图或上一镜尾图>"}, "role": "first_frame"},
    {"type": "image_url", "image_url": {"url": "<本镜尾图>"}, "role": "last_frame"}
  ]
}
→ 返回 {"id": "<task_id>", ...}
```

**任务轮询** ⚠️ 待验证（按方舟 v3 推断）：

```
GET {base}/contents/generations/tasks/{task_id}
→ {"status": "queued|running|succeeded|failed", "content": {"video_url": "..."}, "error": {...}}
```

**M0 验证结果（2026-06-12 实测回填）：**

- [x] **模型确认**：`d7jf6nd5boeaebtfbdqg` 底层即 `doubao-seedance-2-0`（轮询响应 error 报文中透出），`implement: volcengine`，无需换 ID
- [x] **轮询端点**：`GET {base}/contents/generations/tasks/{id}` ✅，响应字段：`status`（running/failed/succeeded…）、`content.video_url`、`content.last_frame_url`、`error.message`、`expired_at`（创建后 7 天过期，**视频必须立即下载落盘**）
- [x] **文本模型真身**：`d7ev7il5boeaebtf4sgg` = `doubao-seed-2-0-lite-260215`，是**推理模型**，响应含 `reasoning_content` 字段——JSON 提取必须只读 `message.content`
- [x] **⚠️ 网关不做同步参数校验**：非法参数（如 `--dur 999`）创建仍返回 200 + task id，仅在轮询时异步 failed。**参数校验必须 100% 前置在编译器**，否则浪费任务额度且失败延迟发现
- [x] **时长合法取值**：网关实测 `--dur 3` 非法、`4 / 12 / 15` 可创建；当前产品策略统一使用 **10s**，产品侧编译器只放行 `--dur 10`，并在 prompt 中要求模型在 10s 内尽可能塞入更多连续小镜头和剧情节点
- [x] **取消端点**：`DELETE .../tasks/{id}` 对运行中任务返回 500，**不支持取消**——合法参数的探测会真实计费，探测一律用非法参数（免费 failed）
- [x] 异步失败报文形态：`error.message = 'Error code: 400 - {"message":..., "code":"InvalidParameter", "param":"contents[0].text.duration"}'`
- [x] `GET {base}/models` 可用，列出全部 3 个已部署模型
- [ ] prompt 字符上限（待在真实长 prompt 中触发后回填；编译器先按 1500 保守值）
- [ ] 原声台词/音频输出能力（待验证；P0 视频按无声处理）

**一致性机制验证结果（2026-06-12 第二轮实测）：**

- [x] **reference_image 端到端可用**：Seedream 定妆照以 base64 data URL 传入 `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."},"role":"reference_image"}`，生成视频中角色发型/服装/五官与定妆照一致（目检通过，样片 `m0_samples/ref_test.mp4`）
- [x] **⚠️ first_frame/last_frame 与 reference_image 互斥**：混用返回 400 "first/last frame content cannot be mixed with reference media content" → 视频阶段只传预生成首/尾关键图；角色一致性在关键帧生成阶段注入
- [x] **⚠️ 成功任务不回传 last_frame_url**（succeeded 时该字段为空）→ 当前链路不再依赖回传或本地抽取尾帧，下一镜首帧直接使用上一镜预生成尾图
- [x] **Seedream 尺寸下限 3,686,400 像素**（400 报文明示）；定妆照用 1440x2560（与视频 9:16 同比例），返回 `data[0].url`（JPEG）
- [x] **HiAgent `/api/proxy/up/*` 文件接口（uploadraw/downloadkey）受 CSRF 保护**，程序化调用 403 EBADCSRFTOKEN，仅限控制台会话使用；本项目用 data URL 直传，不依赖文件托管
- [x] `/api/proxy/api/v1/create_conversation`（Agent 会话 API，Apikey 头鉴权）可用，但视频生成保留直连 aigw 任务 API（结构化、可轮询）

### 3.3 图像生成（Seedream 定妆照）✅ 路径已验证

```
POST {base}/images/generations
{"model": "<MODEL_IMAGE>", "prompt": "<锚点串+定妆照构图要求>", "n": 1, "size": "1024x1024"}
```

⚠️ 待验证：竖屏尺寸取值（如 `1080x1920`）、返回 URL 还是 b64_json。

### 3.4 VLM 质检 ✅ 模型可用已验证

走 chat/completions，messages 中混合 image_url（抽帧）与文本。
⚠️ 待验证：网关是否支持直接传视频 URL；不支持则本地 ffmpeg 抽 3 关键帧（首/中/尾）传图。

## 4. 错误分类与处理（迁移自 1.0 探针逻辑，该部分代码质量可靠）

| 信号 | 判定 | 动作 |
|---|---|---|
| 401/403 + 报文含 "no access to model" | 凭证有效但模型未授权 | 失败并提示去 HiAgent 控制台开通 |
| 其余 401/403 | API Key 无效 | 失败并提示检查 `.env` |
| 400/422 | 参数错误 | **不重试**，透出完整报文（编译器校验应已前置拦截，出现即编译器 bug） |
| 429 | 限流 | 退避重试（基数 5s 翻倍），队列并发数自动 -1 |
| 5xx | 网关/上游故障 | 退避重试 2 次后失败 |
| 超时 | 见 §2 | 按各端点重试策略 |
| 内容审核拒绝（报文形态 ⚠️ 待验证） | 敏感内容 | 不重试，镜头标记"被审核拒绝"，提供"软化改写"操作 |

所有失败的最终态都必须：① 写 provider_calls ② UI 红色可见 ③ 附原始报文前 500 字。禁止吞错误、禁止降级为模板输出（PRD 原则 P2）。

## 5. 并发与队列参数（初始值，可在 settings 调整）

| 参数 | 初始值 | 依据 |
|---|---|---|
| Seedance 并发 | 2 | 1.0 配置 max_concurrency=1~2 |
| 文本 LLM 并发 | 4 | 章节摘要可并行 |
| 队列重启恢复 | jobs 表持久化，启动时 running→queued 重新入队 | PRD §4.5 验收项 |
| 幂等键 | sha256(prompt + 全部参数 + 参考图hash) | 同键直接复用 shot_versions 已有结果 |
