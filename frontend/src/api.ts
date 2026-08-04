import { requestCapabilityApproval } from "./capabilityApproval";

class ApiError extends Error {
  // code/category/errorId 来自后端报错码系统：技术类报错前端只拿到这三样，原文留后端日志。
  constructor(
    public status: number,
    message: string,
    public code?: string,
    public category?: string,
    public errorId?: string,
    public detail?: unknown,
  ) {
    super(message);
  }
}

const SESSION_HEADER = "X-Manju-Session";
const APPROVAL_HEADER = "X-Manju-Approval-Token";

let sessionToken: string | null = null;
let sessionReady: Promise<void> | null = null;
const inflightGets = new Map<string, Promise<any>>();

async function ensureSession(forceRefresh = false): Promise<void> {
  if (forceRefresh) {
    sessionToken = null;
    sessionReady = null;
  }
  if (sessionToken) return;
  if (!sessionReady) {
    sessionReady = fetch("/api/session")
      .then(async (resp) => {
        if (!resp.ok)
          throw new Error(`无法领取本机会话凭证：HTTP ${resp.status}`);
        const body = (await resp.json()) as { session_token?: string };
        sessionToken = body.session_token || null;
      })
      .catch((err) => {
        sessionReady = null;
        throw err;
      });
  }
  await sessionReady;
}

function baseHeaders(extra?: HeadersInit, approvalToken?: string): Headers {
  const headers = new Headers(extra);
  if (sessionToken) headers.set(SESSION_HEADER, sessionToken);
  if (approvalToken) headers.set(APPROVAL_HEADER, approvalToken);
  return headers;
}

async function handle(resp: Response) {
  if (resp.ok) return resp.json();
  let detail = `HTTP ${resp.status}`;
  let code: string | undefined;
  let category: string | undefined;
  let errorId: string | undefined;
  let rawDetail: unknown;
  try {
    const body = await resp.json();
    rawDetail = body.detail ?? body;
    detail =
      typeof body.detail === "string"
        ? body.detail
        : typeof body.detail?.message === "string"
          ? body.detail.message
          : JSON.stringify(body.detail ?? body);
    code =
      typeof body.code === "string"
        ? body.code
        : typeof body.detail?.code === "string"
          ? body.detail.code
          : undefined;
    category =
      typeof body.category === "string"
        ? body.category
        : typeof body.detail?.category === "string"
          ? body.detail.category
          : undefined;
    errorId = typeof body.error_id === "string" ? body.error_id : undefined;
  } catch {
    /* keep default */
  }
  let message = detail;
  if (code && errorId && !detail.includes(errorId)) {
    message = `${detail}（${category ?? ""}${category ? " · " : ""}${code} · ${errorId}）`;
  }
  throw new ApiError(resp.status, message, code, category, errorId, rawDetail);
}

async function request(
  method: string,
  path: string,
  body?: unknown,
  options?: {
    form?: FormData;
    approvalToken?: string;
    _retried?: boolean;
    _sessionRefreshed?: boolean;
  },
): Promise<any> {
  const mutating = method !== "GET" && method !== "HEAD";
  await ensureSession(mutating && !options?._sessionRefreshed);
  const isForm = Boolean(options?.form);
  const headers = baseHeaders(
    !isForm && body !== undefined
      ? { "Content-Type": "application/json" }
      : undefined,
    options?.approvalToken,
  );
  const resp = await fetch(`/api${path}`, {
    method,
    headers,
    body: isForm
      ? options?.form
      : body !== undefined
        ? JSON.stringify(body)
        : undefined,
  });

  if (resp.status === 202) {
    const payload = await resp.json();
    if (payload?.status === "waiting_approval") {
      if (options?._retried) {
        throw new ApiError(403, "批准后仍未通过，请刷新页面后重试");
      }
      if (!payload.approval_token) {
        throw new ApiError(403, "需要批准但未返回令牌，请在控制台确认后重试");
      }
      const approved = await requestCapabilityApproval(payload);
      if (!approved) throw new ApiError(403, "已取消操作");
      return request(method, path, body, {
        ...options,
        approvalToken: String(payload.approval_token),
        _retried: true,
        _sessionRefreshed: true,
      });
    }
    // 异步受理（如单视角重做）：直接返回 payload，不再走 handle
    return payload;
  }

  return handle(resp);
}

function get(path: string): Promise<any> {
  const active = inflightGets.get(path);
  if (active) return active;
  const pending = request("GET", path).finally(() => {
    if (inflightGets.get(path) === pending) inflightGets.delete(path);
  });
  inflightGets.set(path, pending);
  return pending;
}

function mutate(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
) {
  return request(method, path, body);
}

async function download(path: string): Promise<Blob> {
  await ensureSession();
  const resp = await fetch(`/api${path}`, { headers: baseHeaders() });
  if (!resp.ok) await handle(resp);
  return resp.blob();
}

export const api = {
  get,
  post: (path: string, body?: unknown) => mutate("POST", path, body),
  put: (path: string, body: unknown) => mutate("PUT", path, body),
  del: (path: string) => mutate("DELETE", path),
  download,
  upload: (path: string, form: FormData) =>
    request("POST", path, undefined, { form }),

  /* ── 便捷方法 ── */
  episodeGenerate: (episodeId: string) =>
    request("POST", `/episodes/${episodeId}/generate`),
  createVideoGenerationPlan: (
    episodeId: string,
    force = false,
  ): Promise<EpisodeVideoGenerationPlan> =>
    request("POST", `/episodes/${episodeId}/video-generation-plan`, { force }),
  getVideoGenerationPlan: (
    episodeId: string,
  ): Promise<EpisodeVideoGenerationPlan | null> =>
    request("GET", `/episodes/${episodeId}/video-generation-plan`),
  validateVideoGenerationPlan: (episodeId: string) =>
    request("POST", `/episodes/${episodeId}/video-generation-plan/validate`),
  reconcileVideoGenerationPlan: (
    episodeId: string,
    body?: { shot_id?: string; adopted_version_id?: string },
  ) =>
    request("POST", `/episodes/${episodeId}/video-generation-plan/reconcile`, body || {}),
  overrideVideoGenerationPlan: (
    episodeId: string,
    body: {
      shot_id: string;
      mode: VideoGenerationMode;
      video_input_intent?: VideoInputIntent | null;
      depends_on_shot_id?: string | null;
      required_assets: VideoPlanAssetRequirement[];
      reason: string;
    },
  ): Promise<EpisodeVideoGenerationPlan> =>
    request("POST", `/episodes/${episodeId}/video-generation-plan/override`, body),
  executeVideoGenerationPlan: (
    episodeId: string,
    planId: string,
    body?: Record<string, unknown>,
  ) =>
    request(
      "POST",
      `/episodes/${episodeId}/video-generation-plan/${planId}/execute`,
      body || {},
    ),
  stopEpisodeVideo: (episodeId: string) =>
    request("POST", `/episodes/${episodeId}/video/stop`) as Promise<{
      episode_id: string;
      paused_jobs: number;
      provider_may_continue: boolean;
      resume_supported: true;
      job_ids: string[];
    }>,
  resumeEpisodeVideo: (episodeId: string) =>
    request("POST", `/episodes/${episodeId}/resume`) as Promise<{
      resumed_jobs: number;
      budget_resumed_jobs: number;
      skipped_completed: number;
      enqueued: Array<{ job_id?: string; reused?: boolean; error?: unknown }>;
    }>,
  episodeVideoCompletion: (episodeId: string, body?: Record<string, unknown>) =>
    request("POST", `/episodes/${episodeId}/video-completion`, body || {}),
  getVideoCompletion: (episodeId: string) =>
    request("GET", `/episodes/${episodeId}/video-completion`),
  resetVideoCompletion: (episodeId: string) =>
    request("POST", `/episodes/${episodeId}/video-completion/reset`),
  projectVideoCompletion: (projectId: string, body?: Record<string, unknown>) =>
    request("POST", `/projects/${projectId}/video-completion`, body || {}),
  shotGenerate: (
    shotId: string,
    promptOverride?: string,
    reroll?: boolean,
    withCritique?: boolean,
    qualificationVersion?: string,
    idempotencyKey?: string,
  ) =>
    request("POST", `/shots/${shotId}/generate`, {
      prompt_override: promptOverride,
      reroll,
      with_critique: Boolean(withCritique),
      qualification_version: qualificationVersion,
      idempotency_key: idempotencyKey,
    }),
  stopShotVideo: (shotId: string): Promise<StopShotVideoResult> =>
    request("POST", `/shots/${shotId}/video/stop`),
  adoptVersion: (
    shotId: string,
    versionId: string,
    reason?: string,
    playbackRate?: number,
    qualificationVersion?: string,
    idempotencyKey?: string,
  ) =>
    request("POST", `/shots/${shotId}/adopt`, {
      version_id: versionId,
      reason,
      playback_rate: playbackRate ?? 1,
      qualification_version: qualificationVersion,
      idempotency_key: idempotencyKey,
    }),
  cancelShotAdoption: (shotId: string) =>
    request("POST", `/shots/${shotId}/adoption/cancel`),
  deleteVersion: (versionId: string) =>
    request("DELETE", `/versions/${versionId}`),
  discardReferenceImage: (versionId: string, refId: string) =>
    request("DELETE", `/versions/${versionId}/reference-images/${refId}`),
  restoreReferenceImage: (
    versionId: string,
    refId: string,
    overrideReason?: string,
  ) =>
    request(
      "POST",
      `/versions/${versionId}/reference-images/${refId}/restore`,
      {
        override_reason: overrideReason,
      },
    ),
  clearEpisodeArtifacts: (episodeId: string) =>
    request("POST", `/episodes/${episodeId}/clear-artifacts`),
  clearShotArtifacts: (shotId: string) =>
    request("POST", `/shots/${shotId}/clear-artifacts`),
  clearEpisodeVideos: (episodeId: string) =>
    request("POST", `/episodes/${episodeId}/videos/clear`),
  clearShotReferences: (shotId: string) =>
    request("POST", `/shots/${shotId}/references/clear`),
  clearShotVideos: (shotId: string) =>
    request("POST", `/shots/${shotId}/videos/clear`),
  getReviewContext: (episodeId: string) =>
    request(
      "GET",
      `/episodes/${episodeId}/review-context`,
    ) as Promise<ReviewWallContext>,
  archiveVersion: (versionId: string, reason?: string) =>
    request("POST", `/versions/${versionId}/archive`, { reason }),
  unarchiveVersion: (versionId: string) =>
    request("DELETE", `/versions/${versionId}/archive`),
  /* 场景图素材库 */
  sceneBiblePreview: (projectId: string) =>
    request("POST", `/projects/${projectId}/scene-bible/preview`) as Promise<{
      project_id: string;
      scenes: Scene[];
      precheck: SceneCostPrecheck;
      generates_images: false;
    }>,
  sceneBiblePrecheck: (projectId: string, scenes: Scene[]) =>
    request("POST", `/projects/${projectId}/scene-bible/precheck`, {
      scenes,
    }) as Promise<SceneCostPrecheck>,
  genSceneBible: (
    projectId: string,
    body: {
      scenes: Scene[];
      confirm: true;
      quote_id: string;
    },
  ) => request("POST", `/projects/${projectId}/scene-bible`, body),
  sceneRefsPrecheck: (
    projectId: string,
    body?: {
      scenes?: string[];
      resume?: boolean;
      view_role?: string;
      scene_reference_id?: string;
      action?: string;
    },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/scene-refs/precheck`,
      body || {},
    ) as Promise<SceneCostPrecheck>,
  sceneRefsGaps: (projectId: string) =>
    request(
      "GET",
      `/projects/${projectId}/scene-refs/gaps`,
    ) as Promise<SceneGapScan>,
  sceneRefsProgress: (projectId: string) =>
    request(
      "GET",
      `/projects/${projectId}/scene-refs/progress`,
    ) as Promise<SceneRefsProgress>,
  genSceneRefs: (
    projectId: string,
    body: {
      scenes?: string[];
      resume?: boolean;
      confirm: true;
      quote_id: string;
    },
  ) => request("POST", `/projects/${projectId}/scene-refs`, body),
  cancelSceneRefs: (projectId: string) =>
    request("POST", `/projects/${projectId}/scene-refs/cancel`),
  editScenePrompt: (
    projectId: string,
    sceneName: string,
    scenePrompt: string,
  ) =>
    request(
      "PUT",
      `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/prompt`,
      {
        scene_prompt: scenePrompt,
      },
    ),
  editSceneAnchor: (
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
      forbidden_elements?: string[];
    },
  ) =>
    request(
      "PUT",
      `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}`,
      body,
    ),
  regenerateCharacterView: (
    projectId: string,
    characterName: string,
    portraitId: string,
    viewRole: string,
    body?: {
      confirm?: boolean;
      quote_id?: string;
      idempotency_key?: string;
    },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/characters/${encodeURIComponent(characterName)}/portraits/${portraitId}/views/${encodeURIComponent(viewRole)}/regenerate`,
      body || {},
    ),
  regenerateSceneView: (
    projectId: string,
    sceneName: string,
    sceneRefId: string,
    viewRole: string,
    body: {
      confirm: true;
      quote_id: string;
    },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/refs/${sceneRefId}/views/${encodeURIComponent(viewRole)}/regenerate`,
      body,
    ),
  cancelSceneViewRegeneration: (
    projectId: string,
    sceneName: string,
    sceneRefId: string,
    viewRole: string,
  ) =>
    request(
      "POST",
      `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/refs/${sceneRefId}/views/${encodeURIComponent(viewRole)}/regenerate/cancel`,
    ),
  startSceneReview: (
    projectId: string,
    body?: { shadow_mode?: boolean; block_new_references?: boolean },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/scene-reviews`,
      body || {},
    ) as Promise<SceneReviewBatch>,
  listSceneReviews: (projectId: string) =>
    request("GET", `/projects/${projectId}/scene-reviews`) as Promise<{
      project_id: string;
      items: SceneReviewBatch[];
    }>,
  getSceneReview: (projectId: string, batchId: string) =>
    request(
      "GET",
      `/projects/${projectId}/scene-reviews/${batchId}`,
    ) as Promise<SceneReviewBatch>,
  disposeSceneReviewItem: (
    projectId: string,
    batchId: string,
    itemId: string,
    body: {
      action:
        | "accepted_risk"
        | "repair_planned"
        | "repaired"
        | "false_positive"
        | "deferred";
      reason: string;
    },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/scene-reviews/${batchId}/items/${itemId}/disposition`,
      body,
    ),
  adoptSceneCandidate: (
    projectId: string,
    sceneName: string,
    artifactId: string,
    reason?: string,
  ) =>
    request(
      "POST",
      `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/candidates/${encodeURIComponent(artifactId)}/adopt`,
      {
        reason: reason || "人工采纳候选",
      },
    ),
  reviewSceneCandidate: (
    projectId: string,
    sceneName: string,
    artifactId: string,
  ) =>
    request(
      "POST",
      `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/candidates/${encodeURIComponent(artifactId)}/review`,
    ) as Promise<{
      reviewed: boolean;
      image_regenerated: false;
      artifact_id: string;
      evaluation: ArtifactEvidence["evaluations"][number];
      qa: Record<string, unknown>;
    }>,
  manualReviewSceneCandidate: (
    projectId: string,
    sceneName: string,
    artifactId: string,
    body: {
      confirmations: {
        person_free: boolean;
        watermark_free: boolean;
        forbidden_text_free: boolean;
        space_type_matches: boolean;
      };
      reason: string;
    },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/candidates/${encodeURIComponent(artifactId)}/manual-review`,
      body,
    ),
  rollbackSceneReference: (
    projectId: string,
    sceneName: string,
    sceneRefId: string,
    reason?: string,
  ) =>
    request(
      "POST",
      `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/refs/${sceneRefId}/rollback`,
      {
        reason: reason || "回滚到历史通过场景包",
      },
    ),
  bibleImpactPreview: (
    projectId: string,
    body: {
      bible: unknown;
      expected_version?: number | null;
    },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/bible/impact-preview`,
      body,
    ) as Promise<BibleImpactPreview>,
  bibleGeneratePrecheck: (projectId: string) =>
    request(
      "POST",
      `/projects/${projectId}/bible/generate-precheck`,
    ) as Promise<
      RefsCostPrecheck & {
        estimated_duration_min?: number[];
        estimate_note?: string;
        character_names?: string[];
      }
    >,
  refsPrecheck: (
    projectId: string,
    body?: {
      character?: string;
      characters?: string[];
      resume?: boolean;
      view_role?: string;
    },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/refs/precheck`,
      body || {},
    ) as Promise<RefsCostPrecheck>,
  refsGaps: (projectId: string) =>
    request("GET", `/projects/${projectId}/refs/gaps`) as Promise<{
      missing_count: number;
      image_count: number;
      items: Array<Record<string, unknown>>;
      precheck: RefsCostPrecheck;
    }>,
  refsProgress: (projectId: string) =>
    request("GET", `/projects/${projectId}/refs/progress`) as Promise<{
      total: number;
      ready: number;
      failed: number;
      missing: number;
      refs_status?: string;
      refs_target?: string | null;
      items: Array<{
        character: string;
        status: string;
        missing_views?: string[];
        current?: boolean;
        pack_status?: string;
      }>;
      updated_at?: number;
    }>,
  saveBibleDraft: (
    projectId: string,
    body: { bible: unknown; expected_version?: number | null },
  ) => request("POST", `/projects/${projectId}/bible/draft`, body),
  getBibleDraft: (projectId: string) =>
    request("GET", `/projects/${projectId}/bible/draft`) as Promise<{
      draft: unknown;
      updated_at?: number | null;
      bible_version: number;
    }>,
  saveCharacter: (
    projectId: string,
    name: string,
    body: {
      character: Character;
      expected_version?: number | null;
      impact_preview_fingerprint?: string;
      confirm?: boolean;
    },
  ) =>
    request(
      "PUT",
      `/projects/${projectId}/characters/${encodeURIComponent(name)}`,
      body,
    ) as Promise<{
      bible_version?: number;
      character?: Character;
      impact?: BibleImpactPreview;
    }>,
  listPortraitCandidates: (projectId: string, name: string) =>
    request(
      "GET",
      `/projects/${projectId}/characters/${encodeURIComponent(name)}/portrait-candidates`,
    ) as Promise<
      | {
          items?: CharacterPortraitCandidate[];
          candidates?: CharacterPortraitCandidate[];
        }
      | CharacterPortraitCandidate[]
    >,
  adoptPortraitCandidate: (
    projectId: string,
    name: string,
    portraitId: string,
    body: {
      reason: string;
      bypass_soft?: boolean;
    },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/characters/${encodeURIComponent(name)}/portraits/${encodeURIComponent(portraitId)}/adopt`,
      body,
    ),
  rollbackPortraitCandidate: (
    projectId: string,
    name: string,
    portraitId: string,
  ) =>
    request(
      "POST",
      `/projects/${projectId}/characters/${encodeURIComponent(name)}/portraits/${encodeURIComponent(portraitId)}/rollback`,
    ),
  listAutoChanges: (projectId: string) =>
    request("GET", `/projects/${projectId}/auto-changes`) as Promise<{
      items: AutoChangeItem[];
    }>,
  decideAutoChange: (
    projectId: string,
    changeId: string,
    decision: string,
    options?: {
      reason?: string;
      merge_into_character?: string;
      merge_into_scene?: string;
      ep_start?: number;
    },
  ) =>
    request(
      "POST",
      `/projects/${projectId}/auto-changes/${encodeURIComponent(changeId)}/decide`,
      {
        decision,
        ...(options || {}),
      },
    ),
  staleAssetsPreview: (episodeId: string) =>
    request("GET", `/episodes/${episodeId}/stale-assets-preview`) as Promise<{
      episode_id: string;
      stale_count: number;
      preview_version: string;
      estimated_cost_cny: number;
      qualification: ReviewUpstreamSnapshot;
      shots: Array<{
        shot_id: string;
        shot_no: number;
        adopted_version_id?: string | null;
        reasons: string[];
        reason_labels: string[];
        hint?: string;
        estimated_cost_cny: number;
        storyboard_artifact_id?: string | null;
        current_storyboard_artifact_id?: string | null;
        asset_qualification?: Array<{
          ref_id?: string;
          entity_type?: string;
          entity_name?: string;
          asset_version?: string;
          rule_version?: string;
          gate_status?: string;
          hard_failures?: string[];
        }>;
        asset_soft_warnings?: Array<{ ref_id?: string; warning?: string }>;
        rule_versions?: string[];
      }>;
    }>,
  repairStaleAssets: (
    episodeId: string,
    shotIds?: string[],
    previewVersion?: string,
    qualificationVersion?: string,
  ) =>
    request("POST", `/episodes/${episodeId}/repair-stale-assets`, {
      confirm: true,
      shot_ids: shotIds,
      preview_version: previewVersion,
      qualification_version: qualificationVersion,
      idempotency_key: previewVersion
        ? `repair-stale:${episodeId}:${previewVersion}`
        : undefined,
    }) as Promise<{
      queued: number;
      shot_ids: string[];
      errors: Array<{ shot_id: string; shot_no: number; error: string }>;
      message: string;
      preview_version: string;
    }>,
};

export interface AutoChangeItem {
  id: string;
  kind?: string;
  status?: string;
  character?: string;
  scene?: string;
  ep_start?: number;
  reason?: string | null;
  change_dimensions?: string[];
  persistence?: string;
  pack_status?: string;
  created_at?: number;
  source?: string;
  decision_reason?: string;
  payload?: {
    source_episode?: number;
    source_episode_label?: string;
    evidence_fragments?: string[] | string;
    scene?: Scene;
    duplicate_candidates?: string[];
  };
}

export interface BibleImpactPreview {
  project_id: string;
  bible_version: number;
  computed_at: number;
  fingerprint: string;
  change_types: string[];
  style_changed: boolean;
  stale_descendant_ids: string[];
  stale_count: number;
  stale_assets?: Array<{
    id: string;
    type: string;
    status: string;
    scope_type?: string | null;
    scope_id?: string | null;
  }>;
  stale_assets_truncated?: boolean;
  by_artifact_type?: Record<string, number>;
  paid_assets?: { character_portraits?: number; scene_references?: number };
  rebuild?: {
    image_count: number;
    unit_price_cny: number;
    estimated_cost_cny: number;
    max_retry_budget_cny: number;
    note?: string;
  };
  requires_reconfirm?: boolean;
  paid_media_invalidated?: boolean;
  old_asset_policy?: string;
}

export interface RefsCostPrecheck {
  quote_id: string;
  computed_at: number;
  quote_expires_at: number;
  project_id: string;
  action: string;
  character?: string | null;
  view_role?: string | null;
  character_count: number;
  views_per_character: number;
  image_count: number;
  unit_price_cny: number;
  estimated_cost_cny: number;
  max_retry_budget_cny: number;
  budget_cap_cny: number;
  scope: Array<Record<string, unknown>>;
  old_asset_policy?: string;
  idempotency_hint?: string;
  stop_policy?: string;
}

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

export interface SceneReviewBatch {
  id: string;
  project_id: string;
  status: string;
  denominator: number;
  evaluated: number;
  passed: number;
  warning: number;
  hard_failed: number;
  unverified: number;
  coverage?: number;
  cutoff_at?: number | null;
  shadow_mode?: number | boolean;
  task_id?: string;
  disposition_count?: number;
  items?: Array<{
    id: string;
    scene_name: string;
    result_status: string;
    old_status?: string;
    disposition?: string | Record<string, unknown>;
    evidence?: {
      hard_failures?: string[];
      warnings?: string[];
      uncertainties?: string[];
      [key: string]: unknown;
    };
  }>;
}

export interface RunSummary {
  id: string;
  workflow_type: string;
  scope_type: string;
  scope_id: string;
  status: string;
  current_step_key?: string | null;
  cost_cny: number;
  budget_limit_cny?: number | null;
  started_at?: number | null;
  updated_at: number;
  finished_at?: number | null;
  failure_code?: string | null;
  failure_message?: string | null;
  resume_from_step?: string | null;
}

export interface StepRun {
  id: string;
  run_id: string;
  step_key: string;
  iteration_no: number;
  status: string;
  decision?: string | null;
  exit_reason?: string | null;
  started_at?: number | null;
  finished_at?: number | null;
  latency_ms: number;
  output_artifact_id?: string | null;
  error_message?: string | null;
}

export interface RunEvent {
  id: string;
  run_id: string;
  step_run_id?: string | null;
  ts: number;
  event_type: string;
  severity: string;
  message: string;
  payload?: Record<string, unknown>;
}

export interface EvidenceIssue {
  code: string;
  severity: "info" | "warning" | "blocker";
  subject: string;
  message: string;
  repair_hint?: string | null;
  repairable: boolean;
}

interface EvaluationSummary {
  id: string;
  evaluator_type: string;
  evaluator_name: string;
  evaluator_version: string;
  status: string;
  hard_gate_passed: number | boolean;
  score?: number | null;
  issues?: EvidenceIssue[];
  evidence?: Record<string, unknown>;
  recovered: number | boolean;
}

export interface ArtifactEvidence {
  id: string;
  type: string;
  version: number;
  status: string;
  trust_level: string;
  content_hash: string;
  contract_version?: string | null;
  prompt_version?: string | null;
  parent_artifact_ids?: string[];
  stale_reason?: string | null;
  evaluations: EvaluationSummary[];
}

interface Dialogue {
  speaker: string;
  line: string;
  emotion: string;
}

interface AudioTimelineItem {
  start_s: number;
  end_s: number;
  type: string;
  speaker_id?: string | null;
  text: string;
  lip_sync?: boolean;
  emotion?: string;
}

interface RequiredText {
  surface: string;
  exact_text: string;
  strategy?: "audio_only" | "deterministic_insert" | "embedded_prop" | "none" | string;
  delivery_owner_shot_no?: number | null;
  appear_start_s?: number;
  stable_until_s?: number | null;
  style?: string;
  allow_other_text?: boolean;
  max_other_text?: number;
  font_role?: string;
  reading_priority?: string;
}

interface ContinuityState {
  scene?: {
    scene_revision_id?: string;
    time_of_day?: string;
    lighting_state?: string;
    axis_id?: string;
    landmarks?: Record<string, string>;
  };
  characters?: Record<string, {
    look_revision_id?: string;
    outfit_revision_id?: string;
    visibility?: string;
    screen_side?: string;
    pose?: string;
    facing?: string;
    gaze_target?: string;
    left_hand?: string;
    right_hand?: string;
  }>;
  props?: Record<string, {
    canonical_name?: string;
    revision_id?: string;
    owner?: string;
    location?: string;
    form?: string;
    visibility?: string;
    text_state?: string;
    required?: boolean;
  }>;
}

export interface ScriptScene {
  scene_no: number;
  scene_heading: string;
  story_function: string;
  characters: string[];
  summary: string;
  conflict?: string;
  turn?: string;
  source_basis?: string;
}

export interface PlotSpineBeat {
  beat_id: string;
  who?: string;
  does?: string;
  turn?: string;
  must_keep?: boolean;
}

export interface PlotSpine {
  episode_premise?: string;
  spine_beats?: PlotSpineBeat[];
  must_keep_ending?: string;
  drop_list?: string[];
}

export interface KeyDialogueTurn {
  speaker: string;
  line: string;
  function:
    | "trigger"
    | "announcement"
    | "question"
    | "response"
    | "decision"
    | "statement"
    | string;
  source_text: string;
}

export interface KeyDialogueChain {
  chain_id: string;
  topic: string;
  turns: KeyDialogueTurn[];
}

export interface EpisodeScreenplay {
  id?: string | null;
  episode_no: number;
  mode?: "full_script" | string;
  title?: string;
  source_text_range?: string;
  logline?: string;
  script_format_note?: string;
  dramatic_question?: string;
  protagonist_goal?: string;
  obstacle?: string;
  stakes?: string;
  key_lines?: string[];
  dialogue_chains?: KeyDialogueChain[];
  key_plot_points?: string[];
  plot_spine?: PlotSpine | null;
  scene_outline?: ScriptScene[];
  full_script_text?: string;
  character_state_changes?: string[];
  emotional_curve?: string;
  ending_hook?: string;
  source_basis?: string;
  adaptation_direction?: string;
  opening?: string;
  development?: string;
  conflict?: string;
  climax?: string;
  episode_premise?: string;
  events?: Record<string, unknown>[];
  information_ledger?: Record<string, unknown>[];
  voice_bible?: Record<string, unknown>[];
  approved_adaptations?: string[];
  forbidden_additions?: string[];
  narrative_plan?: Record<string, unknown> | null;
  created_at?: number | null;
  updated_at?: number | null;
}

export interface NarrativeContractSummary {
  contract_version: string;
  proposition_count: number;
  event_count: number;
  audience_prior_count: number;
  experience_intent_count: number;
  assimilation_task_count: number;
}

export interface NarrativeReviewSummary {
  artifact_id: string;
  version: number;
  status: string;
  decision: string | null;
  low_percentile: Record<string, unknown>;
  inference_variance: number;
  reason: string;
}

export interface ShotContribution {
  shot_contribution_id: string;
  experience_intent_ids: string[];
  target_delta_ids: string[];
  assimilation_task_ids: string[];
  evidence_ids: string[];
  story_delta_fact_ids: string[];
  character_state_delta_ids: string[];
  audience_state_delta_ids: string[];
  affective_delta: Record<string, unknown>;
  spatial_temporal_delta: Record<string, unknown>;
  dramatic_pressure_delta: number;
}

export interface ShotCapacityBudget {
  action_phase_s: number;
  spoken_and_text_s: number;
  attention_switch_s: number;
  inference_processing_s: number;
  reaction_registration_s: number;
  spatial_reorientation_s: number;
  entry_exit_settle_s: number;
  other_s: number;
  other_reason?: string | null;
}

export interface AudienceStatePathRef {
  audience_prior_id: string;
  audience_state_in_id: string;
  audience_state_out_target_id: string;
}

export interface NarrativeBoundaryContract {
  boundary_id: string;
  previous_shot_id: string;
  next_shot_id: string;
  narrative_relation: string;
  required_state_invariants: string[];
  allowed_state_deltas: string[];
  state_delta_transitions: Array<{
    transition_id: string;
    basis_type: string;
    source_fact_id?: string | null;
    target_fact_id?: string | null;
    basis_action_phase_id?: string | null;
    custom_basis?: string | null;
    reason: string;
  }>;
  forbidden_replay_action_ids: string[];
  handoff_action_phase_id?: string | null;
  spatial_orientation_contract: Record<string, unknown>;
  temporal_orientation_contract: Record<string, unknown>;
  audience_state_handoffs: Record<string, unknown>[];
  affective_handoff: Record<string, unknown>;
  cut_motivation: string;
}

export type VideoGenerationMode =
  | "REFERENCE_IMAGE_MODE"
  | "FIRST_LAST_FRAME_MODE"
  | "VIDEO_INPUT_MODE";

export type VideoInputIntent =
  | "CONTINUE_PREVIOUS_TAKE"
  | "MOTION_REFERENCE"
  | "CAMERA_REFERENCE"
  | "RHYTHM_REFERENCE"
  | "AUDIO_REFERENCE";

export type VideoPlanAssetSource =
  | "ASSET_REVISION"
  | "STATIC_BOUNDARY_ASSET"
  | "PREVIOUS_ADOPTED_TAIL"
  | "PREVIOUS_ADOPTED_VIDEO";

export type VideoPlanAssetRole =
  | "identity_reference"
  | "scene_reference"
  | "prop_reference"
  | "style_reference"
  | "first_frame"
  | "last_frame"
  | "previous_adopted_video"
  | "motion_reference_video"
  | "camera_reference_video"
  | "audio_reference_video";

export interface VideoPlanAssetRequirement {
  role: VideoPlanAssetRole;
  source: VideoPlanAssetSource;
  asset_revision_id?: string | null;
  source_shot_id?: string | null;
  fingerprint?: string | null;
}

export interface ShotVideoRelations {
  temporal: "same_moment" | "elapsed" | "jump" | "new_domain" | "unknown";
  spatial: "same_space" | "adjacent_space" | "new_space" | "unknown";
  edit:
    | "continuous_take"
    | "match_cut"
    | "angle_cut"
    | "reaction_cut"
    | "reverse_angle"
    | "insert_cut"
    | "montage"
    | "scene_cut"
    | "unknown";
  action:
    | "continues_same_action"
    | "starts_new_action"
    | "shows_result"
    | "observes_result"
    | "no_action"
    | "unknown";
}

export interface ShotVideoGenerationPlan {
  shot_plan_id: string;
  episode_video_plan_id: string;
  plan_revision: number;
  source_storyboard_revision_id: string;
  shot_id: string;
  published_shot_id: string;
  shot_no: number;
  mode: VideoGenerationMode;
  planned_mode: VideoGenerationMode | null;
  actual_mode: VideoGenerationMode | null;
  video_input_intent: VideoInputIntent | null;
  depends_on_shot_id: string | null;
  relations: ShotVideoRelations;
  state_dependency: "none" | "start_only" | "start_and_end" | "full_trajectory";
  motion_dependency: "none" | "pose" | "trajectory" | "camera" | "rhythm" | "audio";
  required_assets: VideoPlanAssetRequirement[];
  reason_codes?: string[];
  confidence: number;
  unknown_dimensions: string[];
  fallback_order: VideoGenerationMode[];
  max_attempts: number;
  max_cost: number;
  timeout_s: number;
  estimated_latency_ms: number;
  estimated_cost: number;
  critical_path_group: string | null;
  capability_snapshot_id: string;
  input_revision_fingerprints: Record<string, string>;
  status: string;
  degraded_from_mode: VideoGenerationMode | null;
  degraded_to_mode: VideoGenerationMode | null;
  degraded_reason: string | null;
}

export interface EpisodeVideoGenerationPlan {
  episode_video_plan_id: string;
  episode_id: string;
  plan_revision: number;
  source_storyboard_revision_id: string;
  published_storyboard_artifact_id: string;
  published_storyboard_artifact_hash: string;
  completion_certificate_id: string;
  narrative_review_artifact_id: string;
  release_qualification_hash: string;
  capability_snapshot_id: string;
  status: "draft" | "valid" | "blocked" | "superseded" | "stale";
  planner_provider: string;
  planner_model: string;
  planner_prompt_fingerprint: string;
  shots: ShotVideoGenerationPlan[];
  blockers: Array<Record<string, unknown>>;
  estimated_latency_ms: number;
  estimated_cost: number;
  critical_path_latency_ms: number;
  safe_parallelism_ratio: number;
  created_at: number;
}

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

export interface ShotVersion {
  id: string;
  version_no: number;
  prompt_text: string;
  status: string;
  error?: string;
  video_url?: string;
  qa?: {
    overall: number;
    issues: string[];
    failure_types?: string[];
    observed_state_out?: string;
    start_state_match?: number | boolean;
    end_state_match?: number | boolean;
  } | null;
  cost_cny: number;
  latency_s: number;
  artifact_id?: string | null;
  adoption_reason?: string | null;
  playback_rate?: number | null;
  technical_validation_json?: string | null;
  created_at?: number | null;
  image_inputs?: {
    mode?: VideoGenerationMode;
    planned_mode?: VideoGenerationMode;
    actual_mode?: VideoGenerationMode | null;
    video_input_intent?: VideoInputIntent | null;
    episode_video_plan_id?: string | null;
    shot_plan_id?: string | null;
    plan_revision?: number | null;
    capability_snapshot_id?: string | null;
    after_shot_id?: string | null;
    plan_status?: string | null;
    degraded_reason?: string | null;
    stale?: boolean;
    stale_reason?: string | null;
    reference_image_used?: boolean;
    reference_images?: ReferenceImage[];
    reference_failure_logs?: {
      type?: string;
      reason?: string;
      error?: string;
      fallback?: string;
      qa?: { overall?: number; issues?: string[] };
    }[];
    fallback_reason?: string | null;
    retry_reason?: string | null;
  };
}

export interface ReviewUpstreamSnapshot {
  episode_id: string;
  episode_status: string;
  published_screenplay_artifact_id?: string | null;
  confirmed_storyboard_artifact_id?: string | null;
  active_upstream_runs: Array<{
    kind: string;
    run_id?: string | null;
    status: string;
    stage?: string | null;
  }>;
  qualification_version: string;
  eligible_for_production: boolean;
  blockers: string[];
  assets: {
    eligible: boolean;
    status: string;
    checked_inputs: number;
    blockers: Array<Record<string, unknown>>;
    soft_warnings: Array<Record<string, unknown>>;
  };
  server_time: number;
}

export interface NumberConstraint {
  type: "number";
  unit: string;
  default: number;
  min: number;
  max: number;
  step: number;
  finite: boolean;
}

export interface ReviewWallContext {
  episode_id: string;
  object_version: string;
  upstream: ReviewUpstreamSnapshot;
  archived_versions: Record<
    string,
    { version_id: string; reason?: string | null; archived_at: number }
  >;
  authorization_constraints: {
    budget_cap_cny: NumberConstraint;
    wall_clock_cap_s: NumberConstraint;
    add_budget_cny: NumberConstraint;
    add_wall_clock_s: NumberConstraint;
  };
  server_time: number;
}

export interface ShotPipelineStatus {
  /** 持久化媒体任务已创建；不等于供应商已接单。 */
  task_accepted?: boolean;
  task_id?: string | null;
  task_created_at?: number | null;
  task_updated_at?: number | null;
  next_retry_at?: number | null;
  retry_count?: number;
  /** 供应商已返回 task id，表示实际生成请求已下发上游。 */
  provider_submitted?: boolean;
  video_status?:
    | "pending_generation"
    | "generating"
    | "pending_adoption"
    | "adopted"
    | "generation_failed";
  video_status_label?: "待生成" | "生成中" | "待采纳" | "已采纳" | "生成失败";
  pipeline_status: string;
  pipeline_stage?: string | null;
  current_stage?: string | null;
  stage_label?: string | null;
  stage_progress?: {
    current?: number;
    total?: number;
    unit?: string;
    attempt?: number;
    attempt_limit?: number;
  } | null;
  queue_position?: number | null;
  provider_elapsed_s?: number | null;
  stage_elapsed_s?: number | null;
  stage_started_at?: number | null;
  reference_progress?: { done: number; total: number } | null;
  candidate_count: number;
  retake_count: number;
  attempt?: number;
  attempt_limit?: number;
  blocked_reason?: string | null;
  reason_code?: string | null;
  reason_text?: string | null;
  scheduler_lane?: string | null;
  blocked_by_shot_id?: string | null;
  next_stage?: string | null;
  state_revision?: number;
  estimated_start_at?: number | null;
  estimated_finish_at?: number | null;
}

export interface EpisodePipelineSummary {
  shots_total: number;
  adopted: number;
  with_candidate: number;
  upstream_generating: number;
  preparing_references: number;
  video_ready?: number;
  waiting_continuity?: number;
  video_qa?: number;
  queued: number;
  waiting_human: number;
  failed?: number;
  paused?: number;
  video_status_counts?: {
    pending_generation: number;
    generating: number;
    pending_adoption: number;
    adopted: number;
    generation_failed: number;
  };
}

export interface StopShotVideoResult {
  shot_id: string;
  stopped_count: number;
  provider_may_continue: boolean;
  resume_supported: false;
  jobs: {
    job_id: string;
    status: string;
    provider_may_continue: boolean;
    cancelled: boolean;
  }[];
}

export interface Shot {
  id: string;
  episode_id: string;
  script_id?: string | null;
  shot_no: number;
  duration_s: number;
  shot_size: string;
  camera_move: string;
  scene_time: string;
  scene_name: string;
  scene_setting: string;
  characters: string[];
  action_desc: string;
  first_frame_desc: string;
  last_frame_desc: string;
  source_excerpt: string;
  narration: string | null;
  dialogues: Dialogue[];
  transition: string;
  spoken_content_chars?: number;
  spoken_limit?: number;
  has_legacy_narration?: boolean;
  spoken_contract_status?: "coherent" | "conflict" | "legacy" | string;
  spine_beat_ids?: string[];
  key_line_ids?: string[];
  information_ids?: string[];
  continuity_from_prev: number;
  adopted_version_id: string | null;
  story_event_id?: string;
  purpose?: string;
  new_information_ids?: string[];
  new_information_items?: {
    info_id: string;
    content: string;
    source?: "ledger" | "derived";
  }[];
  reinforcement_info_ids?: string[];
  state_in?: string;
  primary_action?: string;
  state_out?: string;
  observed_state_out?: string;
  continuity_mode?: string;
  characters_visible?: string[];
  audio_cast?: string[];
  audio_timeline?: AudioTimelineItem[];
  required_text?: RequiredText | null;
  continuity_state_in?: ContinuityState;
  continuity_state_out?: ContinuityState;
  reference_roles?: string[];
  do_not_repeat?: string[];
  risk_tags?: string[];
  prompt_contract_version?: string;
  legacy_unvalidated?: boolean;
  shot_id?: string;
  scene_id?: string;
  event_ids?: string[];
  primary_action_id?: string | null;
  supporting_action_ids?: string[];
  action_phase_ids?: string[];
  visible_entity_ids?: string[];
  offscreen_action_actor_ids?: string[];
  offscreen_action_target_ids?: string[];
  capacity_budget?: ShotCapacityBudget | null;
  shot_contribution?: ShotContribution | null;
  audience_state_paths?: AudienceStatePathRef[];
  planned_state_in_fact_ids?: string[];
  planned_delta_add_fact_ids?: string[];
  planned_delta_remove_fact_ids?: string[];
  planned_state_out_fact_ids?: string[];
  completed_before_action_ids?: string[];
  completed_before_action_phase_ids?: string[];
  reserved_future_event_ids?: string[];
  readability_window_ids?: string[];
  narrative_boundary_from_previous?: NarrativeBoundaryContract | null;
  mode_plan?: ShotVideoGenerationPlan | null;
  is_final?: boolean;
  preflight_errors?: string[];
  qa_warnings?: string[];
  prompt_preview?: string;
  est_cost_cny: number;
  versions: ShotVersion[];
  version_count?: number;
  video_stale: boolean;
  video_status?:
    | "pending_generation"
    | "generating"
    | "pending_adoption"
    | "adopted"
    | "generation_failed"
    | null;
  video_grade?: "A" | "B" | "C" | null;
  fallback_reason?: string | null;
  continuity_degraded?: boolean;
  storyboard_artifact_id?: string | null;
  storyboard_evidence?: ArtifactEvidence | null;
  source_binding?: {
    shot_id?: string;
    chapter_id: number;
    chapter_idx: number;
    source_version_hash: string;
    start_offset: number;
    end_offset: number;
    excerpt_hash: string;
  } | null;
  pipeline?: ShotPipelineStatus | null;
}

export interface Episode {
  id: string;
  episode_no: number;
  title: string;
  hook: string;
  cliffhanger: string;
  synopsis: string;
  source_chapters: number[];
  target_duration_s: number;
  status: string;
  script_error?: string;
  storyboard_warning?: string | null;
  cost_cny: number;
  cost_limit_cny?: number;
  screenplay_status: string;
  screenplay_error?: string | null;
  screenplay_updated_at?: number | null;
  screenplay?: EpisodeScreenplay | null;
  narrative_contract_summary?: NarrativeContractSummary | null;
  narrative_review_summary?: NarrativeReviewSummary | null;
  narrative_metrics?: Record<string, unknown> | null;
  scene_options?: string[];
  source_dialogue_occurrences?: DialogueOccurrence[] | null;
  required_dialogue_lines?: string[];
  required_dialogue_occurrence_ids?: string[];
  shot_count?: number;
  video_count?: number;
  pending_adoption_count?: number;
  failed_count?: number;
  screenplay_artifact_id?: string | null;
  screenplay_evidence?: ArtifactEvidence | null;
  screenplay_state?: ScreenplayState | null;
  screenplay_production?: {
    revision_id?: string;
    operation: "baseline" | "repair";
    phase: string;
    baseline_done: boolean;
    first_evaluation_done: boolean;
    task_active: boolean;
    can_resume_repair: boolean;
    activation_count?: number;
    patch_count?: number;
    open_issue_count?: number;
    yield_reason?: string;
  } | null;
  shots?: Shot[];
  storyboard_planned_shots?: number | null;
  storyboard_artifact_id?: string | null;
  storyboard_evidence?: ArtifactEvidence | null;
  storyboard_status?: StoryboardStatus | null;
  active_storyboard_run_id?: string | null;
  active_video_run_id?: string | null;
  video_completion_mode?: string | null;
  video_supervisor?: Record<string, unknown> | null;
  supervisor?: {
    phase: string;
    repair_epoch: number;
    lifetime_repair_count?: number;
    activation_no?: number;
    activation_attempt_count?: number;
    activation_attempt_limit?: number;
    validated_prefix_end: number;
    next_shot_no: number;
    expected_total: number;
    outcome?: string | null;
    strategy?: string;
    frontier?: number;
    issue_codes?: string[];
    last_repair?: Record<string, unknown> | null;
    pending_control?: { action: string; pending: boolean } | null;
  } | null;
  delivery_artifact_id?: string | null;
  delivery_status?: string;
  pipeline_summary?: EpisodePipelineSummary | null;
}

export interface DialogueOccurrence {
  id: string;
  text: string;
  order: number;
  offset: number;
  chapter?: number | null;
  paragraph: number;
  context: string;
  estimated_seconds: number;
  group_id?: string | null;
}

export interface ScreenplayState {
  version: number;
  code: string;
  message: string;
  recommended_action:
    | "generate_screenplay"
    | "stop_screenplay"
    | "resume_screenplay"
    | "generate_storyboard"
    | "resume_storyboard"
    | "view_storyboard"
    | "view_save_progress"
    | "view_cancel_progress"
    | "refresh";
  screenplay_status: string;
  storyboard_status: string;
  screenplay_run_id?: string | null;
  storyboard_run_id?: string | null;
  checkpoint_shot?: number | null;
  storyboard_running: boolean;
  publish_blocked: boolean;
  reason?: string;
}

export interface StoryboardStatus {
  contract_version: string;
  snapshot_version: number;
  state_fingerprint: string;
  state:
    | "no_screenplay"
    | "empty"
    | "running"
    | "paused"
    | "failed"
    | "ready_to_confirm"
    | "confirmed"
    | "syncing";
  headline: string;
  screenplay_available: boolean;
  task_phase?: string | null;
  planned_shots: number;
  produced_shots: number;
  validated_shots: number;
  /** 当前仍保留、可在页面审阅的草稿镜头数。 */
  draft_shots?: number;
  /** 连续通过结构检查、可作为安全恢复点保留的镜头数。 */
  safe_checkpoint_shots?: number;
  /** 安全恢复点之后、继续任务时可能更新的现有草稿镜头数。 */
  pending_revalidation_shots?: number;
  resume_from_shot?: number;
  /** 继续任务是生成后续镜头，还是只修复现有完整分镜。 */
  resume_mode?: "continue_generation" | "repair_existing" | null;
  final_shot_valid: boolean;
  hard_gates_passed: boolean;
  hard_gate_issue_count?: number;
  hard_gate_issues?: string[];
  feature_flags?: {
    safe_readonly: boolean;
    structure_edit: boolean;
    source_rebind: boolean;
  };
  confirmed: boolean;
  editable: boolean;
  confirmable: boolean;
  recommended_action:
    | "go_screenplay"
    | "generate_storyboard"
    | "view_progress"
    | "resume_storyboard"
    | "confirm_storyboard"
    | "go_review_wall"
    | "refresh_status";
  write_block_reason?: string | null;
}

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

export interface DeliveryPackage {
  package_id: string;
  artifact_id: string;
  trust_level: string;
  status: string;
  package_path: string;
  archive_path?: string;
  manifest: Record<string, unknown>;
  quality_report: Record<string, unknown>;
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
  included_shot_nos?: number[];
  partial?: boolean;
  final_video_stale?: boolean;
  playback_rates?: Record<string, number>;
  final_edit?: Record<string, unknown>;
  note?: string;
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
}

export interface ChapterContent {
  idx: number;
  title: string;
  content: string;
  prev_idx: number | null;
  next_idx: number | null;
  first_idx: number;
  last_idx: number;
  total: number;
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
  forbidden_elements?: string[];
  first_episode?: number | null;
  required_views?: string[];
  discovery_sources?: string[];
}

export interface Bible {
  characters: Character[];
  world: { era: string; genre: string; visual_style_canonical: string };
  scenes?: Scene[];
}

export interface Project {
  id: string;
  name: string;
  status: string;
  novel_chars: number;
  bible_status: string;
  bible_error?: string;
  plan_status: string;
  plan_error?: string;
  bible_version?: number;
  refs_status?: string;
  refs_error?: string;
  refs_target?: string | null;
  scene_refs_status?: string;
  scene_refs_error?: string;
  scene_refs_target?: string | null;
  bible?: Bible | null;
  key_timeline?: string[];
  chapters?: {
    idx: number;
    title: string;
    char_count: number;
    preview?: string;
  }[];
  episodes?: Episode[];
  episodes_total?: number;
  episodes_page?: number;
  episodes_page_count?: number;
  episodes_query?: string;
  episodes_status_filter?: string;
  episodes_busy?: boolean;
  first_chapter_idx?: number | null;
  episode_counts?: {
    total: number;
    done: number;
    screenplay_running: number;
    scripting: number;
    screenplay_todo: number;
    storyboard_ready: number;
  };
  chapter_count?: number;
  episode_count?: number;
  bible_artifact_id?: string | null;
  bible_evidence?: ArtifactEvidence | null;
  harness_engine_enabled?: number | boolean;
}

export const numToCn = (n: number): string => {
  const cn = "零一二三四五六七八九";
  if (n <= 10) return n === 10 ? "十" : cn[n];
  if (n < 20) return "十" + cn[n % 10];
  if (n < 100)
    return cn[Math.floor(n / 10)] + "十" + (n % 10 ? cn[n % 10] : "");
  return String(n);
};

export { ApiError };
