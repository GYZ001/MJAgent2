# Evidence Harness 运行手册

## 冻结合同

- Episode Mapping 由确定性代码执行，一章严格对应一集，不调用模型。
- 每个视频镜头由分镜模型按单一连续动作与口播密度选择 5~10 秒整数时长；选择能自然完成内容的最短时长，超过 10 秒仍放不下或出现不同节拍时拆成相邻镜头。
- Storyboard 逐镜循环每轮只接受单数 `shot`；超出 10 秒口播容量的大纲内容由 Harness 在模型调用前确定性拆成多个 checkpoint，模型不得用 `shots` 数组绕过合同。
- 功能性路人使用确定性通用标签白名单：可以入画、开口，但必须在画面描述中明确调度，且不创建持久角色圣经身份；其它圣经外具体姓名继续由硬门禁拒绝。
- 模型、文件校验器失败不能伪装成通过；恢复结果不能独立触发自动采用。
- 付费媒体必须先原子预留预算，并由 lease 的 CAS 所有者执行。
- T5 交付包必须同时具备文件门禁、人工决定、完整血缘和至少 90% 证据覆盖率。

## 日常操作

1. 在运行中心检查 Run/Step 时间线、门禁队列、退出原因与 Artifact 血缘。
2. 人物谱、剧本、分镜的 warning/blocker 未解决时，不进入下游。
3. 场景图和视频先横向比较候选，再自动或人工采用；人工采用必须填写理由。
4. 成片台先执行 Delivery Readiness，再生成交付候选。复验人可批准、带风险批准或拒绝。
5. 报告和 ZIP 从交付包列表导出；客户反馈会追加不可变 Evaluation，并可创建修订 Run。

## 故障恢复

- 启动时由单一恢复协调器先做数据库对账，再恢复 worker 和业务编排。覆盖自动项目、人物谱、
  人物/场景参考图、分集规划、剧本、逐镜分镜、关键帧/视频和交付包。父编排先恢复，子阶段会跳过已被父编排
  接管的 scope，避免双重续跑。
- 过期运行 lease 会置回队列，尚未到 `next_retry_at` 的任务会重新建立定时器。旧的失败 Run/Step 不会
  被改回运行中；续跑会创建新迭代并通过 `parent_run_id` / `parent_step_run_id` 保留恢复链。
- 已拿到 provider task id 的视频任务只恢复轮询，不再次创建付费任务。
- 视频创建请求使用基于 version 的稳定 operation/idempotency key，并持久化 `submitting/unknown/accepted`
  创建状态。这能在上游支持幂等键时抑制重复创建；若上游不支持幂等且连接在“提交成功、回包前”中断，
  系统会在监控中标记结果未知，不宣称绝对 exactly-once。
- 供应商调用按稳定 `operation_id` 记录尝试链。重启时未回写的 `RUNNING` 记为 `INTERRUPTED`；
  后续尝试会链接为“重试中/已恢复”，不再永久显示一条孤立的“服务重启、结果未回写”。
- 旧进程的迟到回包不能覆盖新进程写下的 `INTERRUPTED`；worker 一旦失去 lease，就不能再完成、重排或改写
  新 owner 的任务。`scripts/dev.sh restart` 会按独立进程组停止旧服务，避免热重载长请求造成新旧实例重叠。
- 图片、JSON、文本、导出文件和 ZIP 复制采用同目录临时文件 + `fsync` + 原子替换；启动会清理
  遗留的 `.part` 文件，业务层不会看到半写入成品。
- provider 不可取消时，用户取消会把本地任务标为 `abandoned`；上游结果不再采用，预算预留释放。
- 预算暂停后先调整单集上限，再调用剧集恢复接口；原 job 和 reservation 被复用。
- Storyboard 按逐镜 Artifact checkpoint 恢复，已通过镜头不重做。
- 分镜台在「已有落库镜头 + script_error + status 为 scripted/script_failed」时显示“从镜 N 继续”
  （含追加失败、取消保留 checkpoint、单镜需修改等）；该入口创建带 `parent_run_id` 的续跑 Run，
  并以 `resume=true` 从下一镜生成。“重新生成整版”才会主动清空旧镜头。
- 文本 provider 的可重试 429/网络/5xx 在 Harness 网关按 30s / 60s / 120s 有界退避；
  独占 Run 进入 `WAITING_RETRY` 并记录调度/恢复事件。若冷却期间服务重启，旧 Run 明确转为
  `PAUSED_EXTERNAL`，Storyboard 启动恢复器从最后一个逐镜 checkpoint 创建续跑 Run。

重启后先执行：

```bash
scripts/dev.sh status
curl -fsS http://127.0.0.1:8230/api/system/jobs
```

`startup_recovery` 应列出本次各工作流的接管数；存量任务应显示“恢复中”或新尝试的运行状态，不应只留在
“外部中断”。还可用 `GET /api/system/calls` 查看 `operation_id` 的旧尝试与新尝试链接。

## 发布门禁与双轨基准

使用 `POST /api/benchmarks` 提交旧链路 baseline 和 Harness candidate。严禁用候选数据冒充基线。真实项目记录必须显式提交 `is_real_project=true`、`attested_by` 和脱敏说明；未声明真实来源的 demo/金样记录不会进入发布门禁。发布门禁要求三个不同真实项目的最新基准均通过：Verified Delivery Rate 与一次验收率不低于旧链路，证据覆盖率不低于 90%，状态漂移和重复计费为零。

金样合同位于 `.benchmarks/golden_cases.json`。本地验证：

```powershell
py scripts/check_contract_surface.py
py -m compileall -q app
py -m pytest -q
cd frontend
npm.cmd run build
```

默认阈值只允许通过真实双轨结果调整。每次调整需保留 benchmark 记录、样本项目、旧/新指标、回归项和决定人；不得为了让门禁变绿而降低证据覆盖率或放宽重复计费/状态漂移上限。
