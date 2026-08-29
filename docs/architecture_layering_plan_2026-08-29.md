# 架构分层与企业级重构方案（2026-08-29 · 死代码清理后复测）

本文接续 `docs/coupling_review_2026-08-29.md`（以下称「耦合审查」）。耦合审查在
commit `639fe6f` 上完成测量并给出五步解耦方案；本文做三件它没有做的事：

1. 在**死代码清理之后**（commit `52eba2b`，删掉 9,743 行）重新取数，回答「清理有没有
   缓解耦合」；
2. 把耦合审查赖以判定成败的统计脚本**固化成可运行工具**（`scripts/arch_graph.py`）——
   原文把「团规模单调下降」定为唯一判据，但脚本没有入库，判据此前不可运行；
3. 补上耦合审查缺的那一半：**目标分层的显式定义**与**防回潮的自动判据**。原方案只说
   「把什么搬到哪」，没有说搬完之后「什么依赖什么」是合法的、以及靠什么阻止它退回去。

测量口径与复现命令：

```bash
.venv/bin/python scripts/arch_graph.py          # 摘要
.venv/bin/python scripts/arch_graph.py --json   # 机器可读，用于前后对比
```

---

## 一、结论先行：删代码不解耦

| 指标 | 清理前（639fe6f） | 清理后（52eba2b） | 变化 |
|---|---|---|---|
| 后端模块 / 行数 | 198 / 189,893 | 198 / 187,349 | −2,544 行 |
| 依赖边 | 1,204 | 1,122 | −82 |
| **最大强连通分量** | **118 模块 / 82.0%** | **112 模块 / 81.6%** | **−6 模块 / −0.4pp** |
| 团内单符号边占比 | 53% | 57% | +4pp |

删掉 9,743 行死代码，环只缩小了 6 个模块、0.4 个百分点。**这条数据是本文最重要的结论：
本仓的耦合是结构性的，不是垃圾堆积出来的，清理清不掉它。** 单符号边占比不降反升，也印证
剩下的纠缠更纯粹是「常量住错地方」而非真实业务依赖。

> 注：耦合审查记「函数内延迟导入 1,230 处」，本文工具记 1,818 处。两者口径不同（本文统计
> 所有位于函数体内的 import 节点，原文统计的是团内边），**不可直接相减**。除这一项外的
> 指标口径一致，可比。

两个 P0 缺陷在清理后依旧成立，已用运行期独立观察点复验（不是转述原文）：

```
app.api      vs domain.storyboard_ops : 123 个同名对象不是同一个（可调用 112）
app.api      vs domain.video_ops      : 130 个同名对象不是同一个（可调用 119）
app.worker   vs media_exec.run_job    :  92 个同名对象不是同一个（可调用  80）

LeaseLost / VideoPlanStaleFence / ReviewDependencyFence / VideoInputRepairRequired
    same=False  issubclass=False   ← except worker.LeaseLost 抓不住 run_job 抛的同名异常
```

`app/db.py` 仍在 import `completion_grant`、`model_migration`、`production.revision/
certificate/grant/shot_uid`、`delivery`、`artifacts`、`observability.tracing`，
而全仓 **82 个模块**依赖 `db.py`。最底层 import 最上层，这是 112 模块团的地基。

---

## 二、耦合审查没回答的问题：目标分层是什么

原方案是「减法清单」——把常量搬去 `contracts.py`、把修复搬去 `recovery.py`、把 `exec()`
换成 import。做完之后依赖图会更干净，但**没有任何东西定义「干净」的边界**，也没有任何
东西阻止下一个 PR 再加一条上行边。企业级架构的差别不在于某一次重构做得多彻底，而在于
**边界是被声明的、且是被自动强制的**。

### 2.1 声明式分层（六层，自底向上）

层号越小越底层。**合法依赖只能指向层号更小或同层的模块。**

| 层 | 名称 | 归属模块（按当前文件名） | 允许依赖 |
|---|---|---|---|
| L0 | 契约内核 | `contracts.py`（**待建**）、`schemas`、`errors`(去掉 db 依赖后) | 仅标准库 |
| L1 | 平台设施 | `config`、`atomic_io`、`db_schema`(**待建**)、`observability.tracing`、`auth`、`authz` | L0 |
| L2 | 持久化 | `db`（**瘦身后**）、`artifacts`、`evidence` | L0–L1 |
| L3 | 供应商适配 | `harness.model_gateway`、`hiagent`、`seedance`、`minimax_h3`、`video_providers` | L0–L2 |
| L4 | 领域能力 | `stages/*`、`portraits`、`validators`、`narrative`、`screenplay_ir`、`production/*`、`media_pipeline/*` | L0–L3 |
| L5 | 编排与入口 | `domain/*`、`media_exec/*`、`capabilities/*`、`orchestration/*`、`api`、`worker`、`main` | L0–L4 |

这份表不是凭空设计的，它**基本就是当前代码已经想成为的样子**——耦合审查已经测出
`app/schemas`（37 入度 / 0 出度）、`app/auth`、`app/authz`、`app/evidence`、
`app/atomic_io`、`app/harness.model_gateway` 已经是干净的叶子或真抽象。分层要做的是
把剩下那些「本该在 L2 却 import 了 L4」的边掰正，而不是重新发明结构。

### 2.2 防回潮：把分层变成可执行判据

**这是本文相对原方案的核心增量。** 光搬代码没用——本仓已经有过「上游事实变了、下游
为旧事实服务的校验没停」的教训（见 CLAUDE.md「Retiring Features」）。分层同理：不强制
就必然退化。

做法（不引框架，符合 CLAUDE.md「不得自动引入新框架或大型依赖」）：

1. 在 `app/` 下放一份 `LAYERS.toml`（纯数据，不是代码），声明每个包/模块的层号。
2. `scripts/arch_graph.py` 增加 `--check-layers`：读 `LAYERS.toml`，报出所有上行边，
   有上行边就非零退出。
3. 挂进 `scripts/verify.py --full`，作为发布前闸门。
4. **判据挂产物信号，不挂状态字段**（CLAUDE.md）：判据是「AST 解析出的实际 import 边是否
   上行」，从代码事实推导，不维护任何豁免白名单。确需临时豁免的，写进 `LAYERS.toml` 的
   `allowed_exceptions` 并**必须带失效日期与原因**，过期即红。

先量后守：现阶段直接开红会拦住所有工作（当前有大量上行边），因此按下面的顺序推进——
每完成一步就把该层收紧一格，红线只降不升。

---

## 三、实施顺序（P0 / P1 / P2）

耦合审查的五步方案在技术上是对的，本文只调整优先级排序依据，并补上每步的**分层收紧动作**。

### P0-1 拆掉 `exec()` 外观（最高危，先做）

理由是它是**唯一有正确性风险**的一项：四个围栏异常被复制成互不相识的两份类，
`except` 抓不住，会一路穿透到顶层当未知故障处理。目前测试全部走 `worker.` 前缀，所以
测试看不见这个缺口——**这是一个测试结构性看不见的活缺陷，不是理论问题。**

做法照耦合审查第 1 步执行。补两点：

- 主要工作量在**测试打桩目标迁移**（32 个文件用 `app.api.*`、30 个用 `app.worker.*`）。
  改成真模块后 `monkeypatch.setattr("app.api.f", ...)` 会**静默失效**——打桩不报错但不生效，
  这是最危险的失败形态。必须配套一条独立观察点：断言打桩后被调用的确实是替身。
- 完成判据：`app.api.X is app.domain.<chunk>.X` 对全部同名对象成立（现在 123/130 个为 False），
  且 `run_job.LeaseLost is worker.LeaseLost`。

分层收紧：L5 内部不再有 exec 注入，`api`/`worker` 成为纯聚合入口。

### P0-2 抽出契约常量内核（最便宜，可与 P0-1 并行）

新建 `app/contracts.py`（L0，零依赖）。收益直接可量化：**当前团内 649 条边里 369 条
（57%）只为了一个符号**，其中大量是契约版本号常量。这一步风险最低（搬的是常量与纯函数），
`scripts/verify.py` 即可覆盖。

完成判据：`scripts/arch_graph.py` 报出的团内单符号边数显著下降，且 `contracts` 出度为 0。

### P0-3 反转 `db.py`

照耦合审查第 2 步执行（DDL 注册表 + 修复动作搬去 `recovery.py`）。

**这一步触碰事务边界，是全仓最危险的改动**：CLAUDE.md 记载本仓已因隐式提交毁过三次真实
数据。硬性要求：搬移过程中不得给任何函数保留 `conn=None` 默认值；回滚必须是 except 块的
第一条语句，排在任何日志与 recorder 调用之前；验证要用**第二条独立连接**读盘。

完成判据：`db.py` 的 import 列表里不出现任何 L4/L5 模块。

### P1 切分巨模块

前三步做完再切，否则会被循环拽回去。当前最大的六个：

```
12,155 行  app.stages                     （至少 7 个关注点焊在一起）
10,821 行  app.portraits
 6,472 行  app.screenplay_scene_shards
 6,289 行  app.validators
 6,068 行  app.screenplay_ir
 5,422 行  app.production.screenplay_repair
```

切法照耦合审查第 4 步（`stages.py` 按已存在的连续区段切成 7 个文件，主要是移动而非重写）。
补充一条来自本次死代码清理的实证：`stages.py` 里 581 行的 `generate_screenplay` 子树是
**休眠管线**——切分前先确认区段是否还在现役调用路径上，休眠的直接删而不是搬。

`portraits.py` 与 `validators.py` 同时高入度又高出度，是把环焊死的两个节点，切分时优先
把它们的「被当基础库用」的部分下沉到 L4 下缘或 L0。

### P2 入口统一与前端收口

后端 224 个 REST 路由与 62 个命令总线 command 覆盖同一批操作，`bus.py` 的 docstring 还写着
未完成的迁移计划。前端两个问题：`api.ts` 的泛型动词逃生口（88 处调用点把 URL 知识漏回页面）、
`MonitorPage.tsx` 4,553 行 / 68 个 `useState` / 24 个本地重声明的后端响应类型（后端改字段
前端不会编译报错）。

前端目标结构：`api/client.ts`（`request` 与泛型动词，**内部可见**）+ `api/<域>.ts`（具名方法与
该域类型）。判据：`frontend/src` 里除 `api/` 之外对泛型动词的调用数归零。

### 明确不做

不引入依赖注入框架、事件总线、仓储层抽象。本仓的问题不是缺抽象，是常量放错地方 + 两个
`exec()` 外观 + 一个反向依赖的 `db.py`；加抽象层只会让 112 模块的团更难看清。

---

## 四、验证要求

- P0-1、P0-3 改的是共享底层原语，**必须跑全量测试**，不接受子集绿。本仓有过子集 586 passed、
  全量才炸出 7 个红的先例。
- 每步之后重跑 `scripts/arch_graph.py --json` 并与上一版 diff，**确认团规模单调下降**。
  这是唯一能证明「确实解耦了」的判据——闸门全绿不算证据。
- 取基线一律用干净 worktree（`git worktree add /tmp/baseline <commit>`），不得在共享工作区
  `git stash`。
- 已知既有红（与本方案无关，勿误判为回归）：
  `tests/test_capability_registry.py::test_mutating_endpoints_fully_classified`
  （画风功能新增 `POST /api/projects/{project_id}/bible/style` 未在能力注册表分类），
  以及 `tests/test_screenplay_scene_shards.py` 的 3 个 `asyncio.wait_for(timeout=1)` 用例。
