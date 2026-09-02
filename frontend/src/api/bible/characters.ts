import { mutate, request } from "../client";
import type { BibleImpactPreview } from "./core";

export interface ReferenceImage {
  id: string;
  image_url?: string | null;
  type: string;
  source: string;
  qualityScore?: number | null;
  selectedForSeedance?: boolean;
  deleted?: boolean;
  rejectReason?: string | null;
  qa?: {
    overall?: number | null;
    status?: string;
    issues?: string[];
    action_match?: number;
    body_proportion?: number;
    face_identity?: number | null;
    outfit_match?: number;
    hair_match?: number;
    scene_match?: number;
    hard_failures?: string[];
  } | null;
  entity_type?: string | null;
  entity_name?: string | null;
  library_revision_id?: string | null;
  library_view_id?: string | null;
  view_role?: string | null;
  purposes?: string[] | null;
  required?: boolean;
  slot_key?: string | null;
  dependency_manifest?: Record<string, unknown> | null;
  gate_status?: "passed" | "failed" | "unverified" | "unknown" | string | null;
  downstream_eligibility?:
    | "eligible"
    | "ineligible"
    | "unverified"
    | string
    | null;
  rule_version?: string | null;
  hard_failures?: string[];
  soft_warnings?: string[];
  referenced_by_version_ids?: string[];
  selection_reason?: string | null;
  restoreOverrideReason?: string | null;
}

export interface PortraitView {
  id: string;
  view_role?: string;
  framing?: string;
  status?: string;
  image_url?: string | null;
  qa?: { overall?: number | null; issues?: string[] } | null;
  qa_overall?: number | null;
}

export interface Portrait {
  id: string;
  ep_start: number;
  ep_end: number | null;
  appearance?: string | null;
  base_portrait_id?: string | null;
  image_url?: string | null;
  pack_status?: string | null;
  group_qa?: {
    overall?: number | null;
    issues?: string[];
    hard_failures?: string[];
    status?: string;
    face_consistency?: number | null;
    outfit_consistency?: number | null;
    hair_consistency?: number | null;
    body_consistency?: number | null;
    views?: Array<{
      view_role?: string;
      overall?: number | null;
      issues?: string[];
      hard_failures?: string[];
      status?: string;
    }>;
  } | null;
  change?: {
    reason?: string;
    adoption_reason?: string;
    rollback_source?: string;
    rolled_back_at?: number;
    change_dimensions?: string[];
    persistence?: string;
    [key: string]: unknown;
  } | null;
  views?: PortraitView[];
}

export interface CharacterPortraitCandidate {
  id?: string;
  portrait_id?: string;
  image_url?: string | null;
  current?: boolean;
  historical?: boolean;
  is_current?: boolean;
  adopted?: boolean;
  pack_status?: string | null;
  status?: string | null;
  reason?: string | null;
  created_at?: number | null;
  adopted_at?: number | null;
  group_qa?: Portrait["group_qa"];
  qa?: Portrait["group_qa"];
  views?: PortraitView[];
  soft_warnings?: string[];
  artifact_id?: string | null;
  candidate_kind?: "single_image" | "portrait_pack" | string;
  attempt?: number | null;
  adoptable?: boolean;
  blocked_reason?: string | null;
  base_portrait_id?: string | null;
  ep_start?: number;
  ep_end?: number | null;
  change?: {
    reason?: string;
    adoption_reason?: string;
    decision_reason?: string;
    review_status?: string;
  } | null;
}

export interface Character {
  name: string;
  role: string;
  appearance_canonical: string;
  personality: string;
  speech_style: string;
  relationships: { to: string; relation: string }[];
  ref_image_path?: string | null;
  ref_image_url?: string | null;
  portrait_prompt_override?: string | null;
  portrait_prompt_effective?: string;
  portraits?: Portrait[];
  presence_status?: "onstage" | "mentioned_only" | "unresolved";
  portrait_eligible?: boolean;
  appearance_status?: "grounded" | "insufficient_evidence" | "deferred";
}

/** 定妆照生成/补齐/单角色重做共用的入口，见 BiblePage.tsx 的 retryRefs /
 *  restartRefsWithLatestSettings / PortraitBlock.regenerate。 */
export function generateRefs(
  projectId: string,
  body: {
    resume?: boolean;
    character?: string;
    confirm: true;
    quote_id: string;
    idempotency_key: string;
  },
) {
  return mutate("POST", `/projects/${projectId}/refs`, body);
}

export function cancelRefsGeneration(projectId: string) {
  return mutate("POST", `/projects/${projectId}/refs/cancel`);
}

export function setCharacterPortraitPrompt(
  projectId: string,
  characterName: string,
  body: { portrait_prompt: string },
): Promise<{ reset_to_default?: boolean }> {
  return mutate(
    "PUT",
    `/projects/${projectId}/characters/${encodeURIComponent(characterName)}/portrait`,
    body,
  );
}

export function refsPrecheck(
  projectId: string,
  body?: {
    character?: string;
    characters?: string[];
    resume?: boolean;
    view_role?: string;
  },
): Promise<import("./core").RefsPrecheck> {
  return request("POST", `/projects/${projectId}/refs/precheck`, body || {});
}

export function refsGaps(projectId: string): Promise<{
  missing_count: number;
  image_count: number;
  items: Array<Record<string, unknown>>;
  precheck: import("./core").RefsPrecheck;
}> {
  return request("GET", `/projects/${projectId}/refs/gaps`);
}

export function refsProgress(projectId: string): Promise<{
  total: number;
  ready: number;
  failed: number;
  missing: number;
  deferred?: number;
  blocked?: number;
  refs_status?: string;
  refs_target?: string | null;
  items: Array<{
    character: string;
    status: string;
    missing_views?: string[];
    current?: boolean;
    pack_status?: string;
    reason?: string;
  }>;
  updated_at?: number;
}> {
  return request("GET", `/projects/${projectId}/refs/progress`);
}

export function saveCharacter(
  projectId: string,
  name: string,
  body: {
    character: Character;
    expected_version?: number | null;
    impact_preview_fingerprint?: string;
    confirm?: boolean;
  },
): Promise<{
  bible_version?: number;
  character?: Character;
  impact?: BibleImpactPreview;
}> {
  return request(
    "PUT",
    `/projects/${projectId}/characters/${encodeURIComponent(name)}`,
    body,
  );
}

export function listPortraitCandidates(
  projectId: string,
  name: string,
): Promise<
  | {
      items?: CharacterPortraitCandidate[];
      candidates?: CharacterPortraitCandidate[];
    }
  | CharacterPortraitCandidate[]
> {
  return request("GET", `/projects/${projectId}/characters/${encodeURIComponent(name)}/portrait-candidates`);
}

export function adoptPortraitCandidate(
  projectId: string,
  name: string,
  portraitId: string,
  body: { reason: string },
) {
  return request(
    "POST",
    `/projects/${projectId}/characters/${encodeURIComponent(name)}/portraits/${encodeURIComponent(portraitId)}/adopt`,
    body,
  );
}

export function rollbackPortraitCandidate(
  projectId: string,
  name: string,
  portraitId: string,
) {
  return request(
    "POST",
    `/projects/${projectId}/characters/${encodeURIComponent(name)}/portraits/${encodeURIComponent(portraitId)}/rollback`,
  );
}

export function regenerateCharacterView(
  projectId: string,
  characterName: string,
  portraitId: string,
  viewRole: string,
  body?: {
    confirm?: boolean;
    quote_id?: string;
    idempotency_key?: string;
  },
) {
  return request(
    "POST",
    `/projects/${projectId}/characters/${encodeURIComponent(characterName)}/portraits/${portraitId}/views/${encodeURIComponent(viewRole)}/regenerate`,
    body || {},
  );
}
