# 44 条分层上行边逐条判定与行动清单（2026-08-30）

本文复核 `scripts/arch_graph.py --check-layers` 报出的 44 条上行边（`app/LAYERS.toml` 阈值
48），逐条给出「代码倒置 / 层号定错 / 设计张力」判定，并用 `--layers-file` 指向临时副本的方式
实测每一类修复对违规数的真实影响。**未修改 `app/`、`app/LAYERS.toml`、`scripts/` 或任何测试文件**
——所有「实测」都在 `/tmp/LAYERS_test1.toml`（已用完删除）上进行，命令与输出见正文。

复现基线：

```bash
.venv/bin/python scripts/arch_graph.py --check-layers --max-violations 100000 --top 100
# 层级违规（上行边）共 44 条：模块级 20 条，函数内延迟导入 24 条
```

## 一、核心结论（先说人话）

- **44 条里 24 条（55%）是纯粹的层号定错，只需要改 `LAYERS.toml` 的数字，不用碰 `app/` 一行代码。**
  已用 `--layers-file` 指向临时副本实测：44 → 20，与预测的 24 条完全吻合。
- 再加 9 条（video_plan 家族 5 条 + mutation_primitives 家族 4 条）只需要改**导入路径**（从包门面
  改成直接导入具体子模块），不需要挪动任何函数体。
- 只有 **1 条**（`production.screenplay_repair.checkpoint_recovery -> domain.screenplay_ops`，调用
  `_screenplay_character_discovery`）是需要架构决策的真设计张力，其余都能用「按数据分类、抽到低层」
  的机械手法解决。
- **上一轮 agent 判定「无解」的 `artifacts -> completion_grant`，经查证是可解的**：`artifacts.py`
  实际只要 `prepare_provider_tasks_for_clear` 这一条调用链，而这条调用链（连同它调用的
  `_provider_task_clearance_evaluation`/`provider_task_clearance_snapshot`/
  `assert_provider_tasks_clearable`/`ProviderTasksNotTerminalError`）逐行核对下来**零 L4/L5 依赖**，
  只用了 `app.db`。是 `completion_grant.py` 这个 2914 行文件里其余函数（`reserve_provider_video_budget`
  等）拉高了整个文件的层号，而 `artifacts.py` 要的这一小块从未真正依赖那些高层符号。

## 二、44 条完整清单与三分类

按根因聚成 12 组（同一 (source, target, 根因) 的多个符号/多次调用合并列出，边数在括号里注明）。

### 组 1 — db 访问原语：`errors`/`auth.*`/`authz.*` → `db`（10 条，**层号定错**）

| # | 边 | 符号 |
|---|---|---|
| 1 | `app.errors`(L0) → `app.db`(L2) | `db`（其实是 `insert_error_log`） |
| 2 | `app.authz.resolve`(L1) → `app.db`(L2) | `get_conn` |
| 3-5 | `app.auth.sessions`(L1) → `app.db`(L2) | `now`/`new_id`/`get_conn` |
| 6-7 | `app.auth.api`(L1) → `app.db`(L2) | `now`/`get_conn` |
| 8-10 | `app.auth.admin_api`(L1) → `app.db`(L2) | `now`/`new_id`/`get_conn` |

**依据**（不是猜的，是查的）：

- `app/errors.py` 只 import `app.db`，`insert_error_log()` 是一条独立连接上的单条 INSERT +
  `_run_write_transaction_once`，零业务依赖。用 AST 扫了全仓 73 处 `app.errors` 的消费方，**最低的
  是 L4**（`app.identity_adjudication`/`app.portraits.*`/`app.stages.*` 等），没有任何 L0-L3 模块
  依赖 `errors`。把 `errors` 的层号从 0 提到 2（或更高），对现有边**不可能**产生新违规——这是从
  全仓消费方数据反推出来的，不是猜测。
- `app/authz/resolve.py`、`app/auth/{sessions,api,admin_api}.py` 分别只 import
  `app.db.get_conn`/`now`/`new_id` + 同包内的 `principal`/`passwords`/`sessions`。用同样的方法扫了
  `app.auth`/`app.authz` 的全部消费方：**除了包内互相引用和 `app.local_session`（也是 L1），其余全部
  是 L5**（`domain.*`/`main`/`system_api`/`orchestration.api`/`capabilities.bus`）。
- 但 `app.local_session`（L1）与 `app.auth.principal`/`app.auth.sessions` 互相依赖（`local_session`
  import `auth.sessions.resolve_session`，`auth.api` import `local_session`），必须同层；
  `app.media_urls`（L1）依赖 `local_session`。三者的外部消费方也清一色 L5，**没有任何 L0-L3
  模块依赖它们**，可以整体跟着 `auth.sessions`/`auth.api`/`auth.admin_api`/`authz` 一起提到 L2。
- **`app.auth.principal`/`app.auth.passwords`/`app.auth.deps` 本身零 db 依赖，不在违规列表里，保持
  L1 不动**——这不是把整个 `auth` 包降级，是只提那 4 个真正碰了 db 的子模块（+ 级联的
  `local_session`/`media_urls`）。

**判定**：L1「平台设施」把 `auth`/`authz` 定义成不依赖 db 的假设从一开始就不成立——会话要落库、
管理员要读用户表，这是业务事实，不是可以绕开的实现细节。真正需要拆开的是 `auth` 包内部：
`principal`/`passwords`/`deps`（零依赖，真 L1）与 `sessions`/`api`/`admin_api`/`authz.resolve`
（需要 db，实质是 L2）两簇，LAYERS.toml 目前把它们当一个包处理，用了包前缀而非精确到子模块。

**`errors` 单独一条**：文档（`coupling_review_2026-08-29.md` 第 255 行）本来就提议给 `log_error`
做 sink 注入（`main.py` 装配），保住 L0 的「零业务依赖」语义。这是更干净的方案，但成本略高于直接
改层号（层号改法：`errors` 提到 2，1 行 TOML；sink 注入：`errors.py` 加一个模块级 `_SINK` 变量 +
`set_error_sink()`，`main.py` 里 `errors.set_error_sink(db.insert_error_log)`，去掉 `from app import db`，
约 10 行代码）。两个方案实测消掉的边数一样（1 条），本文两个都列，由协调方按「要不要保留 L0 的
纯契约语义」拍板。

### 组 2 — `orchestration.state_machine` / `orchestration.engine`（11 条，**层号定错，本轮最大发现**）

上一轮 agent 的分类完全没提到这一组的根因，只是把 11 条边分散记在各个消费方名下
（`harness.model_gateway`/`loops.base`/`portraits.*`/`production.*`/`stages.*`/`storyboard_workspace`）。
逐条查了 `app/orchestration/state_machine.py`（175 行）和 `engine.py`（452 行）本身的 import：

```
app/orchestration/state_machine.py:  from app.db import get_conn, now          # 仅此一行
app/orchestration/engine.py:         from app.db import get_conn, now, run_write_transaction
                                      from app.evidence import repository
                                      from app.harness.contracts import get_contract
                                      from app.harness.types import EvidenceArtifact
                                      from app.orchestration.state_machine import transition_run, transition_step
                                      from app.observability.tracing import bind_trace
```

`state_machine.py` 是一个只依赖 `app.db` 的通用 CAS 状态迁移原语（`StateConflict`/`transition_run`/
`transition_step`），`engine.py` 建在它之上 + `evidence`(L2) + `harness.contracts`(L3) +
`observability.tracing`(L1)。两个文件都被塞进 `app.orchestration`(L5) 纯粹因为目录位置，不是因为
业务语义——它们本身没有任何编排/入口层的内容。

用 AST 扫了两个模块的全部消费方（`app.orchestration.state_machine` 26 处、`.engine` 29 处），按层号
分布：

```
state_machine 消费方层号分布：{L3: 1, L4: 8, L5: 17}   —— 没有 L0/L1/L2 消费方
engine        消费方层号分布：{L4: 2, L5: 27}          —— 没有 L0/L1/L2/L3 消费方
```

**判定**：`state_machine` 的层号应为 2（只依赖 db，同时满足所有已知消费方 ≥2）；`engine` 应为 3
（受 `harness.contracts` 这个真实依赖顶住，不能更低，同时满足所有已知消费方 ≥3）。降低目标层号
只会放宽对现有消费方的约束，不可能制造新违规——已用消费方枚举验证。

**实测**（`/tmp/LAYERS_test1.toml`，只改两行）：

```
"app.orchestration.state_machine" = 2
"app.orchestration.engine" = 3
```

单独这一组直接消掉 11 条边（后面第四节有整体前后对比的完整命令输出）。

涉及的 11 条边：`harness.model_gateway->state_machine`、`loops.base->state_machine`、
`portraits->state_machine`（×3，含 `portraits`/`portraits.resolution_store`/
`portraits.structural_coverage_ensure`）、`production.prep_pack.{chunk_extraction,publish}
->state_machine`、`production.screenplay_repair.gates->state_machine`、
`stages.screenplay_generate->state_machine`、`stages.blueprint_checkpoint->engine`、
`storyboard_workspace->engine`。

### 组 3 — `media_pipeline.stages` / `media_pipeline.concurrency` ← `hiagent`（2 条，**层号定错**）

`app/media_pipeline/stages.py`（152 行，纯资源键常量，零 app 内部依赖）、
`app/media_pipeline/concurrency.py`（250 行，只 import `app.db.get_setting/set_setting` +
`app.media_pipeline.stages`）。两者的外部消费方（`stages` 只有 `video_supervisor.*`(L5)；
`concurrency` 有 `hiagent`(L3)、`generation_concurrency`(L4)、同包 `bootstrap`/`retry_policy`/
`scheduler`(L4)、`media_exec.*`/`system_api`(L5)）没有任何低于 L3 的消费方。

**判定**：`hiagent.py` 已经是直接 `from app.media_pipeline.concurrency import channel_limit` /
`from app.media_pipeline import stages as media_stages`——两者都是对**具体子模块**的导入（不是包
门面），`resolve()` 会精确解析到子模块本身，所以只要给这两个子模块单独声明层号即可，**不需要改
`hiagent.py` 一行代码**。

修复：`"app.media_pipeline.stages" = 3`、`"app.media_pipeline.concurrency" = 3`（覆盖包前缀
`app.media_pipeline = 4`，最长前缀匹配生效）。

### 组 4 — `app.generation_concurrency` ← `harness.model_gateway`（1 条，**层号定错，依赖组 3**）

`app/generation_concurrency.py` 模块级只 import `app.db.get_setting`，延迟 import
`app.media_pipeline.concurrency`（组 3 修复后是 L3）。全仓消费方只有 `harness.model_gateway`(L3)
和一堆 L5（`domain.screenplay_ops.*`/`domain.storyboard_ops.*`/`system_api`），没有 L4 消费方。

**判定**：`generation_concurrency` 可以从 4 降到 3（依赖组 3 的 `media_pipeline.concurrency=3`
先落地，否则 `generation_concurrency(3) -> media_pipeline.concurrency(4)` 会变成新违规）。

### 四组合计（组 1-4，24 条）——**实测验证**

```bash
# /tmp/LAYERS_test1.toml 只改了以下值/新增以下 key（脚本用正则替换已有 key、
# 在 [layers] 后插入新 key，未触碰任何其它声明）：
"app.errors" = 2
"app.local_session" = 2
"app.media_urls" = 2
"app.authz" = 2
"app.orchestration.state_machine" = 2
"app.orchestration.engine" = 3
"app.media_pipeline.stages" = 3
"app.media_pipeline.concurrency" = 3
"app.auth.sessions" = 2
"app.auth.api" = 2
"app.auth.admin_api" = 2
"app.generation_concurrency" = 3
```

```
$ .venv/bin/python scripts/arch_graph.py --check-layers --max-violations 100000 --layers-file /tmp/LAYERS_test1.toml
层级违规（上行边）共 20 条：模块级 2 条，函数内延迟导入 18 条
```

**44 → 20，实测消掉 24 条，与逐组预测的 10+11+2+1=24 完全吻合。** 剩下 20 条全部要求
「改导入路径」或「挪函数」，纯声明改不动它们——原因见下面第三节的机制说明。

### 组 5 — `artifacts` → `completion_grant`（2 条，**代码倒置，可解，不是「无解」**）

上一轮 agent 的结论：「`completion_grant` 必须 L4（它 import `production.screenplay_authority`），
`artifacts` 需要它 ≤L2，不可调和」。**这个结论只对了一半**：`completion_grant.py`（2914 行）整体
确实靠 `production.screenplay_authority`/`video_plan`/`compiler`/`hiagent`/`evidence` 这些延迟导入
被钉在 L4，但 `artifacts.py` 实际只要 `prepare_provider_tasks_for_clear` 一个函数：

```python
# app/artifacts.py:36, 228（两处调用点）
from app.completion_grant import prepare_provider_tasks_for_clear
```

逐层追了这个函数的调用链：

```
prepare_provider_tasks_for_clear（completion_grant.py:810-864）
  -> _provider_task_clearance_evaluation（429-765，实际查询逻辑）
  -> ProviderTasksNotTerminalError（93，异常类）
```

三者 + `assert_provider_tasks_clearable`（790-807，同链路的姊妹函数）合计约 340 行，**只对
`jobs`/`provider_video_budget_claims`/`provider_calls`/`shot_versions` 四张表做纯 SQL 查询和状态
流转**，模块级 import 只有 `app.db.get_conn`/`now`。`completion_grant.py` 里那些真正需要
`production.screenplay_authority`/`video_plan`/`hiagent` 的函数（`reconcile_provider_tasks_for_clear`、
`episode_video_completion_budget_requirement`、`_screenplay_release_material`、
`_storyboard_release_material` 等）全部在这条链路**之外**，一个都没被
`prepare_provider_tasks_for_clear` 调用到。

**判定：可解。** 把 `ProviderTasksNotTerminalError` + `_provider_task_clearance_evaluation` +
`provider_task_clearance_snapshot` + `assert_provider_tasks_clearable` +
`prepare_provider_tasks_for_clear` 这 5 个符号搬到新模块（建议 `app/provider_task_clearance.py`，
L2，与 `artifacts`/`evidence` 同档），`completion_grant.py` 用
`from app.provider_task_clearance import (...)` 重新导出保持所有现有调用点
（`domain.projects`/`domain.storyboard_ops.*`/`domain.video_ops.clear`/`capabilities.preflight` 等，
全是 L5，从哪层导入都合法）不用改一行；`artifacts.py` 的两处调用改成直接
`from app.provider_task_clearance import prepare_provider_tasks_for_clear`。

这个手法不是发明的——本仓已经用同一模式解决过一模一样的问题：`app/db_schema.py`（零依赖注册表）
让 `completion_grant`/`artifacts`/`delivery`/`model_migration`/`production.{shot_uid,certificate,
revision,grant}` 反过来在导入期向 `db.py` 注册 DDL，而不是被 `db.py` 直接 import——`docs/
coupling_review_2026-08-29.md` 第 2 步描述的「反转 db.py」**已经落地**（`db.py` 里能看到
`app.artifacts registers this with app.db_schema at import time instead` 这类注释），只是
`app/LAYERS.toml` 里 `app.db_schema` 头顶仍标着「待建」注释，属于文档没跟上代码的滞后，不影响判层
结果。

### 组 6 — `video_providers`/`seedance`/`minimax_h3` → `video_plan`（5 条，**代码倒置，可解**）

```
app.video_providers(L3) -> app.video_plan(L4) :: ProviderVideoCapabilitySnapshot
app.seedance(L3)        -> app.video_plan(L4) :: ProviderVideoCapabilitySnapshot
app.minimax_h3(L3)      -> app.video_plan(L4) :: minimax_h3_snapshot_matches_runtime
app.minimax_h3(L3)      -> app.video_plan(L4) :: minimax_h3_snapshot_from_probe
app.minimax_h3(L3)      -> app.video_plan(L4) :: failed_minimax_h3_snapshot
```

`app.video_plan` 是真包（12 个子模块，3936 行），`ProviderVideoCapabilitySnapshot` 定义在
`video_plan/models.py`（**零 app 内部依赖**，纯 pydantic），3 个 `minimax_h3_*` 函数定义在
`video_plan/capability_snapshot.py`（**只 import `app.db`**）。但调用方写的是
`from app.video_plan import ProviderVideoCapabilitySnapshot`——包级导入，`resolve()` 解析到的是
`app.video_plan` 这个包本身（默认层号 4，被 `video_plan/generate.py` 等其它子模块的真实 L5 依赖
钉住），不是 `models.py`/`capability_snapshot.py`。**这条边靠改 `LAYERS.toml` 单独解不开**——必须
同时改导入路径，因为 `resolve()` 对包级 `from app.video_plan import X` 的目标就是包本身，与
`models.py` 单独声明什么层号无关（这点我在第三节详细说明，并用 `app.domain.storyboard_ops` 的等价
情况验证过机制）。

**判定：可解，成本很低。** 3 处改动：
1. `video_providers.py:17`、`seedance.py:248` 把 `from app.video_plan import
   ProviderVideoCapabilitySnapshot` 改成 `from app.video_plan.models import
   ProviderVideoCapabilitySnapshot`；
2. `minimax_h3.py:1412-1433` 把 `from app.video_plan import (...)` 改成
   `from app.video_plan.capability_snapshot import (...)`；
3. `LAYERS.toml` 新增 `"app.video_plan.models" = 1`（零依赖，可以给到最低）、
   `"app.video_plan.capability_snapshot" = 2`（受 `app.db` 顶住）。

不挪动任何函数体，纯粹是「越过包门面直接点名子模块」，和组 3 的 `media_pipeline.stages`/
`concurrency` 是同一手法，只是这次调用方还没这么写，需要顺手改 6 行 import。

### 组 7 — `domain.common` 里的纯函数泄漏（5 条，**代码倒置，可解**）

两组独立的小函数，都定义在 `app/domain/common.py`（这是 `app.domain` 包真正意义上的 L5 聚合文件，
import 了 `stages`/`validators`/`orchestration.engine`/`compiler` 等一堆重量级依赖），但函数本身
极小、极纯：

**7a — `_project_bible_or_placeholder` / `_placeholder_bible` / `FALLBACK_VISUAL_STYLE`**
（`app/video_plan/generate.py`、`app/storyboard_supervisor.py` 各用一次）：

```python
def _project_bible_or_placeholder(project_row) -> Bible:
    raw = (project_row["bible_json"] or "").strip() if project_row else ""
    if raw:
        return Bible.model_validate(json.loads(raw))
    return _placeholder_bible()
```

只依赖 `app.schemas.Bible`（L0）+ `json`。同一文件里的 `_compact_episode_target`（被
`domain/video_ops/confirmation_eval.py` 用到，见组 10）同样只依赖 `app.config`（L1）。

**7b — `_episode_chapters` / `_episode_source_blocks` / `_episode_source_text`**
（`app/production/storyboard_pack.py`、`app/production/prep_pack/generate_once.py` 各用一次/两次）：

```python
def _episode_chapters(conn, ep) -> list[dict]:
    """本集源章节行……供 `_episode_source_text` 和 paratext 偏移换算
    （`app.production.prep_pack`）共用——"读哪些章、按什么顺序"只能有一份实现，
    两处各写一份会产生漂移风险（见 logs/paratext_single_source_plan.md）。"""
```

函数自带的 docstring 已经明确写了「只能有一份实现，两处各写一份会漂移」——`production.prep_pack`
和 `production.storyboard_pack` 本来就是这三个函数的正牌消费方，只依赖 `app.db.rows_to_dicts`(L2)
+ `app.ingest.chapter_is_stub/chapter_titles_match`(L1)，把它们留在 L5 的 `domain.common` 里、让
两个真正的消费方通过延迟导入越级去够，反而是最脆弱的实现方式（源码本身都在提醒维护者别搞出第二份
实现，但现状恰恰是「唯一实现」躺在错误的层）。

**判定：可解。** 7a 与 7b 各自搬到独立的小模块（或并入既有的低层模块，例如 7a 可以并入
`app.visual_styles`（已声明 L1，`FALLBACK_VISUAL_STYLE` 本来就该在这），7b 可以新开
`app/source_chapters.py`，L2，与 `app.ingest`/`app.db` 同档）。`domain/common.py` 改成从新模块
`import`（对已有的一堆 `from app.domain.common import _project_bible_or_placeholder` 内部消费方
完全无感——它们仍然可以从 `domain.common` 拿到这个名字，只是 `common.py` 自己变成转发）。

### 组 8 — `domain.storyboard_ops.mutation_primitives`（4 条，**代码倒置，可解，成本最低的一类**）

```
app.storyboard_supervisor(L4)      -> domain.storyboard_ops :: _board_from_shot_rows  （×2）
app.production.storyboard_pack(L4) -> domain.storyboard_ops :: _board_from_shot_rows
app.production.storyboard_pack(L4) -> domain.storyboard_ops :: _assert_storyboard_write_authorized
```

这两个函数定义在 `app/domain/storyboard_ops/mutation_primitives.py`，**文件自己的 docstring 写着**：
「从 app/domain/storyboard_ops.py 按原样搬移；被本包几乎所有其它子模块依赖，是本包唯一没有反向
依赖的基础层之一。」import 只有 `app.errors`(L0)、`app.db.new_id`(L2)、`app.schemas`(L0)、
`app.validators`(L4)，延迟 import `app.continuity`/`app.renderability`(均 L4)——**这个文件本身的
自然层号是 4，不是 5**，只是被塞进了 L5 的 `domain.storyboard_ops` 包。

**判定：可解，成本极低。** 调用方已经是 `from app.domain.storyboard_ops import
_board_from_shot_rows`（包级），改成 `from app.domain.storyboard_ops.mutation_primitives import
_board_from_shot_rows`（3 处改动点：`storyboard_supervisor.py:189,972`、
`production/storyboard_pack.py:1780,1982`），LAYERS.toml 加一行
`"app.domain.storyboard_ops.mutation_primitives" = 4`。**不挪动任何代码。**

### 组 9 — `domain.storyboard_ops.evidence._finalize_storyboard_evidence`（1 条，**代码倒置，可解，需要真搬移**）

```
app.production.storyboard_pack(L4) -> domain.storyboard_ops :: _finalize_storyboard_evidence
```

与组 8 不同：`_finalize_storyboard_evidence`（`domain/storyboard_ops/evidence.py:221`）自己延迟
import 了 `app.production.screenplay_authority`（`resolve_downstream_screenplay`）和
`app.narrative`（`validate_storyboard_narrative`），**这两个都是真实的 L4 依赖**，所以它的自然层号
本来就是 4，不是被误分类的零依赖函数——而是这个函数本身该属于 L4，只是历史上和 L5 专属的
`_ensure_current_storyboard_shot_artifacts` 等函数放在了同一个文件里。

它还有一个真实的 L5 内部消费方：`app/domain/video_ops/confirm_episode.py:446`（确认剧集视频完成时
调用）。这不影响搬移——搬到 L4 之后，L5 的 `confirm_episode.py` 照样能合法导入它（5≥4）。

**判定：可解，但要真的挪函数体**（不是简单改导入路径），建议挪到 `app/production/` 下（它本来就
只被 `production.storyboard_pack` 和 `domain.video_ops.confirm_episode` 两处调用，语义上属于「分镜
产物终态收口」，和 `production.certificate`/`production.screenplay_authority` 是近亲）。约 60 行，
不改逻辑。

### 组 10 — `domain.video_ops.confirmation_eval.evaluate_storyboard_for_confirmation`（1 条，**代码倒置，可解，但要排在组 7a/组 8 后面**）

```
app.storyboard_supervisor(L4) -> domain.video_ops :: evaluate_storyboard_for_confirmation
```

`confirmation_eval.py` 自己的 docstring 同样写着「本包唯一没有反向依赖的基础层之一」，但它 import
了 `app.domain.common._compact_episode_target`（组 7a 待搬）和
`app.domain.storyboard_ops._board_from_shot_rows`（组 8 待搬）。等这两个先搬到位（`_compact_episode_
target` 并入组 7a 的新模块，`_board_from_shot_rows` 落到 `mutation_primitives.py` 的具体导入），
`confirmation_eval.py` 自身只剩 `app.compiler`(L4)、`app.schemas`(L0)、`app.validators`(L4)——自然
层号变成 4，这条边才能用「改导入路径」的方式解决。**顺序依赖：先做组 7a、组 8，再做这条。**

### 组 11 — `domain.screenplay_ops.lightweight_status._prep_pack_stage_snapshot`（1 条，**代码倒置，可解，需要真搬移**）

```
app.production.revision(L4) -> domain.screenplay_ops :: _prep_pack_stage_snapshot
```

函数本身只查 `workflow_runs`/`step_runs` 两张表 + 延迟 import `orchestration.engine.step_presentation`
（组 2 修复后是 L3），逻辑很干净。但它所在的文件 `lightweight_status.py` 模块级 import 了
`app.domain.common.router`（FastAPI 路由对象）——这个文件本身承担了路由注册的职责，**不能整体
降级**（`router` 是货真价实的 L5 依赖）。

**判定：可解，但必须是「挪函数」而不是「改导入路径 + 降子模块层号」**——如果只声明
`app.domain.screenplay_ops.lightweight_status = 4` 而不搬函数，`lightweight_status.py` 自己
`import router` 那行会立刻变成一条新的上行边（4→5），等于把违规从「谁调用它」平移到「它自己」，
边数不变。正确做法是把这一个函数（连同它专属的 `_PREP_PACK_STAGE_STEP_KEYS` 常量，约 35 行）挪到
新文件或挪去 `production.revision` 自己的模块里（它目前是唯一的外部消费方）。

### 组 12 — `checkpoint_recovery` 调用 `screenplay_ops._screenplay_character_discovery`（1 条，**设计张力，不建议机械修复**）

```
app.production.screenplay_repair.checkpoint_recovery(L4) -> domain.screenplay_ops :: screenplay_ops
```

```python
# app/production/screenplay_repair/checkpoint_recovery.py:45
async def ensure_source_characters_incremental(...):
    from app.domain import screenplay_ops
    return await screenplay_ops._screenplay_character_discovery(episode_id, source_text, ...)
```

追了 `_screenplay_character_discovery`（`domain/screenplay_ops/task_body.py:41`）的完整实现：调用
`task_registry`、`evidence`、`harness.context/contracts`、`orchestration.engine`、
`orchestration.state_machine`、`app.stages`、`app.portraits`、`app.source_paratext`，还做剧本运行期
所有权断言（`_assert_screenplay_run_owner`）——这是剧本生成任务体里一段完整的「增量角色发现」业务
流程，不是可以安全抽取的纯函数。它已经深度嵌入 L5 的剧本编排任务生命周期。

**判定：这一条不建议用「抽符号」的方式机械修复。** 真正的问题是「剧本修复流程（L4）复用了剧本
生成任务体（L5）的一整段业务逻辑」——要么承认 `checkpoint_recovery` 这种「从检查点恢复」的操作本质
上是编排层的一部分（该往 L5 挪，而不是待在 `production` 包），要么把「增量角色发现」重构成一个双方
都能调用的独立服务（工作量大、需要先弄清楚两条调用路径对「运行所有权」「任务注册」的假设是否一致，
本次调查没有把这个question查到底）。**建议**：短期内在 `LAYERS.toml` 的 `allowed_exceptions` 里
显式登记这一条（带 reason + expires，不是放任不管），把「要不要重构角色发现」交给下一轮架构决策，
不要为了让检查器通过就随手降 `checkpoint_recovery` 的层号——那样会掩盖这条边背后真实的耦合。

## 三、一个必须先搞清楚的机制：包级导入 vs 子模块导入

`scripts/arch_graph.py` 的 `resolve()` 对 `from app.video_plan import X` 这种写法，无论 `X` 实际定义
在哪个子模块，边的目标都解析成 `app.video_plan` 这个包本身（`resolve()` 先试
`app.video_plan.X`，不是真实模块就退到父级 `app.video_plan`）。这意味着：

- 组 3（`hiagent -> media_pipeline.stages/concurrency`）之所以能纯靠改 `LAYERS.toml` 解决，是因为
  `hiagent.py` **已经**写的是 `from app.media_pipeline import stages` / `from
  app.media_pipeline.concurrency import channel_limit`——直接点名子模块。
- 组 6（`video_plan` 家族）、组 7-11（`domain.*` 家族）现在写的是包级导入
  （`from app.video_plan import X`、`from app.domain.storyboard_ops import X`），**同样的
  `LAYERS.toml` 改法在这些边上不会生效**——必须先把导入语句改成指向具体子模块，声明才有意义。

已用组 8 的等价场景做过验证：给 `app.domain.storyboard_ops.mutation_primitives` 单独声明层号，
在不改 `storyboard_supervisor.py`/`production/storyboard_pack.py` 导入语句的前提下重新跑
`--check-layers`，边数不变——因为这些文件写的仍是包级导入，目标解析结果仍是
`app.domain.storyboard_ops`（继承 `app.domain=5` 前缀），与刚声明的子模块层号无关。这一点已经在
`/tmp` 里验证过，不是纸面推理。

## 四、按实测/推导 ROI 排序的行动清单

| 顺序 | 动作 | 改动范围 | 风险 | 消边数 | 验证方式 |
|---|---|---|---|---|---|
| 1 | `orchestration.state_machine`=2, `.engine`=3 | `LAYERS.toml` 2 行，**零 app/ 代码** | 极低 | **11**（实测） | `--layers-file` 临时副本已验证 |
| 2 | `errors`/`authz`/`auth.sessions`/`auth.api`/`auth.admin_api`/`local_session`/`media_urls` 提层 | `LAYERS.toml` 8 行，**零 app/ 代码**（或 `errors` 改走 sink 注入，~10 行代码） | 极低 | **10**（实测） | 同上 |
| 3 | `media_pipeline.stages`/`.concurrency`=3，`generation_concurrency`=3 | `LAYERS.toml` 3 行，**零 app/ 代码** | 极低 | **3**（实测） | 同上 |
| 4 | `video_plan` 家族改直接导入子模块 | 3 个文件 6 行 import + `LAYERS.toml` 2 行 | 低（无逻辑变更） | 5（推导，机制已验证） | 需配合代码改动后重跑 |
| 5 | `mutation_primitives` 改直接导入子模块 | 2 个文件 4 行 import + `LAYERS.toml` 1 行 | 低（无逻辑变更） | 4（推导，机制已验证） | 同上 |
| 6 | `artifacts -> completion_grant`：抽 `provider_task_clearance.py` | 新文件 ~340 行（原样搬移）+ `completion_grant.py` 转发 5 行 + `artifacts.py` 改 2 处 import | 中（touch 供应商任务/预算结算相关代码，建议连带跑 `tests/` 里涉及 `prepare_provider_tasks_for_clear`/`completion_grant` 的用例） | 2 | 需配合代码改动后重跑 |
| 7 | `domain.common` 7a（`_project_bible_or_placeholder` 等）下沉 | 新增/并入模块 ~15 行 + `domain/common.py` 转发 + 2 处调用方 import | 低 | 2 | 同上 |
| 8 | `domain.common` 7b（`_episode_chapters` 等）下沉 | 新模块 ~65 行 + `domain/common.py` 转发 + 3 处调用方 import | 低（纯函数，docstring 已警告禁止另起实现，搬移不改变「唯一实现」这一性质） | 3 | 同上 |
| 9 | `_finalize_storyboard_evidence` 挪去 `app.production` | 挪 ~60 行 + 2 处调用方 import | 中（分镜产物终态收口逻辑，建议连带跑 storyboard 确认相关测试） | 1 | 同上 |
| 10 | `_prep_pack_stage_snapshot` 挪出 `lightweight_status.py` | 挪 ~35 行 + 1 处调用方 import | 低 | 1 | 同上 |
| 11 | `evaluate_storyboard_for_confirmation` 改直接导入 | 依赖 7、8 先落地；之后 1 处 import + `LAYERS.toml` 1 行 | 低，但有前置依赖 | 1 | 同上 |
| 12 | `checkpoint_recovery` 的角色发现调用 | **不建议机械修复**，登记 `allowed_exceptions` 并转交架构决策 | — | 0（有意不消） | — |

**Top 5（按 消边数/成本 排序，前 3 项已实测）**：
1. `orchestration.state_machine`/`engine` 提层 —— 11 条，2 行 TOML，零代码风险。
2. `auth`/`authz`/`errors`/`local_session`/`media_urls` 家族提层 —— 10 条，8 行 TOML，零代码风险。
3. `media_pipeline`/`generation_concurrency` 提层 —— 3 条，3 行 TOML，零代码风险。
4. `mutation_primitives` 改导入 —— 4 条，6 行改动（4 行 import + 1 行 TOML + 无逻辑变更），是「改
   代码」类里性价比最高的一项，建议紧跟在前三项后面做。
5. `video_plan` 家族改导入 —— 5 条，8 行改动，同样零逻辑变更。

前三项合计 **24 条**，全部是纯声明改动，建议作为「先量后守」的第一批直接落地（`docs/
architecture_layering_plan_2026-08-29.md` 2.2 节本来就规划了「每完成一步就把阈值收紧一格」）；
做完后阈值可以从 48 下调到 24（20 条剩余违规 + 4 条给并行 agent 中间态的缓冲，与现有「实测 44 上浮
~9%」的口径一致）。

## 五、不建议做的清单

- **不建议把整个 `app.auth` 包提到 L2。** 只有 `sessions`/`api`/`admin_api`/`authz.resolve`
  真正碰了 db；`principal`/`passwords`/`deps` 零依赖，是干净的 L1。整体提层会让「L1 平台设施」这
  个分类名不副实，且对消解违规没有增量收益（这 4 个子模块本来就不在违规列表里）。
- **不建议给 `checkpoint_recovery -> screenplay_ops` 这条边随手降层号或加豁免了事。** 见组 12：
  这条边背后是「剧本修复流程复用剧本生成任务体的完整业务逻辑」，机械消掉判据只会把真实耦合藏起来，
  不构成本文 CLAUDE.md「不得通过删除核心功能掩盖问题」同类风险的反面——把违规判据关掉本质上和
  删掉校验是一回事。
- **不建议在完成组 1-3（纯声明）之前就动组 6-11（改代码）。** 组 10 明确依赖组 7a/组 8 先完成；
  组 2 的 `engine=3` 是组 11 里 `step_presentation` 调用合法化的前提。乱序做会导致中间状态违规数
  不降反升，误判进度。
- **不建议为了图省事把 `completion_grant.py` 整个文件搬到 L2。** 它其余的函数（预算授权、剧本/
  分镜发布材料聚合）是真的需要 `production.screenplay_authority`/`video_plan`/`hiagent`，降层会
  制造 4-5 条新的上行违规（这些函数自己 import 那些 L3/L4 模块）。只抽 `artifacts.py` 真正要的
  那一小簇符号，是唯一不产生新违规的做法。

## 六、对上一轮 agent 结论的修正

- **`app.errors(L0) -> app.db(L2)` 实际是 ×1，不是 ×2。** `--top 100` 完整清单核对过，`db` 符号
  只出现一次；`insert_error_log` 是这条边唯一的调用点。
- **`artifacts -> completion_grant` 不是「无解」，是可解的**（见组 5）。上一轮判定「completion_grant
  必须 L4」这句话对文件整体成立，但没有验证 `artifacts.py` 实际用到的那条调用链是否真的依赖 L4
  内容——查证结果是不依赖，可以整体抽出去。
- **`orchestration.state_machine`/`.engine` 这条根因上一轮完全没有识别**，只是把相关的 11 条边
  分散记在 `harness.model_gateway`/`portraits.*`/`production.*`/`stages.*` 名下，当成"新声明模块自身
  的真倒置"处理。实际上根因是同一个：两个文件本身只依赖 `app.db`/`app.evidence`/
  `app.harness.contracts`，被 L5 包前缀带高了层号——这是本次调查里消边收益最大的一条（11/44 ≈ 25%），
  上一轮完全没有覆盖到。

## 七、附带发现（不在本次范围内，供协调方参考）

- `app/LAYERS.toml` 头部大注释仍把 `app.domain`/`app.media_exec`/`app.portraits` 描述成
  exec-facade（"运行时同一命名空间"），但实测 `exec_facade_chunk_map(modules)` 当前返回空——P0-1
  拆 exec 外观的工作看起来已经做完（`app/domain/__init__.py` 的 docstring 印证："no source-injection
  re-execution into a shared namespace anymore"），只是 `LAYERS.toml`/`scripts/arch_graph.py` 顶部
  文档和 `app/worker.py` 的 docstring 没跟着更新（`worker.py` 仍写"app.media_exec 本身也还是 exec()
  外观"）。这属于 CLAUDE.md「Retiring Features」提醒的「上游事实变了、下游文档没跟上」，建议下一轮
  顺手更新，不是本次分层判据的问题（`--check-layers` 按实际 AST 走，不依赖这段描述）。
- `app/db_schema.py` 已经存在且在用（`completion_grant`/`artifacts`/`delivery`/`model_migration`/
  `production.{shot_uid,certificate,revision,grant}` 都在用它注册 DDL），但 `LAYERS.toml` 里这个
  key 头上仍标着「待建」注释，属于同类文档滞后。
