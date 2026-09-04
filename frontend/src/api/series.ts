// 连播台域（对内代号 series-film，见 docs/series_task_console_plan.md 冻结契约）：
// 一部小说按每 N 集切成若干「连播任务」，每个任务串行跑完整链路（映射/分镜/确认/
// 生成/成片）后合成一部连播成片；任务级队列全项目共享、恒定串行。内部标识（view
// key `series`、路径 `/projects/{id}/series`、接口前缀 `series-tasks`/
// `series-exports`）与后端契约保持字面一致，不做本地改名；面向用户的文案一律叫
// "连播任务/连播成片"，在页面组件里翻译，不在这里。
//
// 2026-09-02 契约改版：项目级单例 run 换成 `series_tasks` 表 + 项目级串行队列，
// 旧的 getSeriesFilm/startSeriesFilm/pauseSeriesFilm/resumeSeriesFilm 四个函数
// 随旧路由一起退场（见 docs/series_task_console_plan.md「退场清单」）。

import { get, mutate } from "./client";

/** 单个连播任务最多跨多少集——后端是唯一兜底，这里只是提前给出前端校验反馈。 */
export const SERIES_MAX_SPAN = 10;

export type SeriesTaskStatus =
  | "idle"
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

/** 单集五个步骤 + 合成连播成片的 merge 步骤（挂在任务级，不挂在某一集上）。 */
export type Stage = "screenplay" | "storyboard" | "confirm" | "video" | "final" | "merge";

/** EpisodeEntry.stages 的键：Stage 去掉 "merge"（合成不是单集步骤）。 */
export type EpisodeStage = Exclude<Stage, "merge">;

/** skipped = 开始前已满足完成判据，未重跑（挂产物信号，不是状态字段）。 */
export type StageState = "pending" | "running" | "done" | "skipped" | "failed";

export interface EpisodeEntry {
  episode_id: string;
  episode_no: number;
  title: string;
  stages: Record<EpisodeStage, StageState>;
  error: string | null;
}

export interface FilmChapter {
  episode_no: number;
  start_s: number;
  duration_s: number;
}

/** 任务列表用的轻量成片摘要（只读 stat + report 摘要，不带章节表）。 */
export interface SeriesTaskFilm {
  url: string;
  duration_s: number;
  size_bytes: number;
  created_at: number;
}

/** 任务详情用的完整成片信息，比列表多一份章节表用于播放器跳转。 */
export interface SeriesTaskFilmDetail extends SeriesTaskFilm {
  chapters: FilmChapter[];
}

export interface SeriesQueueState {
  paused: boolean;
  running_task_id: string | null;
  queued_count: number;
  /** 连续失败自动停队时的中文原文；正常状态为 null。 */
  stop_reason: string | null;
}

export interface SeriesTaskTotals {
  all: number;
  idle: number;
  queued: number;
  running: number;
  succeeded: number;
  failed: number;
  cancelled: number;
}

export interface SeriesEpisodesRange {
  total: number;
  min_no: number;
  max_no: number;
}

export interface SeriesTaskSummary {
  task_id: string;
  index: number;
  /** 空串表示尚未命名，界面按「第 X-Y 集」兜底展示。 */
  title: string;
  episode_from: number;
  episode_to: number;
  episode_count: number;
  /** 区间内缺失的集号；非空时该任务不允许入队。 */
  missing_episode_nos: number[];
  status: SeriesTaskStatus;
  /** 在队列中的位置（1 起）；不在队列时为 null。 */
  queue_position: number | null;
  current_episode_no: number | null;
  current_stage: Stage | null;
  /** 正在并行处理的集号（升序）；串行或空闲时最多一个。 */
  running_episode_nos?: number[];
  steps_done: number;
  steps_total: number;
  error: string | null;
  film: SeriesTaskFilm | null;
  /** 成片存在但输入指纹已变（区间里某一集的成片后来重做过）——此时任务可以重新
   *  入队重合一次，列表/详情都必须标出来，不能被「已完成」三个字盖住。 */
  film_stale: boolean;
  updated_at: number;
  finished_at: number | null;
}

export interface SeriesTaskDetail extends Omit<SeriesTaskSummary, "film"> {
  episodes: EpisodeEntry[];
  film: SeriesTaskFilmDetail | null;
}

export interface SeriesTaskListResponse {
  queue: SeriesQueueState;
  totals: SeriesTaskTotals;
  episodes: SeriesEpisodesRange;
  max_span: number;
  default_group_size: number;
  offset: number;
  limit: number;
  tasks: SeriesTaskSummary[];
}

export interface SeriesTaskPlanGroup {
  episode_from: number;
  episode_to: number;
  /** 该区间对应的任务是否已存在（幂等生成时会被跳过）。 */
  exists: boolean;
  missing_episode_nos: number[];
}

export interface SeriesTaskPlanResponse {
  group_size: number;
  total_groups: number;
  new_groups: number;
  existing_groups: number;
  episodes: SeriesEpisodesRange;
  /** 最多前 200 条；超出时 truncated=true，界面不应假定这就是全部分组。 */
  groups: SeriesTaskPlanGroup[];
  truncated: boolean;
}

export type SeriesTaskCreateBody =
  | { group_size: number }
  | { ranges: { episode_from: number; episode_to: number }[] };

export interface SeriesTaskCreateResult {
  created: number;
  existing: number;
  tasks_total: number;
}

export interface SeriesTaskEnqueueSkipped {
  task_id: string;
  /** 中文原文，例如「已完成，成片未过期」「缺第 3、4 集」。 */
  reason: string;
}

export interface SeriesTaskEnqueueResult {
  enqueued: number;
  skipped: SeriesTaskEnqueueSkipped[];
  queue: SeriesQueueState;
}

export interface SeriesExportItem {
  task_id: string;
  title: string;
  episode_from: number;
  episode_to: number;
  file_name: string;
  url: string;
  size_bytes: number;
  duration_s: number;
}

export interface SeriesExportSkipped {
  task_id: string;
  reason: string;
}

export interface SeriesExport {
  export_id: string;
  created_at: number;
  total_size_bytes: number;
  item_count: number;
  manifest_url: string;
  list_url: string;
  items: SeriesExportItem[];
  skipped: SeriesExportSkipped[];
}

/** GET /projects/{id}/series-tasks——任务列表分页快照，附带队列状态与汇总。 */
export function getSeriesTasks(
  projectId: string,
  offset = 0,
  limit = 50,
): Promise<SeriesTaskListResponse> {
  return get(`/projects/${projectId}/series-tasks?offset=${offset}&limit=${limit}`);
}

/** GET /projects/{id}/series-tasks/plan——按 group_size 切分的预览，不落库。 */
export function getSeriesTaskPlan(
  projectId: string,
  groupSize: number,
): Promise<SeriesTaskPlanResponse> {
  return get(`/projects/${projectId}/series-tasks/plan?group_size=${groupSize}`);
}

/** POST /projects/{id}/series-tasks——补齐式生成任务清单；同区间已存在则跳过。 */
export function createSeriesTasks(
  projectId: string,
  body: SeriesTaskCreateBody,
): Promise<SeriesTaskCreateResult> {
  return mutate("POST", `/projects/${projectId}/series-tasks`, body);
}

/** DELETE /projects/{id}/series-tasks/{taskId}——仅 idle/failed/cancelled 且不在
 *  队列中可删；queued/running 会被后端拒绝（409）。只删任务记录，不删磁盘上的
 *  film.mp4（响应体形状未在契约中固定字段，界面不读取返回值，只按状态码判定
 *  成败并自行刷新列表）。 */
export function deleteSeriesTask(projectId: string, taskId: string): Promise<void> {
  return mutate("DELETE", `/projects/${projectId}/series-tasks/${encodeURIComponent(taskId)}`);
}

/** GET /projects/{id}/series-tasks/{taskId}——任务详情：TaskSummary + 完整分集
 *  进度树 + 带章节表的成片信息 + film_stale。 */
export function getSeriesTaskDetail(
  projectId: string,
  taskId: string,
): Promise<SeriesTaskDetail> {
  return get(`/projects/${projectId}/series-tasks/${encodeURIComponent(taskId)}`);
}

/** POST /projects/{id}/series-tasks/enqueue——数组顺序即执行顺序；只传一个 id 就
 *  是「触发单个任务」，没有第二条代码路径。会顺带清掉队列的 paused/stop_reason。 */
export function enqueueSeriesTasks(
  projectId: string,
  taskIds: string[],
  force = false,
): Promise<SeriesTaskEnqueueResult> {
  return mutate("POST", `/projects/${projectId}/series-tasks/enqueue`, {
    task_ids: taskIds,
    force,
  });
}

/** POST /projects/{id}/series-tasks/cancel——queued 退回 idle 并清队列位次；命中
 *  当前 running 任务则停掉 runner（进度保留），队列继续跑后面的任务。返回体形状
 *  未在契约中固定字段，调用方成功后自行刷新列表取权威状态。 */
export function cancelSeriesTasks(projectId: string, taskIds: string[]): Promise<void> {
  return mutate("POST", `/projects/${projectId}/series-tasks/cancel`, { task_ids: taskIds });
}

/** POST /projects/{id}/series-tasks/queue/pause——暂停队列；当前任务退回 queued
 *  （进度保留）。 */
export function pauseSeriesQueue(projectId: string): Promise<void> {
  return mutate("POST", `/projects/${projectId}/series-tasks/queue/pause`);
}

/** POST /projects/{id}/series-tasks/queue/resume——清 paused/stop_reason 并重启
 *  runner；队列为空时后端返回 409。 */
export function resumeSeriesQueue(projectId: string): Promise<void> {
  return mutate("POST", `/projects/${projectId}/series-tasks/queue/resume`);
}

/** POST /projects/{id}/series-exports——只接受成片已存在的任务，其余进 skipped；
 *  全部不可导出时后端返回 422。落盘为硬链接目录，不是 zip（见契约文档「导出为
 *  什么不打 zip」）。 */
export function createSeriesExport(
  projectId: string,
  taskIds: string[],
): Promise<SeriesExport> {
  return mutate("POST", `/projects/${projectId}/series-exports`, { task_ids: taskIds });
}

/** GET /projects/{id}/series-exports——最近 20 个导出包，倒序。契约文档未给出这
 *  个端点的信封字段名，这里按文档其余列表端点的统一约定（数组包一层具名字段，
 *  从不裸数组）推断为 `exports`；一旦后端落地发现不一致，只需改这一处。 */
export function getSeriesExports(projectId: string): Promise<{ exports: SeriesExport[] }> {
  return get(`/projects/${projectId}/series-exports`);
}
