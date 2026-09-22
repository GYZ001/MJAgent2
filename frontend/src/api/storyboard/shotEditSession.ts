import { mutate } from "../client";

/**
 * 单镜头安全工作区三步握手（app/storyboard_workspace.py）：起草编辑租约、校验并
 * 预览改动影响、再提交保存。目前唯一消费方是分镜台的台词修订入口
 * （components/SegmentDialogueRevision.tsx）——供应商因台词原句拒收时，用户在
 * 界面上改措辞的唯一落地路径。
 */
export interface ShotEditSessionStart {
  edit_session_token: string;
  baseline_artifact_id: string | null;
  baseline_content_hash: string;
  lease_expires_at: number;
}

export function startShotEditSession(shotId: string): Promise<ShotEditSessionStart> {
  return mutate("POST", `/shots/${shotId}/edit-session`, {});
}

export interface ShotEditImpactUnchanged {
  unchanged: true;
  changed_fields: string[];
  message: string;
}

export interface ShotEditImpactChanged {
  unchanged: false;
  changed_fields: string[];
  normalized_changes: Record<string, unknown>;
  baseline_artifact_id: string | null;
  baseline_content_hash: string;
  requires_reconfirm: boolean;
  paid_media_invalidated: boolean;
  stale_descendant_ids: string[];
  stale_count: number;
  /** 键固定为「参考图」「视频版本」「证据链」（app/domain/storyboard_ops/shot_edit_session.py）。 */
  by_artifact_type: Record<string, number>;
  preview_token: string;
  preview_expires_at: number;
}

export type ShotEditImpactPreview = ShotEditImpactUnchanged | ShotEditImpactChanged;

export function previewShotEditImpact(
  shotId: string,
  body: { edit_session_token: string; changes: Record<string, unknown> },
): Promise<ShotEditImpactPreview> {
  return mutate("POST", `/shots/${shotId}/impact-preview`, body);
}

export interface ShotUpdateResult {
  ok: true;
  unchanged?: boolean;
  invalidated?: Record<string, unknown>;
  artifact_id: string | null;
  qa_warnings?: string[];
  gate_retry_exhausted?: boolean;
  impact: {
    stale_count?: number;
    stale_descendant_ids?: string[];
    requires_reconfirm?: boolean;
    paid_media_invalidated?: boolean;
  };
}

/** patch 必须与上一次 previewShotEditImpact 的 changes 逐字相同，否则后端 409
 *  （见 app/domain/storyboard_ops/edit_shot.py::edit_shot 的一致性核对）。 */
export function updateShot(
  shotId: string,
  body: {
    [patchField: string]: unknown;
    expected_version?: string;
    edit_session_token: string;
    preview_token: string;
    baseline_content_hash: string;
    revision_reason?: string;
  },
): Promise<ShotUpdateResult> {
  return mutate("PUT", `/shots/${shotId}`, body);
}
