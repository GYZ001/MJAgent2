# 连播任务台改造契约（2026-09-02 冻结）

## 背景与问题

现状（commit a59739c 之前）：连播台是**项目级单例**——一个项目同时只有一条
`workflow_runs(workflow_type='series_film', scope_type='project')`，整棵进度树塞在
`config_snapshot_json.series_state` 里，页面只有「选起止集 → 开始 / 暂停 / 继续」一条
逻辑。用户的真实用法是：一部 1600 章的小说按每 10 章切成一个**大集**（= 10 集），
一共 160 个大集，每个大集是一个可独立触发、独立查看、独立出片的任务；要能勾选批量
串行执行，要能勾选已完成的打包导出。单例模型装不下 160 个任务，所以换数据载体。

## 命名（对外文案一律中文，内部标识保持英文）

- **连播任务**（`series_task`）：一个连续集区间 `[episode_from, episode_to]`，
  产出一部连播成片（`projects/{pid}/series/ep{from}-ep{to}/film.mp4`，路径不变）。
- **任务队列**（queue）：项目级串行队列，**同一项目一次只跑一个任务**（沿用既有
  `task_registry.spawn('series_film', project_id, ...)` 的单活约束）。
- 「大集」是用户口径，界面上说「连播任务」，一行一个任务。

## P0（本次实现）

1. `series_tasks` / `series_queue_state` 两张表 + 迁移。
2. 按 `group_size`（默认 10，范围 1–10）切分整个项目的集号，**补齐式**生成任务清单。
3. 任务列表（分页）、任务详情、删除未开始任务。
4. 串行队列：入队单个/多个任务、出队/取消、队列暂停/继续、进程重启后恢复。
5. 单任务链路复用既有五台（映射/分镜/确认/生成/成片）+ ffmpeg 合并，判据与
   fail-closed 语义完全不变。
6. 打包导出：勾选已完成任务 → 生成导出包（硬链接 + manifest + 下载清单）。
7. 前端重做：任务列表页 + 任务详情页（路由 `/projects/{pid}/series/{taskId}`）。

## P1（本次不做，留接口余地）

- 任务改名、手工自定义区间新建任务（后端 `POST /series-tasks` 已支持 `ranges`
  形态，前端 P0 只暴露 `group_size` 切分）。
- 导出包打成单个 zip（见下方「导出为什么不打 zip」）。
- 队列并发度 > 1、跨项目队列、任务优先级调整。

## P2（明确不做）

- 大集内部再分片、断点续传到镜级、导出到对象存储/第三方平台。

## 冻结的常量与约束

- `SERIES_MAX_SPAN = 10`：单个任务最多 10 集（沿用既有契约，前后端各一份，后端是
  唯一兜底）。`group_size ∈ [1, 10]`。
- 队列并发度恒为 1，串行；不提供并发开关。
- 任务状态五值：`idle | queued | running | succeeded | failed | cancelled`。
  **`paused` 不是任务状态**——暂停是队列级的：队列暂停时正在跑的任务退回 `queued`，
  进度快照保留。
- 完成判据挂产物，不挂状态字段：任务是否已完成由
  `merge.merge_is_current(pid, from, to, episode_nos)` 决定（film.mp4 存在且
  `film.report.json` 记录的各集输入指纹与当前一致）。列表接口为了轻量只读
  `film.mp4` 的 `stat` + report 摘要，详情接口做完整指纹校验并给出 `film_stale`。
- 队列遇到任务失败：**标记该任务 failed，继续下一个任务**（一个坏集不该堵死整夜的
  159 个任务）；但**连续 3 个任务失败自动暂停整队**并记 `stop_reason`，避免供应商
  整体故障时空烧。单个任务内部的五个步骤仍然 fail-closed、失败即停、不跳过。
- 入队前校验区间内集号齐全；缺集的任务不允许入队（返回 422 / 批量入队时列入
  `skipped` 并说明缺哪几集），不静默跳过、不兜底。
- 已完成（film 未过期）的任务默认不重复执行；`force: true` 才重跑。

## 数据模型

```sql
CREATE TABLE IF NOT EXISTS series_tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',            -- 空串表示用默认标题「第 X-Y 集」
    episode_from INTEGER NOT NULL,
    episode_to INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',
    queue_seq REAL,                            -- 入队序号；NULL = 不在队列
    run_id TEXT,                               -- 最近一次 workflow_runs.id
    progress_json TEXT NOT NULL DEFAULT '{}',  -- {episodes:[...], current_episode_no, current_stage, error}
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_series_tasks_range
    ON series_tasks(project_id, episode_from, episode_to);
CREATE INDEX IF NOT EXISTS idx_series_tasks_queue
    ON series_tasks(project_id, status, queue_seq);

CREATE TABLE IF NOT EXISTS series_queue_state (
    project_id TEXT PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,                          -- 连续失败自动停队时写入，中文原文
    updated_at REAL NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
```

`progress_json` 的形状沿用现有 `state.new_state()` 的 `episodes[]`（每集 5 个步骤
状态 `pending/running/done/skipped/failed`），去掉 `episode_from/episode_to`
（已是表列）。

`UNIQUE(project_id, episode_from, episode_to)` 让「重新生成清单」天然幂等：同区间
已存在就跳过，**不删除任何既有任务**（换切分粒度产生的重叠任务由用户自己删）。

## REST 契约（前缀 `/api`，均挂在 `app.domain.common.router`）

### `GET /projects/{pid}/series-tasks?offset=0&limit=50`

```jsonc
{
  "queue": {"paused": false, "running_task_id": null, "queued_count": 0, "stop_reason": null},
  "totals": {"all": 160, "idle": 154, "queued": 0, "running": 0, "succeeded": 5, "failed": 1, "cancelled": 0},
  "episodes": {"total": 1600, "min_no": 1, "max_no": 1600},
  "max_span": 10, "default_group_size": 10,
  "offset": 0, "limit": 50,
  "tasks": [ /* TaskSummary，按 episode_from 升序 */ ]
}
```

TaskSummary：

```jsonc
{
  "task_id": "st_xxx", "index": 1, "title": "第 1-10 集",
  "episode_from": 1, "episode_to": 10, "episode_count": 10,
  "missing_episode_nos": [],
  "status": "idle", "queue_position": null,
  "current_episode_no": null, "current_stage": null,
  "steps_done": 0, "steps_total": 50,
  "error": null,
  "film": {"url": "...", "duration_s": 0, "size_bytes": 0, "created_at": 0} /* 或 null */,
  "updated_at": 0, "finished_at": null
}
```

`limit` 上限 200；`index` 是全局序号（`offset + 行号`，1 起）。

### `GET /projects/{pid}/series-tasks/plan?group_size=10`

切分预览，不落库：

```jsonc
{"group_size": 10, "total_groups": 160, "new_groups": 155, "existing_groups": 5,
 "episodes": {"total": 1600, "min_no": 1, "max_no": 1600},
 "groups": [{"episode_from": 1, "episode_to": 10, "exists": true, "missing_episode_nos": []}],
 "truncated": true}
```

`groups` 最多返回前 200 条，超出置 `truncated: true`。

### `POST /projects/{pid}/series-tasks`

body 二选一：`{"group_size": 10}` 或 `{"ranges": [{"episode_from": 1, "episode_to": 10}]}`。
补齐创建，返回 `{"created": 155, "existing": 5, "tasks_total": 160}`。
两者都缺 → 422；`group_size` 越界 → 422；`ranges` 里跨度 > 10 或倒置 → 422。

### `DELETE /projects/{pid}/series-tasks/{task_id}`

仅 `idle / failed / cancelled` 且不在队列中可删；`queued / running` → 409 并提示先取消。
**只删任务记录，不删磁盘上的 film.mp4**（响应里明确写出这一点，界面照抄）。

### `GET /projects/{pid}/series-tasks/{task_id}`

TaskDetail = TaskSummary + `"episodes": [EpisodeEntry]` + `"film"` 带完整 `chapters`
+ `"film_stale": bool`（film 存在但输入指纹已变）。

EpisodeEntry 形状与现有契约一致：
`{"episode_id","episode_no","title","stages":{screenplay|storyboard|confirm|video|final: StageState},"error"}`
（比现契约多一个 `title`，详情页展示用）。

### `POST /projects/{pid}/series-tasks/enqueue`

body `{"task_ids": ["st_a","st_b"], "force": false}`，**数组顺序即执行顺序**。
返回：

```jsonc
{"enqueued": 2, "skipped": [{"task_id": "st_c", "reason": "已完成，成片未过期"}],
 "queue": {"paused": false, "running_task_id": "st_a", "queued_count": 1, "stop_reason": null}}
```

入队会顺带清掉 `series_queue_state.paused` 与 `stop_reason`（用户主动发起 = 解除停队），
并确保 runner 在跑。「触发单个任务」= 只传一个 id 的入队，没有第二条代码路径。

### `POST /projects/{pid}/series-tasks/cancel`

body `{"task_ids": [...]}`。`queued` → 退回 `idle` 并清 `queue_seq`；命中当前
`running` 任务 → 停掉 runner（进度保留，任务退回 `idle`），队列继续跑后面的任务。

### `POST /projects/{pid}/series-tasks/queue/pause` / `POST .../queue/resume`

暂停：置 `paused=1`，取消当前 runner；当前任务退回 `queued`（进度保留）。
继续：清 `paused`/`stop_reason`，重启 runner。无队列内容时 resume 返回 409。

### `POST /projects/{pid}/series-exports`

body `{"task_ids": [...]}`。只接受**成片已存在**的任务；其余列入 `skipped`。
全部不可导出 → 422。返回 Export 对象。

### `GET /projects/{pid}/series-exports`

最近 20 个导出包，倒序，元素同 Export 对象。

Export 对象：

```jsonc
{"export_id": "20260902-201530-ab12cd34", "created_at": 0.0,
 "total_size_bytes": 0, "item_count": 3,
 "manifest_url": "...", "list_url": "...",
 "items": [{"task_id","title","episode_from","episode_to","file_name",
            "url","size_bytes","duration_s"}],
 "skipped": [{"task_id","reason"}]}
```

落盘 `projects/{pid}/series/exports/{export_id}/`：
- `第001-010集.mp4` … **硬链接**指向各任务的 `film.mp4`（同一文件系统，零拷贝零占用）
- `manifest.json`（Export 对象的持久化形态）
- `下载清单.txt`（每行一个绝对可下载 URL，可直接喂给 aria2 / wget -i / 迅雷）

### 导出为什么不打 zip

单集成片实测 40–400MB，一个 10 集大集的连播成片约 1–2GB，勾选 10 个大集就是
10–20GB。zip 会（a）把磁盘占用翻倍（本机 `/` 只剩 26G），（b）把 10 个可分别重试的
下载合并成一次失败就得全部重来的单次下载，（c）mp4 本身已压缩，zip 压缩率≈0，纯粹
是搬运字节。所以 P0 的「打包」= 建一个零成本的导出目录 + 一份可批量喂给下载工具的
链接清单，界面上如实说明「共 N 个文件、合计 X GB，可逐个下载或用下载工具批量拉取」。
需要单文件 zip 时再按 P1 加异步打包任务（要带磁盘余量闸门）。

## 退场清单（一次删干净）

删除以下为旧单例模型服务的机器：

- 路由：`POST/GET /projects/{pid}/series-film`、`POST .../series-film/pause`、
  `POST .../series-film/resume`（`app/domain/series_ops/routes.py`）。
- 能力命令：`series.film_start` / `series.film_pause` / `series.film_resume`
  及 handler；输入模型 `SeriesFilmStartInput`（`SeriesFilmControlInput` 改名/复用给
  队列命令）。
- `orchestrator.start_series_film_core / pause_series_film_core / resume_series_film_core`
  （逻辑迁入 `queue.py`）。
- `state.new_state()` 里的 `episode_from/episode_to` 字段（已提升为表列）。
- 前端：`api/series.ts` 的四个旧函数、`pages/series/EpisodeRangePicker.tsx`
  （被「按每 N 集切分」取代）、`SeriesPage.tsx` 的单例分支。
- **半边状态清理**：启动恢复时把遗留的
  `workflow_runs(workflow_type='series_film', status IN ('CREATED','RUNNING','PAUSED_EXTERNAL'))`
  统一标为 `CANCELLED` + `failure_code='SERIES_TASK_MIGRATION'` +
  中文 `failure_message`，避免它们永远挂在「运行中」误导观测台。

新增能力命令（每条 mutating 路由都必须有，`assert_full_coverage` 守着）：
`series.tasks_generate` / `series.task_delete` / `series.tasks_enqueue` /
`series.tasks_cancel` / `series.queue_pause` / `series.queue_resume` /
`series.export_create`。风险等级沿用既有：会烧生成的（enqueue/queue_resume）
R2_MATERIAL，其余 R1_REVERSIBLE；`confirmation=NEVER`；scopes
`{"manju:generation-media"}`。

## 模块划分（app/domain/series_ops/，Python ≤500 行、单函数 ≤50 代码行）

| 模块 | 职责 |
| --- | --- |
| `tasks.py`（新） | `series_tasks` 表读写、切分计划、TaskSummary/TaskDetail 投影 |
| `queue.py`（新） | 串行 runner、入队/出队/暂停/继续、连续失败停队 |
| `exports.py`（新） | 导出包生成与列举 |
| `orchestrator.py`（改） | 只保留「跑一个任务」的五台 + merge 主循环 |
| `routes.py`（改） | 新路由集合；超 300 行就拆 `routes_tasks.py`/`routes_exports.py` |
| `state.py`（改） | 进度树形状、持久化到 `series_tasks.progress_json` |
| `stages.py` | 不变 |
| `merge.py` | 基本不变，可加轻量 `film_summary()` |
| `recovery.py`（改） | 复位 `running` 任务为 `queued` + 重启 runner + 遗留 run 清理 |

**包内纪律不变**：只用 `from . import x` + `x.name(...)`，禁止
`from .x import name`（`tests/test_series_ops_monkeypatch_guard.py` 守着；新模块
`tasks`/`queue`/`exports` 必须加进该测试的 `GUARDED_MODULES`）。

## 前端结构（每个文件 ≤300 行）

- `api/series.ts`：重写为任务契约。
- `pages/SeriesPage.tsx`：按 `useNav().episodeId` 分流——空 = 列表页，有值 = 任务详情页
  （该槽位承载 `taskId`；`App.tsx` 的 `routeFromPath`/`locationFor` 增加
  `/projects/{pid}/series/{taskId}`）。
- `pages/series/SeriesTaskPlanner.tsx`：切分表单（group_size + 预览 + 生成）。
- `pages/series/SeriesTaskList.tsx`：表格、勾选（跨页保留）、分页、单行「开始/查看」。
- `pages/series/SeriesTaskBar.tsx`：队列状态条 + 批量按钮（串行执行选中 / 取消选中 /
  暂停队列 / 继续队列 / 打包导出选中）。
- `pages/series/SeriesTaskDetail.tsx`：任务详情（复用 `SeriesProgressBoard` +
  `SeriesFilmPlayer`）。
- `pages/series/SeriesExportPanel.tsx`：导出包列表 + 下载入口。
- `pages/series/SeriesProgressBoard.tsx`：改为接收 `episodes/current_*/error`，不再接
  `SeriesRun`。
- `pages/series/EpisodeRangePicker.tsx`：删除。
- 样式统一进 `styles/SeriesPage.css`（`scripts/check_css_split.py` 已登记
  `pages/series/` 目录）。

## 界面承诺必须与实际行为一致

- 删除任务的弹窗要写「只删任务记录，磁盘上的成片保留」。
- 批量执行按钮要写「按勾选顺序串行执行，一次跑一个」。
- 已完成任务默认跳过，界面上要显式说「已完成的任务会跳过，需要重跑请先删除或勾选重跑」。
- 队列因连续失败自动暂停时，状态条要显示 `stop_reason` 原文并给「继续队列」按钮。
