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

export function submitCustomerFeedback(
  episodeId: string,
  body: { message: string; created_by: string; request_revision: boolean },
) {
  return mutate("POST", `/episodes/${episodeId}/customer-feedback`, body);
}

/** 交付候选质检报告/归档包下载——CinemaPage.tsx::downloadDeliveryFile。
 *  download() 本身没有 dedup（只有 client.ts 的 get() 有），保持原样直传。 */
export function downloadDeliveryFile(
  packageId: string,
  kind: "report" | "archive",
): Promise<Blob> {
  return download(`/delivery/packages/${packageId}/${kind}`);
}
