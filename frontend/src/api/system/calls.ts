import { get } from "../client";
import type { Page } from "./jobs";

interface CallContext {
  project_id?: string;
  project_name?: string;
  episode_id?: string;
  episode_no?: number;
  episode_title?: string;
  shot_id?: string;
  shot_no?: number;
  stage?: string;
  purpose?: string;
  error_stage?: string;
}

export interface Call {
  id: number;
  ts: number;
  kind: string;
  model?: string;
  model_label?: string;
  status: string;
  effective_status: string;
  category: "business" | "workflow" | "internal";
  http_status?: number;
  latency_ms: number;
  error?: string;
  run_id?: string;
  step_run_id?: string;
  trace_id?: string;
  operation_id?: string;
  attempt_no?: number;
  supersedes_call_id?: number;
  superseded_by_call_id?: number;
  context?: CallContext;
}

export interface CallAggregate {
  key: string;
  project_id?: string;
  project_name: string;
  episode_no?: number;
  shot_no?: number;
  kind: string;
  root_cause: string;
  count: number;
  first_ts: number;
  last_ts: number;
  call_ids: number[];
  run_id?: string;
}

export interface CallsPage extends Page<Call> {
  aggregates: CallAggregate[];
  failed_total: number;
}

export interface CallDetail extends Call {
  request_json?: string;
  response_json?: string;
  meta?: string;
  request_json_size: number;
  response_json_size: number;
  meta_size: number;
  raw_access: boolean;
}

export function getCallsPage(query: string, projectId?: string): Promise<CallsPage> {
  return get(
    projectId
      ? `/projects/${encodeURIComponent(projectId)}/observability/calls?${query}`
      : `/system/calls/query?${query}`,
  );
}

export function getCallDetail(callId: number | string, projectId?: string): Promise<CallDetail> {
  return get(
    projectId
      ? `/projects/${encodeURIComponent(projectId)}/observability/calls/${callId}`
      : `/system/calls/${callId}`,
  );
}
