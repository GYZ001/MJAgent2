import { request } from "../client";
import type { ArtifactEvidence } from "../common";
import type { RefsCostPrecheck } from "./core";

export interface SceneCostPrecheck
  extends Omit<RefsCostPrecheck, "character_count" | "views_per_character"> {
  scene_count: number;
  actual_view_count: number;
  views_per_scene: number;
  max_retries?: number;
}

export interface SceneGapItem {
  scene: string;
  scene_reference_id?: string | null;
  category:
    | "missing"
    | "hard_failure"
    | "warning"
    | "interrupted"
    | "unverified";
  reason: string;
  views: string[];
  hard_failures?: string[];
  warnings?: string[];
  pack_status?: string | null;
}

export interface SceneGapScan {
  project_id: string;
  total: number;
  items: SceneGapItem[];
  counts: Record<string, number>;
  read_only: true;
}

export interface SceneRefsProgress {
  project_id: string;
  total: number;
  ready: number;
  failed: number;
  missing: number;
  unverified: number;
  remaining: number;
  refs_status?: string;
  refs_target?: string | string[] | null;
  updated_at?: number;
  run_id?: string | null;
  phase?: string | null;
  current_scene?: string | null;
  current_view?: string | null;
  attempt?: number;
  spent_cny?: number;
  items: Array<{ scene: string; status: string; detail?: SceneGapItem }>;
}

export interface SceneRefSegment {
  id?: string;
  ep_start: number;
  ep_end: number | null;
  scene_canonical?: string | null;
  image_url?: string | null;
  qa?: {
    overall?: number;
    issues?: string[];
    warnings?: string[];
    hard_failures?: string[];
    uncertainties?: string[];
    status?: string;
    hard_gate_passed?: boolean;
  } | null;
  qa_overall?: number | null;
  artifact_id?: string | null;
  evidence?: ArtifactEvidence | null;
  change?: { reason?: string; [key: string]: unknown } | null;
  reference_summary?: {
    episode_numbers: number[];
    episodes?: Array<{ id: string; episode_no: number }>;
    shot_count: number;
  };
  pack_status?: string | null;
  group_qa?: {
    overall?: number | null;
    issues?: string[];
    warnings?: string[];
    hard_failures?: string[];
    uncertainties?: string[];
    status?: string;
    hard_gate_passed?: boolean;
    policy_version?: string;
    required_views?: string[];
    missing_required?: string[];
  } | null;
  views?: {
    id: string;
    view_role?: string;
    camera_axis?: string;
    status?: string;
    image_url?: string | null;
    qa?: { overall?: number | null } | null;
    qa_overall?: number | null;
  }[];
}

export interface SceneReferenceCandidate {
  artifact_id: string;
  status: string;
  trust_level: string;
  attempt?: number | null;
  image_url?: string | null;
  evidence?: ArtifactEvidence | null;
}

export interface Scene {
  name: string;
  scene_canonical: string;
  location_kind?: string;
  ref_image_path?: string | null;
  ref_image_url?: string | null;
  scene_prompt_override?: string | null;
  scene_prompt_effective?: string;
  scene_refs?: SceneRefSegment[];
  scene_candidates?: SceneReferenceCandidate[];
  space?: string;
  time_of_day?: string;
  lighting?: string;
  landmarks?: string[];
  first_episode?: number | null;
  required_views?: string[];
  discovery_sources?: string[];
}

export function sceneBiblePreview(projectId: string): Promise<{
  project_id: string;
  scenes: Scene[];
  precheck: SceneCostPrecheck;
  generates_images: false;
}> {
  return request("POST", `/projects/${projectId}/scene-bible/preview`);
}

export function sceneBiblePrecheck(
  projectId: string,
  scenes: Scene[],
): Promise<SceneCostPrecheck> {
  return request("POST", `/projects/${projectId}/scene-bible/precheck`, { scenes });
}

export function genSceneBible(
  projectId: string,
  body: { scenes: Scene[]; confirm: true; quote_id: string },
) {
  return request("POST", `/projects/${projectId}/scene-bible`, body);
}

export function sceneRefsPrecheck(
  projectId: string,
  body?: {
    scenes?: string[];
    resume?: boolean;
    view_role?: string;
    scene_reference_id?: string;
    action?: string;
  },
): Promise<SceneCostPrecheck> {
  return request("POST", `/projects/${projectId}/scene-refs/precheck`, body || {});
}

export function sceneRefsGaps(projectId: string): Promise<SceneGapScan> {
  return request("GET", `/projects/${projectId}/scene-refs/gaps`);
}

export function sceneRefsProgress(projectId: string): Promise<SceneRefsProgress> {
  return request("GET", `/projects/${projectId}/scene-refs/progress`);
}

export function genSceneRefs(
  projectId: string,
  body: { scenes?: string[]; resume?: boolean; confirm: true; quote_id: string },
) {
  return request("POST", `/projects/${projectId}/scene-refs`, body);
}

export function cancelSceneRefs(projectId: string) {
  return request("POST", `/projects/${projectId}/scene-refs/cancel`);
}

export function editScenePrompt(
  projectId: string,
  sceneName: string,
  scenePrompt: string,
) {
  return request(
    "PUT",
    `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/prompt`,
    { scene_prompt: scenePrompt },
  );
}

export function editSceneAnchor(
  projectId: string,
  sceneName: string,
  body: {
    expected_version: number;
    scene_canonical: string;
    location_kind?: string;
    space?: string;
    time_of_day?: string;
    lighting?: string;
    landmarks?: string[];
  },
) {
  return request(
    "PUT",
    `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}`,
    body,
  );
}

export function regenerateSceneView(
  projectId: string,
  sceneName: string,
  sceneRefId: string,
  viewRole: string,
  body: { confirm: true; quote_id: string },
) {
  return request(
    "POST",
    `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/refs/${sceneRefId}/views/${encodeURIComponent(viewRole)}/regenerate`,
    body,
  );
}

export function adoptSceneCandidate(
  projectId: string,
  sceneName: string,
  artifactId: string,
  reason?: string,
) {
  return request(
    "POST",
    `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/candidates/${encodeURIComponent(artifactId)}/adopt`,
    { reason: reason || "人工采纳候选" },
  );
}

export function rollbackSceneReference(
  projectId: string,
  sceneName: string,
  sceneRefId: string,
  reason?: string,
) {
  return request(
    "POST",
    `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/refs/${sceneRefId}/rollback`,
    { reason: reason || "回滚到历史通过场景包" },
  );
}

