import { get, mutate } from "../client";

export interface SystemOverview {
  projects: Array<{
    id: string;
    name: string;
    created_at: number;
    job_counts: Record<string, number>;
    call_count: number;
  }>;
  totals: {
    projects: number;
    jobs: number;
    calls: number;
    unattributed_jobs: number;
    unattributed_calls: number;
  };
  server_time: number;
}

export function getSystemOverview(): Promise<SystemOverview> {
  return get("/system/overview");
}

/** 前端埋点上报——MonitorPage 的 track()、BiblePage 的 trackBible()、
 *  CharacterQaPanel 的候选审阅打开事件共用同一个端点。失败静默吞掉（埋点
 *  不应该影响主流程），调用方自行 `.catch(() => undefined)`。 */
export function reportMonitorEvent(
  name: string,
  dimensions: Record<string, string | number | boolean> = {},
  objectId = "",
) {
  return mutate("POST", "/system/monitor/events", {
    name,
    dimensions,
    object_id: objectId,
  });
}
