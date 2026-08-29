# 前后端模块耦合审查与解耦方案（2026-08-29）

测量口径：AST 解析全部 198 个 `app/**` 模块与 62 个 `frontend/src` 源文件，构建真实 import 图
（含 `from app import x` 形式与函数内延迟导入），Tarjan 求强连通分量；运行期结论均在
`.venv/bin/python` 里实际取对象 `id()` 比对，不靠读码推断。

## 一、总体测量

| 指标 | 值 |
|---|---|
| 后端模块 / 代码量 | 198 个 / 189,893 行 |
| 后端 import 边 | 1,204 条 |
| **最大强连通分量（循环依赖团）** | **118 个模块 / 156,321 行 = 全后端 82%** |
| 该团内部边中「函数内延迟导入」占比 | 429 / 684 = **62%** |
| 全仓函数内延迟导入总数 | 1,230 处 |
| 违反分层（下层 import 上层）的边 | 151 / 1,204 = 14% |
| 前端代码量 | 29,862 行 |

结论一句话：**后端不存在分层，82% 的代码是一个互相递归的整体**；它之所以还能被 Python 加载，
靠的是 1,230 处把 import 挪进函数体的延迟导入。真正的解耦对象是这个 118 模块的团。

## 二、五个根因（按危害排序）

### C1（P0，唯一有正确性风险的一项）`exec()` 命名空间外观造成运行期双份对象

`app/api.py`（25 行）把 `domain/` 下 7 个文件、`app/worker.py`（23 行）把 `media_exec/` 下 5 个文件
用 `exec(compile(...), globals())` 注入自身命名空间。每个被注入的文件顶部都有：

```python
try:
    router
except NameError:          # 被直接 import 时才走这里
    from app.domain.common import *
```

即同一份源码有两种加载方式。运行期实测（已执行）：

```
app.api 与 app.domain.storyboard_ops：132 个同名对象不是同一个（其中 34 个是可调用对象）
app.api 与 app.domain.video_ops    ：132 个同名对象不是同一个
app.worker 与 app.media_exec.run_job：14 个可调用对象不是同一个
app.api.router is app.domain.storyboard_ops.router  ->  False
```

具体危害，两类：

**① 异常类被复制，跨副本 `except` 抓不住。** 实测：

```
LeaseLost:              worker 与 run_job 非同一类，且互相不是子类（issubclass = False）
VideoPlanStaleFence:    同上
ReviewDependencyFence:  同上
VideoInputRepairRequired: 同上
```

这几个是媒体作业的核心围栏异常（租约丢失、计划过期、评审依赖、输入待修）。
`except worker.LeaseLost` 不会捕获 `run_job` 副本抛出的 `LeaseLost`——它会一路穿透到顶层
`except Exception`，被当成未知故障处理。目前测试全部走 `worker.` 前缀（`tests/test_media_scheduler.py:125`
等），所以测试看不见这个缺口。

**② 模块级可变状态被复制。** 实测复制的有：

```
app.media_exec.run_job: _workers, _reference_workers, _poll_workers,
                        _video_ready_workers, _retry_tasks, _worker_retire_events
app.domain.video_ops  : _project_video_queue_pause_requests,
                        _SCREENPLAY_READY_CACHE(经 common), _PROJECT_VIDEO_*_STATUSES
```

`_workers` 等是 worker 任务注册表，`_project_video_queue_pause_requests` 是「用户暂停整片队列」的
唯一信号源（`app/domain/video_ops.py:3945/4177`）。

**已核实的现状：暂停链路目前是自洽的**——`app/orchestration/api.py:230` 用
`from app import api as domain_api`，写入（`:757`）与队列协程的创建（`:637`）都落在 api 副本，
读取（`video_ops.py:4177`）也在同一副本。所以这不是当前的活缺陷。但 `video_ops.py:4234` 与 `:4680`
自身也会创建同一个队列协程，任何一条经模块副本进入的路径都会让「暂停」写在一个集合、读在另一个集合，
表现为按了暂停但队列继续跑——正是 CLAUDE.md「界面承诺必须与实际行为一致」禁止的那类失配。
它离发生只差一次普通的 import 改动。

**双份副本确实是活的，不是理论问题。** 全仓 44 处可执行的直接导入绕过了外观（7 处是 fallback 的
`import *`，其余 37 处是真实旁路），且分布在长驻循环侧：

```
app/storyboard_supervisor.py:189,222,223,972   app/video_supervisor.py:2578,3141
app/media_exec/enqueue.py:1252,1269,1458       app/production/storyboard_pack.py:48,1794,1996
app/capabilities/preflight.py:425,527,612,697,1075,1175
app/capabilities/handlers/screenplay.py:12,131  app/production/revision.py:1168
app/domain/video_ops.py:2989 -> app.media_exec.enqueue
```

即：HTTP 请求走 `app.api` 副本，而监工/看门狗/命令总线预检走 `app.domain.*` 副本。
这是同一个功能的两半跑在两份状态上。

**已核实为「浪费而非损坏」的一项：** `_SCREENPLAY_READY_CACHE` 的键
`screenplay_ready_identity()`（`domain/common.py:470`）是内容寻址的（含 episode/project/章节内容与
契约版本号），双份缓存只会让那段被注释标注为「全量哈希一次 130ms」的判定各算一遍，不会读到脏结论。

**未核实项（需要单独查）：** `media_exec/enqueue.py:1252` 从模块副本取
`ensure_storyboard_pack_release_gate_decision`（分镜包放行闸门）。该闸门是否有非 DB 的进程内状态，
我没有查到底；若有，同样落在 C1 的失配面上。

### C2（P0）`app/db.py` 既是最底层又 import 最上层

`db.py` 3,432 行，被 82 个模块依赖（全仓第一），但它自己 import 了 10 个业务模块：

```
app.completion_grant  app.production.revision  app.production.certificate
app.production.grant   app.production.shot_uid  app.delivery
app.artifacts          app.model_migration      app.observability.tracing
```

原因在 `init_db()`（`db.py:2486`）里：它同时做了五件事——建表、调用 8 个业务模块各自的 DDL
（`ensure_*_table`）、数据迁移、完整性修复（`_repair_dangling_video_adoption`、
`_reconcile_video_slot_activity`、`_quarantine_static_delivery_fallbacks`），以及**直接改业务状态**
（把 `episodes.screenplay_status='warning'` 重写为 `repairing`/`failed`，把
`projects.scene_refs_status` 改写为 `warning`）。

最底层模块 import 最上层，是那个 118 模块团在结构上无法拆开的地基。

另外两处同类倒置：`app/errors.py`（20 入度的叶子）import `app.db`；
`app/observability/api.py` import `app.video_supervisor`。

### C3（P0，最便宜）常量与纯谓词放错了模块

对团内 617 条带符号明细的边做统计：

```
携带 1 个符号: 328 条 (53%)
携带 2 个符号: 124 条 (累计 73%)
携带 3 个符号:  67 条 (累计 84%)
```

**53% 的循环边只为了一个符号。** 抽样看，多数是契约版本号常量与纯函数：

```
app.portraits   -> app.stages           :: _appearance_evidence_verified   （一个纯谓词）
app.screenplay_ir -> app.validators     :: derive_key_lines                （一个纯函数）
app.validators  -> app.screenplay_ir    :: IR_MIN_ADAPTED_SOURCE_RATIO     （一个常量）
app.narrative   -> app.continuity       :: PROMPT_CONTRACT_VERSION
app.compiler    -> app.spoken_contract  :: SPOKEN_DELIVERIES
app.production.revision -> app.production.screenplay_authority :: SCREENPLAY_QA_PROFILE_VERSION
app.production.screenplay_authority -> app.production.prep_pack :: QA_PROFILE_VERSION
app.production.screenplay_authority -> app.storyboard_authority :: OUTLINE_AUTHORITY_VERSION
app.screenplay_scene_shards -> app.renderability :: SCENE_STORY_FUNCTION_MIN_CHARS
app.production.screenplay_repair -> app.screenplay_scene_shards :: SCREENPLAY_MERGED_IR_VERSION
```

这说明**纠缠是浅的**：不是业务真的互相依赖，而是常量住错了地方。这一项投入产出比最高。

### C4（P1）两条并行入口，错误语义按入口不同

224 个 REST 路由 + 62 个命令总线 command，覆盖同一批操作
（`storyboard.confirm` / `video.adopt_version` / `screenplay.generate` …）。
`app/capabilities/handlers/*.py` 里 52 处 `from app import api`，即 handler 主要是在转调外观里的同一批函数。
`app/capabilities/bus.py` 的模块 docstring 自己写着「页面仍走现有 REST，待 M1+ 逐步改为调用本 Bus」——
迁移停在中间态。

`app/domain/*.py` 里 346 处 `HTTPException` vs 23 处裸 `raise ValueError`。CLAUDE.md 记的
「走命令总线转 409、不走的裸奔 500」缺口，就是这 23 处的分布问题。
（说明：我在 `bus.py` 里没有直接 grep 到 `ValueError -> 409` 的转换代码，这条转换发生在哪一层我没查实，
只确认了两条入口并存且 domain 层的异常类型不统一。）

### C5（P1）巨模块把多个关注点焊在一起

```
app/stages.py                  12,969 行 / 178 个顶层定义 / 30 出边 / 7 入边
app/portraits.py               10,807 行 / 135 个顶层定义 / 25 出边 / 16 入边
app/screenplay_scene_shards.py  6,518 行
app/production/screenplay_repair.py 6,398 行 / 29 出边
app/validators.py               6,289 行 / 15 出 / 20 入
app/domain/storyboard_ops.py    5,477 行 / 40 出边（全仓最高）
```

`stages.py` 一个文件里至少 7 个关注点：剧本 IR 保真 / 叙事蓝图分片与重试预算 / 角色点名与身份归并 /
人物谱生成与补充 / 别名取证 / 状态事实回填 / 章节认知卡。
`portraits.py` 与 `validators.py` 同时高入度又高出度——既被当基础库用，又反向调用上层，
是把循环焊死的两个节点。

### 前端（明显好于后端，问题集中在两处）

健康的部分，先说清楚：
- 端点 URL 收口良好：`api.ts` 之外只有 1 处硬编码 `/api/...`（`auth/session.ts`）。
- 页面之间零横向 import，组件只依赖 `components/` 内的通用件（`OperationError`/`DecisionDialog` 等）。
- 类型基本集中：`api.ts` 内 92 个类型声明，全前端重名类型只有 1 个（`PaymentSelection`）。

两个真实问题：

1. **`api` 上帝对象 + 逃生口**。`api.ts:301-847` 是一个 548 行、67 个方法的对象字面量，
   同时暴露 `post/put/del/get/upload` 泛型动词。页面用泛型动词的地方有 **88 处**
   （`MonitorPage` 22、`TeamAdminPage` 11、`BoardPage` 10、`BiblePage` 9…），
   URL 与响应形状的知识因此又漏回页面里，`api.ts` 的收口是半截的。

2. **`MonitorPage.tsx` 4,553 行 / 68 个 `useState` / 24 个本地类型声明**。
   它把 6 个监控分区塞在一个组件里，并在本地重新声明了 `Job` / `Call` / `SettingSchema` /
   `ModelCatalog` / `SystemOverview` 等后端响应类型——这些没有走 `api.ts`，
   后端改字段时前端不会编译报错。`App.tsx` 1,597 行 / 26 个 `useState`，路由与全局态混在一起，同类问题较轻。

### 已经解耦得不错的部分（不要动）

- **供应商层**。`app/harness/model_gateway.py` 是真正被遵守的抽象：全仓绕过它直接调
  `hiagent.chat*` 的只有 4 处（`video_plan.py`、`db.py`、`agent/orchestrator.py`，以及 gateway 自身）。
  `hiagent.py` 里没有以业务概念命名的函数，`seedance` / `minimax_h3` / `video_providers` 都是薄适配。
- `app/auth`、`app/authz`、`app/evidence`、`app/observability.tracing`、`app/atomic_io`、
  `app/schemas`（37 入度、0 出度）是干净的叶子。

## 三、解耦方案

原则依 CLAUDE.md：优先改现有代码、不引新框架、判据挂产物信号、一次删干净。
四步各自独立可交付、可单独回滚，**建议严格按序**——后一步依赖前一步腾出的空间。

### 第 1 步（P0）拆掉 `exec()` 外观，消灭双份对象

目标判据：`app.api.X is app.domain.storyboard_ops.X` 对全部同名对象成立（现在 132 个为 False）。

1. 把 `app/domain/common.py` 与 `app/media_exec/common.py` 变成**普通模块**：显式定义
   `router = APIRouter()` 并显式 `export`，删掉其余 chunk 顶部的 `try: router / except NameError: import *` 前导。
2. 其余 chunk 改为正常模块：顶部写显式 `from app.domain.common import router, get_conn, ...`，
   不再用 `import *`。
3. `app/api.py` 改成纯聚合：
   ```python
   from app.domain import bible_ops, projects, screenplay_ops, storyboard_ops, video_ops, review_wall
   from app.domain.common import router
   ```
   `app/worker.py` 同理聚合 `media_exec/*`。两个文件都不再 `exec()`。
4. **异常类与 worker 注册表必须先搬到 base chunk 并只留一份**：`LeaseLost`、`ProviderCreateUnresolved`、
   `VideoPlanStaleFence`、`ReviewDependencyFence`、`VideoInflightAdmissionDeferred`、
   `VideoInputRepairRequired` 已经在 `media_exec/common.py:77` 定义，只需保证所有引用都指向它；
   `_workers` / `_poll_workers` / `_retry_tasks` / `_worker_retire_events` 同样。
5. `_project_video_queue_pause_requests` 从模块级 `set` 改为**落库或落单例**，
   彻底断掉「暂停信号存在某个副本的内存里」这一类。

兼容性：32 个测试文件用 `app.api.*`、30 个用 `app.worker.*` 打桩。改成真模块后
`monkeypatch.setattr("app.api.f", ...)` 只会改到聚合模块的属性，而函数内部引用的是 `domain` 模块的全局名——
**这会静默失效**（打桩不报错但不生效）。所以本步必须配套：把这些测试的打桩目标改成
`app.domain.<chunk>.f` / `app.media_exec.<chunk>.f`。这是本步的主要工作量，也是必须一次做干净的部分
（CLAUDE.md：删一半会阻塞所有并行工作）。

验证：按 CLAUDE.md「改共享底层原语必须跑全量测试」——本步必须 `py scripts/verify.py --full`，
子集必然漏。另加一条独立观察点：写一个脚本断言 `app.api` 与各 `domain` 模块同名对象 `is` 相等。

### 第 2 步（P0）反转 `db.py`，把它压回底层

目标判据：`app/db.py` 的 import 列表里不出现任何业务模块。

1. **DDL 注册表**。新增 `app/db_schema.py`（只依赖 `sqlite3`），提供
   `register_table(name, ddl_fn)`；`completion_grant` / `production.revision` / `production.certificate` /
   `production.grant` 等各自在模块加载时注册自己的 DDL。`init_db` 只遍历注册表，不再 import 它们。
2. **把完整性修复与业务状态重写搬出去**。`_repair_dangling_video_adoption`、
   `_reconcile_video_slot_activity`、`_quarantine_static_delivery_fallbacks`、
   `_clear_orphan_storyboard_pack_placeholder_versions`，以及那两条改写
   `episodes.screenplay_status` / `projects.scene_refs_status` 的 SQL，全部移入 `app/recovery.py`
   （它已经是启动恢复的归属地）。`app/main.py` 的 lifespan 里本来就已经区分了
   `recovery_owner`，改为 `init_db()` 只建表、`recover_all()` 负责修复——这也修正了当前
   「schema 初始化顺带改业务状态」对 CLI 与测试的副作用。
3. `app/errors.py` 对 `db` 的依赖改为注入：`log_error` 收一个 sink，默认由 `main.py` 装配。
4. `app/observability/api.py` 不再 import `video_supervisor`，改为读它落库的产物信号。

**这一步触碰事务边界，必须逐条对照 CLAUDE.md「所有权必须显式」那一节**：
搬移过程中不得给任何函数保留 `conn=None` 默认值，回滚必须是 except 块第一条语句。
本仓已因隐式提交毁过三次真实数据。

### 第 3 步（P0，最便宜、可与第 2 步并行）抽出契约常量内核

新增 `app/contracts.py`（零依赖，只放常量与纯函数），迁入：

```
PROMPT_CONTRACT_VERSION           SPOKEN_DELIVERIES
IR_MIN_ADAPTED_SOURCE_RATIO       SCREENPLAY_MERGED_IR_VERSION
SCREENPLAY_QA_PROFILE_VERSION     QA_PROFILE_VERSION
OUTLINE_AUTHORITY_VERSION         SCENE_STORY_FUNCTION_MIN_CHARS
AI_VIDEO_PROMPT_CONTRACT_VERSION  NARRATIVE_CONTRACT_VERSION
```

再把纯谓词按归属搬正：`_appearance_evidence_verified`（`stages` → 取证模块，`portraits` 唯一要的就是它）、
`derive_key_lines`（`validators` → 台词工具）、`screenplay_beat_fields_repeat`。

预期收益：直接消掉 328 条单符号边中的相当一部分，并把 `stages ↔ portraits`、
`validators ↔ screenplay_ir` 这两对核心互锁打开。这一步风险最低——搬的都是常量和纯函数，
`py scripts/verify.py` 即可覆盖。

### 第 4 步（P1）按关注点切分巨模块

前三步做完，团的规模会显著下降，此时切分才有意义（先切会被循环拽回去）。建议顺序与切法：

- `app/stages.py` 12,969 行 → 按已经存在的自然边界切：
  `stages/screenplay_ir.py`（IR 保真）、`stages/blueprint.py`（蓝图分片与预算）、
  `stages/roster.py`（点名与身份归并）、`stages/bible.py`（人物谱）、
  `stages/alias.py`（别名取证）、`stages/status_facts.py`、`stages/cognition.py`。
  这些在文件里已经是连续的区段，切分主要是移动而非重写。
- `app/domain/storyboard_ops.py`（40 出边）与 `app/domain/video_ops.py`（35 出边）：
  把「读模型/投影」与「写副作用/编排」分开——前者可下沉为无依赖的查询层，后者留在 domain。
- `app/validators.py`：按 V1~V8 校验族拆，切断它对 `production.screenplay_document`、
  `scene_contract` 的上行依赖。

### 第 5 步（P2）统一入口与前端收口

- 后端：明确 REST 与 Command Bus 的边界并写进 `bus.py` 的 docstring（现在写的是尚未完成的迁移计划）。
  把 `domain/*` 里 23 处裸 `raise ValueError` 统一到与总线一致的映射上，消除同一异常在两条入口下
  409/500 不一致的缺口。
- 前端：
  1. `api.ts` 按域拆为 `api/bible.ts`、`api/video.ts`、`api/storyboard.ts`、`api/system.ts` 等，
     公共 `request` 留在 `api/client.ts`；泛型 `post/put/del/get` 收窄为内部使用，
     88 处调用点逐步换成具名方法，URL 知识不再外泄。
  2. `MonitorPage.tsx` 按 6 个分区拆成 6 个子组件，各自持有自己的状态（现在 68 个 `useState` 在一个作用域）；
     24 个本地类型声明搬进 `api.ts` 对应域文件，让后端字段变更能在前端编译期暴露。

## 四、优先级与不做的事

**P0（本次建议实施）**：第 1、2、3 步。它们分别消除唯一的正确性风险、结构性的依赖倒置、
以及 53% 的循环边。

**P1（下一轮）**：第 4 步巨模块切分。

**P2（暂不做）**：第 5 步入口统一与前端拆分。前端当前状态可控，不构成阻塞。

**明确不做**：不引入依赖注入框架、不引入事件总线、不做仓储层抽象。
本仓的问题不是缺抽象，是常量放错地方 + 两个 `exec()` 外观；加抽象层只会让 118 模块的团更难看清。

## 五、验证要求

- 第 1、2 步改的是共享底层原语，**必须 `py scripts/verify.py --full`**，不接受子集绿。
- 第 1 步需要独立观察点：脚本断言 `app.api` / `app.worker` 与各 chunk 模块的同名对象 `is` 相等；
  以及 `issubclass(run_job.LeaseLost, worker.LeaseLost)` 为 True（理想是同一个类）。
- 第 2 步需第二条独立连接读盘验证 `init_db` 不再改写业务状态。
- 每步后 `python3` 重跑本报告的 SCC 统计脚本，确认团规模单调下降；这是唯一能证明「确实解耦了」的判据，
  闸门全绿不算证据。
