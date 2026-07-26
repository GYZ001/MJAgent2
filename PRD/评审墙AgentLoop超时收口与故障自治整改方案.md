# 评审墙 AgentLoop 超时收口与故障自治整改方案

> 状态：P0/P1 已实现并通过自动化验收；历史事故数据已完成只读预演，待用户显式确认收口  
> 日期：2026-07-26  
> 适用范围：评审墙“补齐到全片可用”视频 Supervisor、媒体 worker、候选采用、运行状态与计时展示  
> 本文优先级：在“截止时间如何处理候选、是否自动采用、何时停止生成”方面，本文取代《评审墙全片视频Supervisor-AgentLoop与全覆盖补齐方案.md》的旧规则。

## 1. 结论

本次事故不是单一的 Seedance 或 QA 质量问题，而是控制面失效后，执行面和前端仍各自继续运行或显示运行：

1. 当前 Supervisor 在启动约 9 分钟后访问了生产表中不存在的 `shots.state_out`，运行实际已经崩溃；
2. 崩溃没有触发集级收口、媒体任务熔断或 episode 状态复位，worker 仍独立完成重抽；
3. 前端把旧 checkpoint、`video_completion_mode='complete'`、`active_video_run_id` 和 `episode.status='generating'` 当成“仍在运行”，没有以真实 run/task 状态为准；
4. 现有 4 小时时长墙只会进入 `WAITING_AUTHORIZATION`，不会先采用已有候选，也不会停止/隔离在途媒体任务；
5. worker 和 Supervisor 同时拥有重抽及采用相关决策，形成两个互不一致的 AgentLoop；
6. checkpoint 中的旧尝试次数会盖住数据库真实次数，导致“已有 3 个版本仍显示 1/6”，兜底采用条件无法可靠触发；
7. 镜 2 的连续性任务把镜 1 作为尾帧锚点。镜 1 终态失败后，镜 2 仍被视为 active job，既不会降链，也不会转为可处理 Issue，形成无限等待。

因此，问题的根本原因是：**没有单一的集级决策权、没有强制截止收口事务、没有异常后的 fail-closed 机制，同时前端用推断状态冒充真实状态。**

整改后的硬规则如下：

> 在授权截止时间到达时，Supervisor 的采用权高于 QA 建议。只要某镜存在至少一个技术可播放、未删除、未失效的候选，就必须选择其中最优者并落盘采用；随后停止所有新增生成与重抽。没有任何可播放候选的镜头允许形成明确缺口，但不允许让整条流程继续假运行。

> “补齐到全片可用”只填补 `adopted_version_id` 为空的镜头。任何已有采用版——无论由用户还是 Supervisor 采用，也无论 QA、fallback 配额或上游版本风险如何——都必须保留并跳过，不得自动重生成、换版或撤销采用。相关风险仅展示提示；重新生成已采用镜头必须由用户通过单镜“重生成视频”显式发起。

## 2. 本次事故的可复核证据

### 2.1 运行时间线

事故对象：`ep_23517af4b5a8 / 第1章 陨落的天才`。

| 时间（Asia/Shanghai） | 持久化事实 | 结论 |
|---|---|---|
| 01:14:18 | `run_4c89c2023bd0` 启动，策略快照为 ¥1000、`wall_clock_cap_s=14400`、允许 fallback | 用户确实授权了 4 小时 |
| 01:23:09 | run 变为 `FAILED`，`failure_code=OPERATIONALERROR` | Supervisor 只运行了约 9 分钟 |
| 01:23:09 | `failure_message='no such column: state_out'` | 直接根因是代码与真实 schema 不一致 |
| 01:23 之后 | worker 继续生成和重抽，多个镜头的第三版在 01:24～02:00 落盘 | Supervisor 崩溃未熔断执行面 |
| 02:04:11 | 镜 4 的第三个 job 才结束为 failed；此前已有两个 succeeded 候选 | 候选存在但无人执行最终采用 |
| 09:42:24 | 镜 2 的 queued job 仍被反复更新为“等待镜 1 尾帧” | 连续性等待没有终态和超时转路由 |
| 09:42 | 最新 checkpoint 仍停在 01:23 的 `PLANNING_COVERAGE` | 页面展示的是遗留快照，不是活任务 |

`workflow_runs.deadline_at` 在本次运行中为 `NULL`。系统虽然把 4 小时写入了策略 JSON 和 grant，却没有把截止时刻提升为工作流一等字段，也没有独立 watchdog 对它负责。

### 2.2 镜头事实

| 镜头 | 数据库事实 | 页面问题 |
|---|---|---|
| 镜 1 | 2 个版本均 failed，无技术可用候选 | Supervisor 崩溃后没有继续自治，也没有形成明确终态缺口 |
| 镜 2 | 1 个 queued 版本，持续等待镜 1 尾帧 | 上游已经失败，仍无限等待连续性锚点 |
| 镜 4 | 2 个 succeeded 候选、1 个 failed 候选，`adopted_version_id=NULL` | 已有可用视频却长期停在“候选待采用” |
| 镜 3、5～12 | 共 9 镜已有 adopted 版本 | QA 低分镜仍显示“需重生”，且旧 run 让页面继续表现为补齐中 |

镜 4 的 v2 在截图与持久化 QA 中均为当前更优候选。按用户授权语义，它最迟应在截止收口时被采用，而不是继续等待 QA 达标。

### 2.3 之前两次失败暴露同类工程问题

同一集在 01:06 和 01:10 的两次 run 曾因读取不存在的 `gate_decisions.payload_json` 失败。该处后来已删除，但本次又由 `shots.state_out` 触发相同类型故障。这说明问题不只是漏改一列，而是：

- 业务模块手写不存在的列名；
- 单元测试只测纯函数或自建最小数据，没有执行真实 schema 上的完整分支；
- run 崩溃后没有自动收口，因此任何一个普通 SQL 错误都能把评审墙永久留在假运行状态。

## 3. 根因分解

### R1：生产 schema 漂移，异常分支没有测试

`app/video_supervisor.py::_apply_cascade` 直接执行：

```sql
SELECT state_out FROM shots WHERE id=?
```

但真实 `shots` 表只有 `shot_contract_json`、`last_frame_desc` 和 `observed_state_out`，不存在 `state_out` 列。计划状态实际保存在 `shot_contract_json.state_out` 中。

该错误只会在“新重抽已派发，随后计算连续性级联”时触发；现有测试测了 `should_cascade()`，却没有在生产 schema 上调用 `_apply_cascade()`，所以测试全绿仍会在线崩溃。

### R2：异常没有进入业务收口

`_recorded_video_completion_task` 捕获异常后只调用 `recorder.fail(exc)` 并重新抛出。它没有：

- 冻结该 run 的新增 dispatch；
- 取消或 abandon 该 run 创建的活动 jobs；
- 对已有候选执行一次安全采用；
- 清空 `active_video_run_id`；
- 把 checkpoint 写成真实终态；
- 把 episode 从 `generating` 复位；
- 通知 UI “Supervisor 已失败，但媒体队列可能仍有在途任务”。

因此，控制面死亡后，执行面和展示面都继续遗留。

### R3：4 小时时长墙的业务语义实现错误

当前主循环在检测到：

```python
now() - cp.started_at >= wall_cap
```

后直接写 `WAITING_AUTHORIZATION / VIDEO_WALL_CLOCK_EXCEEDED` 并返回。该分支位于候选兜底采用之前，也没有停止 jobs。

这与本次明确的产品语义冲突：4 小时是“到点交差并停止”，不是“到点继续保留未采用候选，再等用户追加授权”。

### R4：同一模式存在两个决策循环

在 `complete` 模式中：

- Supervisor 会路由 Issue、派发重抽并调用 `select_best_video_candidate(force_best=True)`；
- `_maybe_auto_qa()` 仍会自行 enqueue QA 重抽；
- worker 完成每个版本后仍会调用 `select_best_video_candidate()`；
- `complete` 模式仅把 worker 的 `force_best` 改为 `False`，并没有禁止它重抽或普通自动采用。

这导致预算、尝试次数、重抽原因和采用时机没有单一权威。Supervisor 崩溃后，worker 仍继续产生版本；Supervisor 活着时，两边也可能重复决策。

### R5：尝试次数会倒退

Coverage Ledger 使用旧 checkpoint 值优先：

```python
saved.attempts_paid or attempts_map_from_versions
```

一旦 checkpoint 里已经保存 `1`，后续数据库即使出现 2、3 个真实付费版本，结果仍为 `1`。后果包括：

- UI 长期显示 `1/6`；
- `exhausted_but_technically_ok()` 不会按真实次数触发；
- fallback 采用和单镜预算判断不可信；
- 预算面板沿用旧 checkpoint，与实际 worker 成本脱节。

正确规则只能是 `max(checkpoint_attempts, durable_version_attempts)`，数据库事实不得被 checkpoint 回滚。

### R6：连续性等待被错误当成永久 active

镜 2 的 job 处于 `queued + waiting_continuity_anchor`。因为 `queued` 属于 `ACTIVE_JOB_STATUSES`：

- Coverage Ledger 认为镜 2 有 active job；
- `actionable()` 跳过它；
- 上游镜 1 已终态失败也不会转成 `VIDEO_CHAIN_ANCHOR_BLOCKED`；
- worker 只会不断刷新 `updated_at`，形成永久活跃假象。

等待连续性锚点必须有 `blocked_since`、上游终态检查和最大等待时长；超过阈值后应交给 Supervisor 选择“重做链头 / 降链 / 截止时形成缺口”，不能继续算 active。

### R7：完成判定没有强制验证“已经采用”

`covered_within_quota()` 主要检查 A/B/C grade、stale 和 fallback quota，没有逐镜强制要求 `adopted_version_id` 已落盘。grade 又是从 best candidate 派生的，因此“有好候选”和“已经采用”在模型中被混为一谈。

完成条件必须改成：每镜要么有 adopted 版本，要么在终态报告中有明确 `NO_USABLE_CANDIDATE` 缺口；不能用 best candidate 指针冒充 adopted 指针。

### R8：前端把持久化意图当成活性事实

`WallPage` 只要看到以下任意状态就推断 Supervisor live：

- `video_completion_mode === 'complete'`；
- `active_video_run_id` 非空；
- 存在任何非终态旧 checkpoint；
- episode 仍为 `generating`。

这些字段在崩溃后均未清理，于是 `TaskTimer` 的浏览器计时持续增长。虽然 `/video-completion` 已能返回内存 task 的 `running=false`，评审墙主状态并没有以它为唯一依据。

## 4. 新的产品不变量

### 4.1 权限与门禁优先级

从高到低：

1. 人工已锁定的采用决定；
2. 授权 grant 的绝对截止和预算边界；
3. Supervisor 的候选选择与采用决定；
4. 技术可播放门禁；
5. QA 分数、连续性建议和重生建议。

QA 是正常运行阶段的优化目标；到了截止收口阶段，QA 只用于候选排序和风险标记，不再拥有否决技术可用候选的权力。

### 4.2 “截止时可采用”的最小定义

候选同时满足以下条件即可进入截止候选池：

- `shot_versions.status='succeeded'`；
- 文件存在且非空；
- 容器可识别、视频可解码；
- 未被删除、未被标记 stale；
- 不属于明确的法律/平台禁止内容。

以下 QA 问题在截止阶段不再阻止采用，只转成风险说明：低分、状态不一致、人物外观偏差、画面瑕疵、时长建议、`qa_recovered`、需要裁切、文字瑕疵等。

文件缺失、容器损坏、无法解码不属于“可用”；不存在候选时不得伪造采用。

### 4.3 fallback quota 的新语义

`max_fallback_shots` 只控制“截止前能否提前宣布高质量完成”，不再限制截止时的交差覆盖率。

- 截止前：A/B 达标并在质量配额内，可提前结束；
- 截止时：所有技术可用候选均必须采用，即使 B/risk 数量超过配额；
- 报告中记录 `quality_target_missed=true`，但不得因此继续生成。

## 5. 截止收口状态机

新增终态与阶段：

```text
RUNNING / OBSERVING / REPAIRING
  -> DEADLINE_CLOSING
     1. FENCE_DISPATCH
     2. STOP_OR_ABANDON_ACTIVE_JOBS
     3. SNAPSHOT_CANDIDATES
     4. ADOPT_BEST_AVAILABLE
     5. WRITE_CLOSEOUT_REPORT
  -> COMPLETED_DEADLINE_FALLBACK   # 每镜都有 adopted，但质量目标可能未达
  -> PARTIAL_NO_USABLE_CANDIDATE   # 至少一镜完全没有可用候选

任意未捕获异常
  -> RECOVERING_CONTROL_PLANE
  -> 恢复 Supervisor（截止前）
  -> DEADLINE_CLOSING（已到截止）
  -> FAILED_CLOSED（恢复/收口自身也失败，所有 dispatch 仍必须被冻结）
```

`WAITING_AUTHORIZATION` 不再是时长墙到点后的状态。预算在截止前耗尽时仍可等待授权，但绝对截止继续计时；到点必须进入 `DEADLINE_CLOSING`。截止后追加时间必须创建新 run，不得偷偷延长旧 run。

## 6. 截止收口算法

### 6.1 绝对截止

启动时一次性持久化：

```text
workflow_runs.started_at
workflow_runs.deadline_at = started_at + wall_clock_cap_s
checkpoint.deadline_at
grant.deadline_at
```

所有判断读取同一 `deadline_at`。禁止用 UI 本地时间、最后一次 resume 时间或最后一个 checkpoint 时间重新计算。

### 6.2 原子收口步骤

1. 以 compare-and-set 把 run 从活动态改为 `DEADLINE_CLOSING`；重复调用只允许一个成功；
2. 写入 `dispatch_fenced_at`，此 run 的 enqueue 入口从此返回 `RUN_CLOSED`；
3. 对尚未提交 provider 的 job 执行 cancel；对 provider 已接单且不可取消的 job 标为 abandoned，允许供应商继续但其晚到结果不得自动采用；
4. 以 `candidate.created_at <= cutoff_at` 建立不可变候选快照；
5. 保留人工锁定版本；其他镜头按以下顺序选一：技术通过、QA 分数高、版本号新；QA 缺失按风险候选处理而不是丢弃；
6. 对每个有候选镜头写入 `adopted_version_id`、`adoption_reason='deadline_fallback'`、比较集合与风险；
7. 对无候选镜头写 `NO_USABLE_CANDIDATE`，不再生成；
8. 写一次 closeout report，记录 adopted、missing、abandoned jobs、实际成本和质量目标是否未达；
9. 终结 run，清空 `active_video_run_id`，把 `video_completion_mode` 复位为 `quick`，消费 grant，停止计时；
10. 任一步失败均保持 dispatch fence，并由幂等 closeout 重试，绝不能回到生成态。

### 6.3 晚到结果隔离

每个 job/version 必须携带 `owner_run_id` 和 `run_generation`。worker 落盘前校验：

- run 仍允许接收结果；或
- 结果属于 cutoff 前已提交任务，但只能保存为 `orphan_candidate`，不得更改 adopted 指针。

这样可以同时做到“不丢供应商结果”和“截止后不再改变已经交差的版本选择”。

## 7. 删除与收敛的代码

### 7.1 本轮已经删除的确定性错误写法

- 删除 `_apply_cascade` 对不存在的 `shots.state_out` 的访问，改从 `shot_contract_json.state_out` 读取，并以 `last_frame_desc` 兼容旧数据；
- 删除“checkpoint 非零值优先”的尝试计数规则，改为 checkpoint 与真实版本台账取最大值；
- 新增基于生产 `db.SCHEMA` 的回归测试，确保连续性级联不再依赖虚构列。

### 7.2 P0 必须删除的重复控制逻辑

完整补齐模式中必须删除以下 worker 决策权，仅保留快速模式兼容路径：

- `_maybe_auto_qa()` 在 `video_completion_mode='complete'` 下自行 enqueue 重抽；
- worker 在 complete 模式下直接调用候选采用；
- 通过修改 `force_best=False` 来假装“已交给 Supervisor”的半禁用分支；
- 前端用 `video_completion_mode`、旧 checkpoint 或 episode status 推断 task live；
- L0 通过反复把 `retry_count` 重置为 0 来实现无限重排。

complete 模式中，worker 的职责仅为：执行一次明确 job、产出候选和 Evaluation、发布事件。重试、重抽、降链、采用、截止全部由 Supervisor 决定。

### 7.3 不属于本事故的文件

仓库根目录存在大量以下划线开头的诊断脚本/输出，且当前被 git 忽略。只读审计未发现它们被应用入口 import，也没有证据表明它们参与本次运行，因此不以“垃圾代码清理”为名盲目删除用户现场文件。后续可单独执行仓库卫生清理，但不得把它冒充本次根因修复。

## 8. 连续性死锁整改

为 `waiting_continuity_anchor` 增加明确状态：

```text
blocked_since
blocked_by_shot_id
blocked_by_terminal_failure
continuity_wait_deadline_at
```

规则：

1. 上游仍在真实 provider 运行：继续等待；
2. 上游已失败且无可用候选：当前 job 不再算 active，发布 `VIDEO_CHAIN_ANCHOR_BLOCKED`；
3. Supervisor 优先重试链头；链头无收益或临近截止时，选择 `degrade_chain`，去除尾帧依赖后生成；
4. 到绝对截止仍无候选：取消该 job 并形成明确缺口；
5. `updated_at` 的周期性刷新不能重置 `blocked_since`。

## 9. 持久化与健康检查

Checkpoint 新增：

```json
{
  "deadline_at": 0,
  "last_heartbeat_at": 0,
  "dispatch_fenced_at": null,
  "closeout_started_at": null,
  "finished_at": null,
  "terminal_reason": null,
  "quality_target_missed": false,
  "missing_shot_nos": [],
  "adopted_at_closeout": []
}
```

同时要求：

- `workflow_runs.deadline_at` 必填；
- `workflow_runs.status`、checkpoint phase 和 task lease 必须可交叉校验；
- heartbeat 超过 2 个 tick 未更新且 run 仍为活动态，watchdog 自动接管；
- checkpoint 只在状态变化、决策变化和固定低频快照时落盘，不再每个 tick 创建多份 Evidence Artifact；
- 异常恢复使用同一 run generation 或显式 child recovery run，不得留下多个“active”指针。

## 10. API 与评审墙整改

`GET /episodes/{episode_id}/video-completion` 必须返回服务端事实：

```json
{
  "run_id": "run_x",
  "run_status": "FAILED",
  "phase": "FAILED_CLOSED",
  "task_running": false,
  "heartbeat_stale": true,
  "started_at": 0,
  "deadline_at": 0,
  "finished_at": 0,
  "active_media_jobs": 0,
  "abandoned_provider_jobs": 0,
  "closeout": {}
}
```

前端规则：

- “运行中”只由 `task_running=true` 且 heartbeat 新鲜决定；
- `video_completion_mode` 只表示用户选择过的模式，不表示 live；
- checkpoint phase 只表示最后完成的业务阶段，不表示进程仍活着；
- 计时使用服务端 `started_at/deadline_at/finished_at`，不用 sessionStorage 累计推断；
- Supervisor 失败而媒体仍在途时分别显示“Supervisor 已失败”和“仍有 N 个媒体任务”，不得合并成一个绿色/黄色运行态；
- 截止采用的镜头显示“已采用（截止兜底）”，QA 风险作为说明，不再显示“需重生”；
- 无候选镜头显示“截止无候选”，整条流程显示“部分完成，已停止”，不继续转圈。

## 11. 当前事故数据的恢复方案

恢复工具必须先 dry-run，再由用户确认执行；不得直接重启旧 run 继续烧钱。

本集预期 dry-run 结果：

- 镜 4：比较已有两个 succeeded 候选，采用 v2；
- 镜 3、5～12：保留当前 adopted 版本，改写为截止风险说明，不再重生；
- 镜 1：无可用候选，记录 `NO_USABLE_CANDIDATE`；
- 镜 2：取消永久等待镜 1 的 queued job，记录 `NO_USABLE_CANDIDATE / BLOCKED_BY_SHOT_1`；
- 旧 run：标为 `PARTIAL_NO_USABLE_CANDIDATE`，清空 active 指针并停止计时；
- 不删除任何已生成视频，不自动启动新 run。

## 12. 实施顺序

### P0：停止假运行与到点不交差

1. 合入生产 schema 热修与尝试次数单调修复；
2. 新增绝对 `deadline_at` 与 `DEADLINE_CLOSING`；
3. 实现幂等的 dispatch fence、job cancel/abandon、候选快照和强制采用；
4. complete 模式关闭 worker 自主重抽/采用，只保留 Supervisor；
5. run 异常时进入 fail-closed，并由 watchdog 恢复或收口；
6. 修复前端 live 判定和服务端计时；
7. 提供当前事故数据 dry-run/repair 命令。

### P1：消除连续性与状态模型死锁

1. 连续性等待转成有截止的 blocker；
2. `covered` 强制要求 adopted 或显式 missing；
3. job terminal status 与 pipeline stage 同步落盘；
4. run/job/version 增加 generation fence，隔离晚到结果；
5. checkpoint 降频并清理重复证据写入。

### P2：质量与运营优化

1. 重新校准候选排序和风险标签；
2. 增加 Supervisor 心跳、截止收口耗时、孤儿任务成本指标；
3. 提供按 run 的成本与候选比较报告；
4. 再讨论默认 4 小时、重试预算和降链阈值，不得先于 P0/P1。

## 13. 必须补齐的测试

### 13.1 后端

- 真实 `db.SCHEMA` 上执行 `_apply_cascade()`，禁止访问不存在列；
- checkpoint attempts=1、数据库 attempts=3 时结果必须为 3；
- 截止时有一个低 QA 技术候选：必须采用并停止；
- 截止时有多个候选：必须按稳定排序采用一个；
- 截止时完全无候选：形成缺口并停止，不能 WAITING 或继续 queued；
- fallback 数超过质量配额：截止仍全部采用，但报告 `quality_target_missed`；
- Supervisor 抛异常：dispatch 立即被冻结，watchdog 能恢复；
- 异常后已过截止：watchdog 直接收口，不再生成；
- provider 晚到：保存 orphan candidate，不改 adopted；
- complete 模式 QA 低分：worker 不得自行 enqueue 重抽；
- 镜 1 终态失败：镜 2 在阈值内转 `VIDEO_CHAIN_ANCHOR_BLOCKED`，不得无限 active；
- closeout 重复执行：采用、报告、消费 grant 均幂等。

### 13.2 前端

- 旧 phase 为 `PLANNING_COVERAGE` 但 `task_running=false`：不得显示运行中；
- run FAILED 且有 queued media job：分别展示两种状态；
- 已到截止：计时冻结在 `finished_at`，刷新页面不再增长；
- 截止兜底采用：显示已采用与 QA 风险，不显示“需重生”；
- 部分完成：明确列出无候选镜头，不出现补齐中动画。

### 13.3 故障注入

- SQL 列不存在；
- 进程在采用前/采用中/报告后被 kill；
- grant 到期与 deadline 同时发生；
- provider 429/5xx 持续到截止；
- 连续性链头失败；
- closeout 事务中途数据库 busy。

## 14. 验收标准

P0 上线必须同时满足：

1. 任意授权时长到达后 60 秒内，run 进入明确终态；
2. 截止后新增 provider 提交数为 0；
3. 截止时每个技术可用候选镜头都有一个 adopted 版本；
4. 完全无候选镜头被明确列为 missing，不存在无限 queued；
5. `active_video_run_id`、task 状态、checkpoint phase 和 UI 展示一致；
6. Supervisor 异常后最多 2 个 tick 被 watchdog 发现，不再出现数小时假运行；
7. complete 模式只有 Supervisor 能决定重抽和采用；
8. attempts、成本和候选数均来自持久化事实，刷新/恢复后不倒退；
9. 全部目标测试通过，并至少完成一次“缩短截止为 2 分钟”的真实链路演练；
10. 当前事故集 dry-run 输出与第 11 节一致，用户确认后才能写入修复。

## 15. 监控与告警

新增指标：

- `video_supervisor_heartbeat_age_seconds`；
- `video_supervisor_deadline_closeout_seconds`；
- `video_supervisor_failed_with_active_jobs_total`；
- `video_supervisor_deadline_fallback_adopted_total`；
- `video_supervisor_deadline_missing_shots_total`；
- `video_supervisor_orphan_provider_cost_cny`；
- `video_continuity_blocked_age_seconds`；
- `video_checkpoint_attempt_regression_total`（目标恒为 0）。

告警：

- run 活动态且 heartbeat > 2 个 tick；
- run FAILED 但仍有未 fenced 的 active job；
- `now > deadline_at + 60s` 仍非终态；
- 有技术可用候选但终态 `adopted_version_id` 为空；
- continuity waiting 超过阈值且上游已终态失败。

---

本方案的核心不是继续提高重试次数，而是把“到点必须选、必须停、必须说清缺口”做成无法绕过的事务性终点。只有先恢复这一基本承诺，评审墙的 QA 优化、连续性优化和预算调度才有产品意义。

## 16. 实施与验收记录（2026-07-26）

### 16.1 已完成

1. 修复生产 schema 访问错误：级联逻辑改从 `shot_contract_json.state_out` / `last_frame_desc` 读取规划状态，不再访问不存在的 `shots.state_out`；
2. grant、workflow run、checkpoint 全链路持久化绝对 `deadline_at`，Supervisor 到点进入不可逆 `DEADLINE_CLOSING`；
3. 截止收口先冻结派发、停止/隔离本 run 的在途视频 job，再逐镜无条件保留已有采用指针；只对未采用镜头强制采用最佳技术可播候选，QA 只参与候选排序和风险标记；
4. 没有技术可播候选的镜头进入显式 `missing_shots`，流程终态为 `PARTIAL_NO_USABLE_CANDIDATE`；全部镜头有候选时终态为 `COMPLETED_DEADLINE_FALLBACK`；
5. complete 模式收回 worker 的自主重抽和采用权，由单一 Supervisor 决策；provider 晚到结果受 run owner/fence 约束，不得覆盖终态采用；
6. Supervisor 控制面异常采用三次有界恢复，仍失败则 fail-closed；应用启动时常驻 watchdog 检测陈旧心跳并恢复或直接收口；
7. checkpoint 尝试次数改为持久化事实单调合并，L0 免费重排设定硬上限，重复无变化 checkpoint 降频；
8. 连续性上游已无候选且无活任务时，下游等待转为 `VIDEO_CHAIN_ANCHOR_BLOCKED`，不再无限冒充 active；
9. API 返回 task/run/job 三层真实状态和服务端时间，前端不再根据旧 phase、mode 或 episode 状态猜测“运行中”，终态计时固定在 `finished_at`；
10. 新增历史事故只读预演及显式确认收口接口，评审墙在旧 run 已失败时展示“预演并确认收口”；确认操作不会启动新生成，也不会删除已有媒体；
11. 确认收口后立即采用接口返回的终态并等待评审墙数据刷新，显示明确的采用数与缺片数；已有采用版固定进入“已采纳”主状态，不得继续显示“需重生”。
12. 补齐台账、actionable 选择、连续性死锁清理和最终付费派发增加多层 adopted 保护；既有采用版不受 QA/stale/fallback 配额驱动而重烧，成本预测也只统计未采用镜头。
13. 最终入队前重新读取 `shots.adopted_version_id`，封住“台账生成后、付费派发前用户刚采用候选”的并发窗口；截止收口也只取消未采用镜头的在途任务，不误伤已采用镜头的独立重抽；评审墙同样以采用指针为最高优先级，即使采用版媒体健康异常、标记 stale 或另有手工重抽任务，也不得把主状态降回待生成/生成中。
14. “补齐到全片可用”的启动确认框明确展示本次只处理多少个未采用镜头、原样保留多少个已采用镜头，避免与“快速生成全部/单镜重生成”的语义混淆。

### 16.2 自动化验收结果

| 验收项 | 结果 |
|---|---|
| Python 完整测试集 | `483 passed, 1 skipped` |
| Supervisor / Worker / 评审墙状态聚焦回归 | `45 passed` |
| Python Ruff 静态检查（本次改动文件） | 通过，0 issue |
| TypeScript 类型检查 | 通过 |
| 前端 Vitest | `7 files / 21 tests passed` |
| Vite 生产构建 | 通过，77 modules transformed |
| `git diff --check` | 通过 |

新增回归覆盖生产 schema 级联、attempts 单调性、未采用不得算覆盖、全部已采用且 stale 时点击补齐零派发、截止低 QA 强制采用、完全无候选显式缺口、fallback 超配额仍收口、closeout 重入幂等、complete 模式禁止 worker 重抽、连续性死锁转 Issue、已采用镜头不参与死锁清理、截止不误停已采用镜头独立重抽、入队前并发采用终检、采用指针在媒体健康异常时仍为主状态、事故预演零写入等关键契约。

### 16.3 当前事故处置结果

对 `ep_23517af4b5a8` 已完成现场止损与最终复核：

- 误派到已采用镜 6 的 `job_fcd585ee5181` 已停止并隔离，供应商晚到结果受 owner/fence 约束，不能覆盖原采用版；
- 事故 run `run_76598269c9d6` 的在途镜 2 任务已取消，episode 的假运行标记已释放；
- 随后的 quick 模式仅对当时未采用的镜 1、2 生成候选并完成采用；
- 当前 12 / 12 镜均有采用指针和成功视频，`active_video_run_id=NULL`，`video_completion_mode=quick`，无活动视频 job；
- 镜 3～12 的原采用结果未被本次补齐替换。

现场处置只停止/隔离错误任务并释放失效控制面标记，没有删除媒体；采用结果来自正常候选比较。今后“补齐到全片可用”只处理未采用镜头，不再要求用户通过“预演并确认收口”手工补做自动流程。

### 16.4 未擅自删除的文件

排查确认根目录下的下划线文件和临时脚本不是本次事故执行路径的根因。由于没有证据证明它们是可安全删除的垃圾代码，本次未做破坏性清理；真正导致事故的失效分支、双重决策权和错误状态推断已在生产路径中修复或封死。
