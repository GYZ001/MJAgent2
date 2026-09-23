import { download, get, mutate } from "./client";

interface DeliveryCheck {
  key: string;
  passed: boolean;
  message: string;
  evidence?: unknown;
}

export interface DeliveryReadiness {
  episode_id: string;
  ready: boolean;
  evidence_coverage: number;
  checks: DeliveryCheck[];
  blockers: DeliveryCheck[];
  warnings: { code?: string; message?: string; shot_no?: number }[];
}

export interface DeliveryPackageRecord {
  id: string;
  episode_id: string;
  artifact_id: string;
  status: string;
  package_path: string;
  created_at: number;
  approved_at?: number | null;
}

interface MixShot {
  shot_id: string;
  shot_no: number;
  duration_s: number;
  video_url: string | null;
  has_adopted: boolean;
  has_model_candidate?: boolean;
  playback_rate?: number;
  effective_duration_s?: number;
}

export interface MixStatus {
  episode_id: string;
  title: string;
  episode_no: number;
  shots_total: number;
  shots_ready: number;
  ready: boolean;
  generation_active?: boolean;
  active_shot_nos?: number[];
  all_ready?: boolean;
  shots_skipped?: number;
  skipped_shot_nos?: number[];
  final_video_url: string | null;
  final_video_stale?: boolean;
  final_is_partial?: boolean;
  final_edit_report?: Record<string, unknown> | null;
  /** 有字幕嵌入且生成了 srt 文件时的可下载地址（/media/...）；否则为 null/缺省。 */
  subtitle_srt_url?: string | null;
  /** 整集合成正在后台执行（2026-09-15：合成改为后台任务，浏览器请求立即返回）。 */
  concat_in_progress?: boolean;
  /** 最近一次后台合成的失败原因；成功后清空。 */
  concat_last_error?: string | null;
  shots: MixShot[];
}

export interface MixResult {
  video_url: string;
  shots: number;
  total_duration_s: number;
  ffmpeg_missing?: boolean;
  shots_total?: number;
  shots_skipped?: number;
  skipped_shot_nos?: number[];
  missing_model_shot_nos?: number[];
  skip_reasons?: Record<string, string>;
  included_shot_nos?: number[];
  partial?: boolean;
  final_video_stale?: boolean;
  playback_rates?: Record<string, number>;
  final_edit?: Record<string, unknown>;
  note?: string;
}

export function getMixStatus(episodeId: string | null): Promise<MixStatus> {
  return get(`/episodes/${episodeId}/mix-status`);
}

export function getDeliveryReadiness(episodeId: string | null): Promise<DeliveryReadiness> {
  return get(`/episodes/${episodeId}/delivery/readiness`);
}

export function getDeliveryPackages(episodeId: string | null): Promise<DeliveryPackageRecord[]> {
  return get(`/episodes/${episodeId}/delivery/packages`);
}

export function approveDelivery(
  episodeId: string,
  body: {
    package_id: string;
    decided_by: string;
    decision: string;
    reason: string;
    accepted_risk?: string;
    idempotency_key: string;
  },
) {
  return mutate("POST", `/episodes/${episodeId}/delivery/approve`, body);
}

export function concatenateEpisode(
  episodeId: string,
  body: { idempotency_key: string },
): Promise<MixResult> {
  return mutate("POST", `/episodes/${episodeId}/concatenate`, body);
}

export function createDeliveryPackage(
  episodeId: string,
  body: { idempotency_key: string },
) {
  return mutate("POST", `/episodes/${episodeId}/delivery/package`, body);
}

export interface CustomerFeedbackRecord {
  id: string;
  created_at: number;
  created_by: string;
  message: string;
  rating: number | null;
}

/** 2026-09-23 起 request_revision 已退场：反馈只做记录，不创建修订任务
 *  （那条 workflow_run 从没有执行者推进过，也没有界面展示过，还会挡生产部署）。
 *  需要修改本集内容，请到分镜台或生成台处理。 */
export function submitCustomerFeedback(
  episodeId: string,
  body: { message: string; created_by: string },
) {
  return mutate("POST", `/episodes/${episodeId}/customer-feedback`, body);
}

export function getCustomerFeedback(episodeId: string): Promise<CustomerFeedbackRecord[]> {
  return get(`/episodes/${episodeId}/customer-feedback`);
}

/** 交付候选质检报告/归档包下载——CinemaPage.tsx::downloadDeliveryFile。
 *  download() 本身没有 dedup（只有 client.ts 的 get() 有），保持原样直传。 */
export function downloadDeliveryFile(
  packageId: string,
  kind: "report" | "archive",
): Promise<Blob> {
  return download(`/delivery/packages/${packageId}/${kind}`);
}
