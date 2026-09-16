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
