import { get, mutate, request } from "../client";

export interface Job {
  id: string;
  source: "run" | "job" | "screenplay";
  run_id?: string;
  kind?: string;
  workflow_type?: string;
  scope_type?: string;
  scope_id?: string;
  project_id?: string;
  episode_id?: string;
  shot_id?: string;
  status: string;
  raw_status?: string;
  error?: string;
  reason_code?: string;
  recovered_by_run_id?: string;
  recovered_tail_run_id?: string;
  state_revision?: number;
  shot_no?: number;
  episode_no?: number;
  episode_title?: string;
  project_name?: string;
  updated_at: number;
}

export interface JobsSummary {
  counts: Record<string, number>;
  startup_recovery?: Record<string, number>;
  recent: Job[];
  total: number;
  server_time: number;
  scope?: { type: "project"; project_id: string; project_name: string };
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  page_count: number;
  server_time: number;
  query_ms?: number;
  scope?: { type: "project"; project_id: string; project_name: string };
}

export interface JobsPage extends Page<Job> {
  counts: Record<string, number>;
  startup_recovery?: Record<string, number>;
}

export interface VideoModeAudit {
  planned_mode?: string | null;
  actual_mode?: string | null;
  video_input_intent?: string | null;
  depends_on_shot_id?: string | null;
  status?: string | null;
  degraded_from_mode?: string | null;
  degraded_to_mode?: string | null;
  degraded_reason?: string | null;
  capability_snapshot_id?: string | null;
  stale?: boolean;
  stale_reason?: string | null;
}

/** 项目观测台 vs 系统观测台共用同一套「任务」查询/操作，projectId 存在时打
 *  `/projects/{id}/observability/...`，否则打 `/system/...`——见 MonitorPage.tsx
 *  的 jobsSummaryPoll/jobsPagePoll/JobDrawer。 */
export function getJobsSummary(projectId?: string): Promise<JobsSummary> {
  return get(
    projectId
      ? `/projects/${encodeURIComponent(projectId)}/observability/jobs?page_size=100`
      : "/system/jobs",
  );
}

export function getJobsPage(query: string, projectId?: string): Promise<JobsPage> {
  return get(
    projectId
      ? `/projects/${encodeURIComponent(projectId)}/observability/jobs?${query}`
      : `/system/jobs/query?${query}`,
  );
}

/** 单条任务详情；source 是任务来源标记（job.source 或 "auto" 表示由后端自行判定）。 */
export function getJobDetail(
  jobId: string,
  source: string,
  projectId?: string,
): Promise<Record<string, unknown>> {
  return get(
    projectId
      ? `/projects/${encodeURIComponent(projectId)}/observability/jobs/${encodeURIComponent(jobId)}?source=${source}`
      : `/system/jobs/${encodeURIComponent(jobId)}?source=${source}`,
  );
}

/** JobDrawer 里「重试/恢复/取消/确认重新提交」四个动作在项目观测台下统一走
 *  这一个端点，区别只在 body 与 action 段。 */
export function runProjectObservabilityJobAction(
  projectId: string,
  jobId: string,
  source: string,
  action: string,
  body?: Record<string, unknown>,
) {
  return mutate(
    "POST",
    `/projects/${encodeURIComponent(projectId)}/observability/jobs/${encodeURIComponent(jobId)}/${action}?source=${source}`,
    body,
  );
}

/** 系统观测台下 source==="run" 的任务走 /runs/{id}/{action}。 */
export function runRunAction(
  runId: string,
  action: string,
  body?: Record<string, unknown>,
) {
  return mutate("POST", `/runs/${encodeURIComponent(runId)}/${action}`, body);
}

export function cancelJob(jobId: string) {
  return mutate("POST", `/jobs/${encodeURIComponent(jobId)}/cancel`);
}

export function retrySystemJob(jobId: string, body: Record<string, unknown>) {
  return request("POST", `/system/jobs/${encodeURIComponent(jobId)}/retry`, body);
}

/** 供应商终态拒绝、且实际计费确凿为零——给 JobDrawer 判断要不要展示「释放零
 *  扣费预留」按钮，以及确认文案里要写的金额与理由。不满足也带结构化 reason，
 *  不把用户晾在原地。 */
export interface ZeroCostCandidate {
  job_id: string;
  eligible: boolean;
  reason: string;
  reserved_amount_cny: number;
}

export function getZeroCostCandidate(jobId: string): Promise<ZeroCostCandidate> {
  return get(`/system/provider-tasks/zero-cost-candidates/${encodeURIComponent(jobId)}`);
}

/** 本地二段式确认：不带 confirm 只预览（服务端会返回将要释放的清单与金额，
 *  抛 422 供 JobDrawer 展示确认文案），带 confirm=true 才真正结算为 0。 */
export function releaseZeroCostJobs(
  jobIds: string[],
  confirm: boolean,
): Promise<{ ok: boolean; released: Array<{ job_id: string; amount_cny: number; reason: string }> }> {
  return request(
    "POST",
    `/system/provider-tasks/zero-cost-release${confirm ? "?confirm=true" : ""}`,
    { job_ids: jobIds },
  );
}
