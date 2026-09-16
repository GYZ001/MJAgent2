import { get, mutate, request } from "../client";
import type { Shot } from "./shot";
import type {
  ConfirmPreview,
  ProviderTaskReconcileResult,
  StartPreview,
  StoryboardClearPreview,
  StoryboardStatus,
  VideoModelSwitchResult,
} from "./status";

/** 兼容缺少内嵌 storyboard_status 的旧详情响应时的兜底轮询——BoardPage.tsx。 */
export function getStoryboardStatus(episodeId: string): Promise<StoryboardStatus> {
  return get(`/episodes/${episodeId}/storyboard/status`);
}

export function storyboardPreflight(episodeId: string): Promise<StartPreview> {
  return mutate("POST", `/episodes/${episodeId}/storyboard/preflight`, {});
}

export function startStoryboard(
  episodeId: string,
  body: { preflight_token: string },
) {
  return mutate("POST", `/episodes/${episodeId}/storyboard`, body);
}

export function confirmStoryboardPreview(episodeId: string): Promise<ConfirmPreview> {
  return request("POST", `/episodes/${episodeId}/confirm-preview`);
}

export function confirmStoryboard(
  episodeId: string,
  body: { preview_token: string },
) {
  return mutate("POST", `/episodes/${episodeId}/confirm`, body);
}

export function previewClearStoryboard(episodeId: string): Promise<StoryboardClearPreview> {
  return mutate("POST", `/episodes/${episodeId}/storyboard/clear-preview`, {});
}

export function clearStoryboard(
  episodeId: string,
  body: { preview_token: string },
) {
  return mutate("POST", `/episodes/${episodeId}/storyboard/clear`, body);
}

export function reconcileProviderTasks(episodeId: string): Promise<ProviderTaskReconcileResult> {
  return mutate("POST", `/episodes/${episodeId}/provider-tasks/reconcile`, {});
}

export function cancelStoryboard(episodeId: string) {
  return mutate("POST", `/episodes/${episodeId}/storyboard/cancel`, {});
}

export function setVideoModel(
  episodeId: string,
  body: { target_video_model: string; confirm_clear_prompts?: boolean },
): Promise<VideoModelSwitchResult> {
  return mutate("POST", `/episodes/${episodeId}/video-model`, body);
}

/** 审阅墙单镜详情——WallPage.tsx::loadDetail。 */
export function getShotReview(shotId: string): Promise<Shot> {
  return get(`/shots/${shotId}/review`);
}

/* ── 单镜编辑三步走 ──
 * 后端刻意把一次编辑拆成「起草会话 -> 影响预览 -> 提交」，三个 token 缺一不可：
 * edit_session_token 锁住进入编辑那一刻的基线（baseline_content_hash 对不上就是
 * 别人改过，409）；preview_token 承载用户实际看到并认可的那份影响（提交时后端
 * 逐字段比对 normalized_changes，对不上也是 409）。所以前端不能跳过预览直接 PUT，
 * 也不能复用上一次的 preview_token 去提交另一组改动。
 * 见 app/domain/storyboard_ops/shot_edit_session.py 与 edit_shot.py。
 */
export interface ShotEditSession {
  edit_session_token: string;
  baseline_artifact_id: string | null;
  baseline_content_hash: string;
  lease_expires_at: number;
}

/** 影响预览：unchanged=true 表示结构化内容没变，不会建新版本也不会失效下游。 */
export interface ShotEditImpact {
  unchanged?: boolean;
  changed_fields: string[];
  preview_token?: string;
  stale_count?: number;
  paid_media_invalidated?: boolean;
  by_artifact_type?: Record<string, number>;
  message?: string;
}

export function startShotEditSession(shotId: string): Promise<ShotEditSession> {
  return mutate("POST", `/shots/${shotId}/edit-session`, {});
}

export function previewShotEditImpact(
  shotId: string,
  body: { edit_session_token: string; changes: Record<string, unknown> },
): Promise<ShotEditImpact> {
  return mutate("POST", `/shots/${shotId}/impact-preview`, body);
}

export function updateShot(
  shotId: string,
  body: Record<string, unknown>,
): Promise<{ ok?: boolean; unchanged?: boolean; artifact_id?: string }> {
  return mutate("PUT", `/shots/${shotId}`, body);
}
