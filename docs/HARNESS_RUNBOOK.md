# Evidence Harness 运行手册

## 冻结合同

- Episode Mapping 由确定性代码执行，一章严格对应一集，不调用模型。
- 每个视频镜头固定 5 秒；动作或口播放不下时拆成相邻镜头。
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

- 启动时会把过期运行 lease 置回队列，并为尚未到 `next_retry_at` 的任务重新建立定时器。
- 已拿到 provider task id 的视频任务只恢复轮询，不再次创建付费任务。
- provider 不可取消时，用户取消会把本地任务标为 `abandoned`；上游结果不再采用，预算预留释放。
- 预算暂停后先调整单集上限，再调用剧集恢复接口；原 job 和 reservation 被复用。
- Storyboard 按逐镜 Artifact checkpoint 恢复，已通过镜头不重做。

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
