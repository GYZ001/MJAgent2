import { requestCapabilityApproval } from "./capabilityApproval";
import type { AuthUser, WorkspaceMembership } from "./auth/session";

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

function normalizeNetworkError(error: unknown): Error {
  if (error instanceof ApiError) return error;
  const message = error instanceof Error ? error.message : String(error);
  if (
    error instanceof TypeError
    || /Failed to fetch|fetch failed|ECONNREFUSED|NetworkError|Load failed/i.test(message)
  ) {
    return new ApiError(
      0,
      "无法连接本机后端服务，请等待服务恢复后重试",
      "BACKEND_UNAVAILABLE",
      "网络错误",
    );
  }
  return error instanceof Error ? error : new Error(message);
}

const SESSION_HEADER = "X-Manju-Session";
const APPROVAL_HEADER = "X-Manju-Approval-Token";

// 会话令牌持久化到 localStorage。
//
// 最初只放模块内存、不落任何存储，理由是 XSS posture。实际用下来这个取舍是错的：
// 整页刷新就要重登一次，而换来的安全收益很有限——真被 XSS 了，攻击者本来就能直接
// 以当前页面身份发请求、或读走内存里的这个变量，持久化只是让被窃令牌多活一会儿。
// 拿"每次刷新重登"去买这点边际收益，不值。
//
// 真正的防线在服务端而不是这里：会话可被随时吊销（停用账号/改密即刻失效）、有滑动
// 过期与绝对上限。localStorage 里这份只是个缓存，失效了第一个请求就 401 并回登录页。
const TOKEN_STORAGE_KEY = "manju:session-token";

function readStoredToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    // 隐私模式/禁用存储时 localStorage 会抛异常；退化成本次会话内可用即可，不能崩。
    return null;
  }
}

function writeStoredToken(token: string | null): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
    else window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    /* 同上：存不进去不影响本次会话，不要因此中断登录 */
  }
}

let sessionToken: string | null = readStoredToken();
const inflightGets = new Map<string, Promise<any>>();

/** AuthContext 订阅这个信号：一次真正判定为「登录已失效」时回调，值切回登录页。
 *  只留一个模块级回调而非事件总线——全局只有一个 AuthProvider 需要它。 */
type UnauthenticatedListener = () => void;
let unauthenticatedListener: UnauthenticatedListener | null = null;

export function onUnauthenticated(listener: UnauthenticatedListener | null): void {
  unauthenticatedListener = listener;
}

function notifyUnauthenticated(): void {
  sessionToken = null;
  writeStoredToken(null);
  unauthenticatedListener?.();
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
  const isForm = Boolean(options?.form);
  const headers = baseHeaders(
    !isForm && body !== undefined
      ? { "Content-Type": "application/json" }
      : undefined,
    options?.approvalToken,
  );
  let resp: Response;
  try {
    resp = await fetch(`/api${path}`, {
      method,
      headers,
      body: isForm
        ? options?.form
        : body !== undefined
          ? JSON.stringify(body)
          : undefined,
    });
  } catch (error: unknown) {
    throw normalizeNetworkError(error);
  }

  // /auth/login 的 401 是「密码错」这一正常业务结果，不是"登录已过期"——
  // 它不该走下面的重试（后端登录限流是 5 次/5 分钟，重试一次等于让一次输错
  // 顶两次额度），也不该触发全局的「未登录」信号（本来就还没登录成功）。
  // 直接交给 handle() 把 401/429 原样抛给 LoginPage 的表单。
  const isLoginEndpoint = path === "/auth/login";

  // 401 意味着登录已失效（会话过期/被登出/被吊销），不再是"共享秘密要轮换"——
  // 内存里也不存在能静默换新的凭证了。先按当前 sessionToken 重试一次，只覆盖
  // "登录刚完成、这个请求发出时用的还是旧值"的竞态；仍是 401 才真正判定为登录
  // 过期，通知 AuthContext 切回登录页。
  if (resp.status === 401 && !isLoginEndpoint && !options?._sessionRefreshed) {
    return request(method, path, body, {
      ...options,
      _sessionRefreshed: true,
    });
  }
  if (resp.status === 401 && !isLoginEndpoint) {
    notifyUnauthenticated();
  }

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

async function download(path: string, sessionRefreshed = false): Promise<Blob> {
  let resp: Response;
  try {
    resp = await fetch(`/api${path}`, { headers: baseHeaders() });
  } catch (error: unknown) {
    // 与 request() 对齐：断网时给出可读文案，而不是原始的 "Failed to fetch"。
    throw normalizeNetworkError(error);
  }
  // 同样与 request() 对齐：401 只重试一次（覆盖登录竞态），仍失败才判定登录过期。
  if (resp.status === 401 && !sessionRefreshed) {
    return download(path, true);
  }
  if (resp.status === 401) {
    notifyUnauthenticated();
  }
  if (!resp.ok) await handle(resp);
  return resp.blob();
}

export interface AuthMeResponse {
  user: AuthUser;
  workspaces: WorkspaceMembership[];
  is_system_admin: boolean;
  must_change_password: boolean;
}

export interface AuthLoginResponse extends AuthMeResponse {
  session_token: string;
  header: string;
}

/** 账号密码登录；成功后把签发的会话令牌记进内存，供后续请求带上。 */
export async function login(
  username: string,
  password: string,
): Promise<AuthLoginResponse> {
  const data = (await request("POST", "/auth/login", {
    username,
    password,
  })) as AuthLoginResponse;
  sessionToken = data.session_token;
  writeStoredToken(sessionToken);
  return data;
}

/** 登出：无论后端调用是否成功，本地内存里的令牌都要清掉。 */
export async function logout(): Promise<void> {
  try {
    await request("POST", "/auth/logout");
  } finally {
    sessionToken = null;
    writeStoredToken(null);
  }
}

/** 「我是否已登录」探针；401 由 request() 统一处理（触发 onUnauthenticated）。 */
export function me(): Promise<AuthMeResponse> {
  return request("GET", "/auth/me");
}

/** 改密成功后后端会吊销其余会话并签发一枚新 token，同样要更新到内存里。 */
export async function changePassword(
  oldPassword: string,
  newPassword: string,
): Promise<AuthLoginResponse> {
  const data = (await request("POST", "/auth/change-password", {
    old_password: oldPassword,
    new_password: newPassword,
  })) as AuthLoginResponse;
  sessionToken = data.session_token;
  writeStoredToken(sessionToken);
  return data;
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
  episodeGenerate: (
    episodeId: string,
    idempotencyKey: string,
    options?: { onlyIncomplete?: boolean; qualificationVersion?: string },
  ): Promise<EpisodeGenerateResult> =>
    request("POST", `/episodes/${episodeId}/generate`, {
      idempotency_key: idempotencyKey,
      only_incomplete: options?.onlyIncomplete,
      qualification_version: options?.qualificationVersion,
    }),
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
  getReviewContext: (episodeId: string) =>
    request(
      "GET",
      `/episodes/${episodeId}/review-context`,
    ) as Promise<ReviewWallContext>,
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
  bibleVisualStyles: (projectId: string) =>
    request("GET", `/projects/${projectId}/bible/visual-styles`) as Promise<{
      default: string;
      items: Array<{ name: string; description: string; sample_image: string }>;
    }>,
  /**
   * 人物谱与场景库共用：只切换项目统一画风，不重新生成人物谱角色内容。
   * 两段式：不带 confirm 时，画风未变化直接返回 changed=false；画风有变化则
   * 后端抛 409（ApiError.code === 'PAYMENT_CONFIRM_REQUIRED'），detail.precheck
   * 是人物+场景合并报价。带 confirm+quote_id 确认后，后端在同一次请求内发起
   * 人物定妆照与场景图两条生成线（见 lib/styleRegen.ts::applyStyleRegen，
   * 前端拿到报价后立即用 quote_id 自动确认，不再弹窗等用户手动点确认）。
   */
  setBibleStyle: (
    projectId: string,
    body: {
      style_name: string;
      expected_version: number;
      confirm?: boolean;
      quote_id?: string;
    },
  ) =>
    request("POST", `/projects/${projectId}/bible/style`, body) as Promise<{
      project_id: string;
      style_name: string;
      changed: boolean;
      bible_version?: number;
      scene_bible_ready?: boolean;
      scenes_total?: number;
      idempotent_replay?: boolean;
      quote_id?: string;
      task_id?: string;
      refs_started?: boolean;
      refs_error?: string | null;
      scene_refs_started?: boolean;
      scene_refs_error?: string | null;
    }>,
  bibleGeneratePrecheck: (projectId: string, body?: { style_name?: string }) =>
    request(
      "POST",
      `/projects/${projectId}/bible/generate-precheck`,
      body || {},
    ) as Promise<
      RefsCostPrecheck & {
        estimated_duration_min?: number[];
        estimate_note?: string;
        character_names?: string[];
        style_name?: string;
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
};

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
  entry_state?: string;
  exit_state?: string;
  context_requirements?: string[];
}

export interface PlotSpineBeat {
  beat_id: string;
  who?: string;
  does?: string;
  turn?: string;
  must_keep?: boolean;
  source_segment_ids?: string[];
  purpose?: string;
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
  source_coverage?: {
    source_segment_id: string;
    disposition: "deliver" | "merge" | "context" | "duplicate";
    beat_ids: string[];
    duplicate_of?: string | null;
    reason?: string;
  }[];
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

/**
 * 映射台（原「剧本台」，2.0.0 架构收窄）转型后的轻量分集映射包
 * （episode_prep_pack，字段/类型名不改，仅界面文案改名）。取代 EpisodeScreenplay
 * 成为映射台的发布产物，投影在
 * `Episode.prep_pack` 字段（不是 `Episode.screenplay`——后端把两种产物形状分到
 * 不同字段，见 Episode.prep_pack 上的注释）。旧产物（无 prep_pack_version 字段）
 * 仍可能出现在 `Episode.screenplay` 中，调用方必须先按 prep_pack_version 判别，
 * 见 ScriptPage.tsx 的 isPrepPack。基础形状冻结见 docs/TRANSFORM_FREEZE_PLAN.md
 * §3；字段随版本持续演进，均按可选处理，不假设某个具体版本号是终点。
 *
 * 2.0.0（架构收窄，见 app/production/prep_pack.py 模块 docstring 的 2.0.0
 * 说明）：映射台不再产出任何叙事内容——`event_chain`/`hook`/`cliffhanger` 全部
 * 撤销，职责收窄为三件事：①发现本章新人物/新场景；②把人物/地点映射到世界书
 * 已有的图像素材；③把原文里的模糊人物称谓映射成人物谱里的精准称谓
 * （`appellation_map`，新增）。哪一集有几个叙事节拍是分镜台自己从原文提炼的
 * 职责，不再是这里的产物。资产条目原来用 `event_ids` 记账"这个资产出现在哪些
 * 事件"，事件没了，改用 `segment_indexes`（原文段落序号，1-based）直接记录
 * "这个资产真正在场的原文段"——不是改名，是从原文重新推导，语义更精确。旧产物
 * （`event_chain`/`event_ids`/`hook`/`cliffhanger`）仍可能出现在尚未重新生成的
 * 已发布集里，前端不假设一定是新形状；本文件的类型只描述当前后端产出的新形状，
 * 读取旧产物时这些字段就是 undefined，调用方需按可选处理（同旧例）。
 */

/**
 * 1.7.0+ 字段：一条绑定的来源证明——method 取值 direct/alias/resolution/
 * resolution_forward/candidate_verdict/discovery/alias_inherited 等，具体见
 * app/production/prep_pack.py 的 _prep_pack_provenance。anchor_segments/
 * anchor_phrase 是绑定判据钉住的原文证据；forward_chapter_label/
 * source_episode_no 只在特定 method 下才非空。前端只把 method 当低调提示
 * （悬浮提示）展示，不做任何业务判断。2.0.0 起 characters/scenes/
 * functional_extras/props 四类资产共用同一个 provenance 形状（此前只有
 * characters 有类型化的 provenance）。
 */
export interface PrepPackProvenance {
  method?: string;
  anchor_segments?: number[];
  anchor_phrase?: string;
  forward_chapter_label?: string;
  source_episode_no?: number;
  dual_anchor?: boolean;
  candidate_verdict_attempted?: boolean;
}

export interface PrepPackCharacterAsset {
  identity_id: string;
  display_name: string;
  portrait_id: string | null;
  /**
   * 2.0.0+ 字段：取代 event_ids，这个角色真正在场（画面出场）的原文段号，1-based。
   * 2.0.0 之前的产物（如 1.11.x）没有这个字段——运行时是 undefined，不是空数组；
   * 调用方必须把「字段不存在」和「测量后是 0 段」分开显示，不许把前者渲染成后者，
   * 见 ScriptPage.tsx 的 isLegacyPrepPackFormat / assetCoverageText。
   */
  segment_indexes?: number[];
  /** 1.2.0+ 字段；本集内对该角色的称谓（如「小胖子」）。之前的产物没有它。 */
  aliases?: string[];
  /**
   * 1.7.0+ 字段：画面与字幕分离（见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.3）。
   * visual_entity_id 全局稳定，决定用哪张定妆照取图；display_name 绑定成功后会被
   * 改写为全局规范名，取图仍看 visual_entity_id 不受影响，两者语义分工不同。
   * 1.7.0 之前的产物没有这个字段。
   */
  visual_entity_id?: string;
  /**
   * 1.7.0+ 字段：本集原文对这个人的称呼（如第 1 集「银色长袍女子」），决定字幕/
   * 台词显示——刻意不提前剧透 display_name 这个全局规范名。1.7.0 之前的产物没有
   * 这个字段，此时前端只能退回展示 display_name。
   */
  display_appellation?: string;
  /** 1.6.0+ 字段；这条绑定是怎么判出来的，见 PrepPackProvenance。 */
  provenance?: PrepPackProvenance;
}

export interface PrepPackSceneAsset {
  scene_id: string;
  display_name: string;
  scene_reference_id: string | null;
  /** 2.0.0+ 字段，undefined 于旧产物——语义同 PrepPackCharacterAsset.segment_indexes。 */
  segment_indexes?: number[];
  provenance?: PrepPackProvenance;
}

/**
 * 2.0.0 新增：道具/物品——世界书没有道具图像素材库，只有文字描述
 * （description），不映射任何图片，见 app/production/prep_pack.py 的
 * _prep_pack_build_prop_manifest。
 */
export interface PrepPackProp {
  label: string;
  description: string;
  /** 2.0.0+ 字段，undefined 于旧产物——语义同 PrepPackCharacterAsset.segment_indexes。 */
  segment_indexes?: number[];
  provenance?: PrepPackProvenance;
}

/**
 * 1.3.0+ 字段：群演 / 一次性人物——没有定妆照是设计使然（不进人物谱身份体系），
 * 不是数据缺失，前端展示时用统一占位图标，不当成"图片没找到"处理。
 */
export interface PrepPackFunctionalExtra {
  label: string;
  /** 2.0.0+ 字段，undefined 于旧产物——语义同 PrepPackCharacterAsset.segment_indexes。 */
  segment_indexes?: number[];
  visual_entity_id?: string;
  provenance?: PrepPackProvenance;
}

export interface PrepPackAssetManifest {
  characters: PrepPackCharacterAsset[];
  scenes: PrepPackSceneAsset[];
  /** 2.0.0+ 字段；更早的产物没有它，读取时按可选处理。 */
  props?: PrepPackProp[];
  /** 1.3.0+ 字段；1.2.0 及更早的产物没有它，读取时按可选处理。 */
  functional_extras?: PrepPackFunctionalExtra[];
}

/**
 * 2.0.0 新增：把原文里的模糊人物称谓（如「那少年」「小胖子」）映射到人物谱里的
 * 精准称谓——asset_manifest.characters[] 已有的别名消歧结论，按 (原文称谓,
 * 原文段号) 逐条摊平展示，供人工核对"这一段原文里的这个称谓，系统认为指的是
 * 谱内哪个人"。只覆盖已解析到 identity_id 的人物提及，不含 functional_extras
 * （群演没有精准身份可映射）。
 */
export interface PrepPackAppellationMapEntry {
  raw_mention: string;
  segment_index: number;
  identity_id: string;
  canonical_appellation: string;
}

export interface PrepPackEpisodeScope {
  chapter_indexes: number[];
  source_segment_count: number;
}

/**
 * 覆盖账本四账 + uncovered 的元素形状后端未最终敲定（frozen payload 示例给的是空数组），
 * 防御性地按 number 或 {segment_index} 两种可能解析，两者都不匹配时原样兜底展示。
 */
export type PrepPackCoverageEntry = number | { segment_index?: number | string } | Record<string, unknown>;

export interface PrepPackCoverageLedger {
  total_segments: number;
  delivered: PrepPackCoverageEntry[];
  merged: PrepPackCoverageEntry[];
  retained_as_context: PrepPackCoverageEntry[];
  proven_duplicates: PrepPackCoverageEntry[];
  uncovered: PrepPackCoverageEntry[];
  /**
   * 第五账（1.4.0+ 字段，1.3.0 及更早的产物没有它）：副文本——章节名/作者留言段等
   * 不属于正文、但已被合法计入覆盖的原文段。不算未覆盖，参与"已覆盖"总数
   * 的并集计算，见 ScriptPage.tsx 的 coverageGateSummary。
   */
  paratext?: PrepPackCoverageEntry[];
}

export interface EpisodePrepPack {
  prep_pack_version: string;
  episode_no: number;
  episode_scope: PrepPackEpisodeScope;
  asset_manifest: PrepPackAssetManifest;
  /** 2.0.0+ 字段；更早的产物没有它，读取时按可选处理。 */
  appellation_map?: PrepPackAppellationMapEntry[];
  coverage_ledger: PrepPackCoverageLedger;
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
  | "FIRST_FRAME_MODE"
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
  | "PREVIOUS_STATIC_TAIL"
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

/** POST /episodes/{id}/generate 单条镜头入队结果——成功态在 job_id/reused/active
 *  之外还可能带 version_id（幂等复用了一条已交付版本）；失败态只有 error/issue_codes，
 *  整批请求仍返回 200，不会因为其中一段失败让其余段落一起回滚（见
 *  app/domain/video_ops.py::_generate_episode_core）。 */
/** reused=true 时说明"为什么没有新建"——不是猜的，是服务端按命中的那条
 *  记录的真实状态直接翻译过来的（见 app/media_exec/enqueue.py
 *  ``_reused_reason_for_status``）：
 *  - "succeeded"：已有交付版本，无需重新生成；
 *  - "in_flight"：仍在排队/生成/等待供应商轮询，重复提交只会双花；
 *  - "stuck_needs_human"：命中的记录卡在需要人工处理，没有新任务被提交
 *    ——这种情况不应该被算进"已提交"。 */
export type ReusedReason = 'succeeded' | 'in_flight' | 'stuck_needs_human';

export interface EpisodeGenerateEnqueueResult {
  shot_id: string;
  reused?: boolean;
  reused_reason?: ReusedReason;
  job_id?: string;
  task_accepted?: boolean;
  active?: boolean;
  version_id?: string;
  error?: string;
  issue_codes?: string[];
}

export interface EpisodeGenerateResult {
  episode_video_plan_id: string;
  plan_revision: number;
  mode_distribution: Record<string, number>;
  critical_path_latency_ms: number;
  estimated_cost: number;
  enqueued: EpisodeGenerateEnqueueResult[];
  /** only_incomplete=true 时被跳过的已完成段数——服务端口径，不是前端猜的。 */
  skipped_completed: number;
  /** 本次实际提交入队循环的段数（已完成段被 only_incomplete 过滤后剩下的）。 */
  selected_shots: number;
  recovered_partial_operation?: boolean;
  remaining_requires_new_idempotency_key?: boolean;
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
  /** 在跑任务的服务端起点（秒）；有值表示这条正在生成，用于实时计时。 */
  running_since?: number | null;
  /** 供应商任务编号（app/media_exec/run_job.py 写入 shot_versions.provider_task_id）；
   *  失败/隔离候选排障用，原样透传不翻译。 */
  provider_task_id?: string | null;
  artifact_id?: string | null;
  adoption_reason?: string | null;
  playback_rate?: number | null;
  technical_validation_json?: string | null;
  created_at?: number | null;
  image_inputs?: {
    first_frame_used?: boolean;
    first_frame_src?: string | null;
    first_frame_source?: VideoPlanAssetSource | null;
    first_frame_scene_id?: string | null;
    first_frame_image_url?: string | null;
    last_frame_used?: boolean;
    last_frame_src?: string | null;
    last_frame_source?: VideoPlanAssetSource | null;
    last_frame_scene_id?: string | null;
    last_frame_image_url?: string | null;
    video_input_url?: string | null;
    video_input_source_revision_id?: string | null;
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
    ai_video_prompt_contract_version?: string | null;
    ai_video_prompt_generated_at?: number | null;
    required_reference_characters?: string[];
    required_interaction_reference_characters?: string[];
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
  /** 按镜作用域的资格版本：本镜生成/采纳只应比对自己这一份——兄弟镜新增
   * 素材不会改变它，真正的本镜素材漂移或上游变化仍会改变它。 */
  shot_qualification_versions?: Record<string, string>;
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

/**
 * 分镜台 2.0.0（docs/STORYBOARD_PROMPT_IR_DESIGN.md）冻结契约：一个 15 秒段的完整
 * 记录。落在 Shot.storyboard_pack_segment 上（后端 app/production/storyboard_pack.py
 * persist_storyboard_pack 写入），非 null 是唯一权威标记——这一行的
 * shot_size/camera_move/camera_angle/first_frame_desc/last_frame_desc 等描述单个
 * 连续镜头的字段在这里粒度失效，段内 3-4 个镜头切换全部写在 prompt_text 文本里。
 */
export interface StoryboardPackDialogueLine {
  speaker_identity_id: string;
  line: string;
  source_segment_index: number;
}

/**
 * 段所属节拍的自包含记录（2026-08-26 补齐）：拿到一个 shot 就能渲染它承载哪几个
 * 节拍、分别在讲什么，不必再跨行反查。取代裸 beat_ids 数组作为展示用真源。
 */
export interface StoryboardPackSegmentBeat {
  beat_id: string;
  summary: string;
  segment_indexes: number[];
}

export interface StoryboardPackResourceCharacter {
  identity_id: string;
  portrait_id?: string | null;
  description?: string;
}

export interface StoryboardPackResourceScene {
  scene_id: string;
  scene_reference_id?: string | null;
  description?: string;
}

export interface StoryboardPackResourceProp {
  label: string;
  description?: string;
}

export interface StoryboardPackResources {
  characters: StoryboardPackResourceCharacter[];
  scenes: StoryboardPackResourceScene[];
  props: StoryboardPackResourceProp[];
}

export interface StoryboardPackSegment {
  segment_no: number;
  duration_s: number;
  synopsis: string;
  source_segment_indexes: number[];
  /** 模型直接产出的整块可复制提示词；代码不拼装、不挂尾缀，必须整块展示与复制。 */
  prompt_text: string;
  shot_count: number;
  dialogue: StoryboardPackDialogueLine[];
  resources: StoryboardPackResources;
  /** 能力降级清单（如 Seedance 侧屏上文字改「无字」）；不许静默吞掉，必须显示。 */
  degraded_capabilities: string[];
  /** 段所属节拍，自包含（含摘要），展示时的唯一真源——见 StoryboardPackSegmentBeat 注释。 */
  beats: StoryboardPackSegmentBeat[];
  /**
   * @deprecated 前端不再读取。早期持久化路径只落了裸 ID，无摘要；现在 beats 已
   * 自包含摘要，展示一律改读 beats。字段仍随行下发（后端过渡期兼容），不属于
   * 前端消费的形状，留着只是避免破坏未迁移的调用方。
   */
  beat_ids: string[];
  /**
   * 冻结契约自己的模型词表（"seedance_2" | "minimax_h3"），由后端从解析出的
   * prompt profile 派生；与 Episode.target_video_model 的供应商 key
   * （"hiagent" | "minimax_h3"）不是同一套词表，不能互相当同义词直接查表。
   */
  target_model: string;
  storyboard_version: string;
}

export interface Shot {
  id: string;
  episode_id: string;
  script_id?: string | null;
  shot_no: number;
  duration_s: number;
  shot_size: string;
  camera_angle?: string;
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
  context_requirement_ids?: string[];
  resulting_change?: string;
  readability_focus?: "context" | "action" | "emotion" | "dialogue" | "evidence" | "transition" | string;
  camera_motivation?: string;
  repeat_of_shot_id?: string | null;
  repeat_gain?: string;
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
  /** 非 null 时这一行是分镜台 2.0.0 的 15 秒段落，见 StoryboardPackSegment 注释。 */
  storyboard_pack_segment?: StoryboardPackSegment | null;
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
  /**
   * 转型后的轻量分集映射包（screenplay 契约 6.0.0+）。后端 _episode_detail_projection
   * 把这两种产物形状投影到不同字段：旧形状仍在 `screenplay`；新形状在 `prep_pack`，
   * `screenplay` 此时为 null（不会把新形状塞进旧字段）。ScriptPage 必须两个字段都看：
   * prep_pack 非空 → 渲染映射包；否则 screenplay 非空 → 旧产物占位提示；否则 → 尚无产物。
   */
  prep_pack?: EpisodePrepPack | null;
  scene_options?: string[];
  shot_count?: number;
  video_count?: number;
  pending_adoption_count?: number;
  failed_count?: number;
  screenplay_artifact_id?: string | null;
  screenplay_evidence?: ArtifactEvidence | null;
  screenplay_state?: ScreenplayState | null;
  screenplay_production?: {
    revision_id?: string;
    operation: "none" | "baseline" | "baseline_rebuild" | "finalize" | "complete";
    mode?: "none" | "baseline" | "baseline_rebuild" | "finalize" | "complete";
    mode_label?: string;
    eligibility?: {
      mode: "none" | "baseline" | "baseline_rebuild" | "finalize" | "complete";
      label: string;
      revision_id: string | null;
      revision_action: "none" | "reuse" | "rebase";
      working_artifact_id: string | null;
      working_compatible: boolean;
      reusable_checkpoint: {
        blueprint_artifact_id?: string;
        identity_artifact_id?: string;
        envelope_artifact_id?: string;
        shards?: Array<Record<string, unknown>>;
        merged_ir_artifact_id?: string;
        shard_progress?: {
          total: number;
          validated: number;
          running: number;
          failed: number;
        };
      };
      reason_code: string;
      reason: string;
      resumable: boolean;
    };
    phase: string;
    phase_label?: string;
    stage_index?: number;
    stage_count?: number;
    /**
     * 旧十步重型流水线遗留的阶段列表（{key, label, status}）。后端正在把集详情投影
     * 统一到 prep_pack_stages 单源；前端已不再读这个字段做任何回退渲染——用户报告过
     * 首屏闪现旧十步阶段带，根因就是曾经的“新字段缺失时回退旧 stages”逻辑，回退
     * 本身已被移除（见 ScriptPage.tsx 的 resolveStages）。字段仍保留在类型里只是
     * 因为后端可能仍在下发，不代表前端会用它。
     */
    stages?: Array<{
      key: string;
      display_name?: string;
      label?: string;
      state?: string;
      status?: string;
    }>;
    /**
     * 转型后的真实轻量流程阶段列表（已定稿，2026-08-24 后端上线）：
     * [{key, display_name, state}]，state: pending/active/done/blocked。
     * 这是阶段带渲染的唯一数据源（见 ScriptPage.tsx 的 resolveStages）：缺失或为
     * 空数组时前端不再回退到上面的旧 `stages`，只渲染轻量占位骨架或完全不渲染。
     * 具体每一项仍过 normalizeStage 的三级文本回退（display_name ?? label ?? key，
     * state ?? status ?? 'pending'）防御，防止这个字段自身漏子字段时渲出空文本。
     */
    prep_pack_stages?: Array<{
      key: string;
      display_name?: string;
      label?: string;
      state?: string;
      status?: string;
    }>;
    baseline_done: boolean;
    first_evaluation_done: boolean;
    task_active: boolean;
    /** 服务端 run 的起止时间（秒）。计时以此为准，不用前端本地起点。 */
    task_started_at?: number | null;
    task_finished_at?: number | null;
    can_resume_baseline?: boolean;
    can_resume_repair: boolean;
    shard_progress?: {
      total: number;
      validated: number;
      running: number;
      failed: number;
    };
    activation_count?: number;
    patch_count?: number;
    open_issue_count?: number;
    quality_score?: number | null;
    quality_issue_count?: number;
    gate_retry_exhausted?: boolean;
    yield_reason?: string;
    stage_stop_reason?: "paused" | "blocked" | "failed" | "";
  } | null;
  shots?: Shot[];
  storyboard_planned_shots?: number | null;
  storyboard_artifact_id?: string | null;
  storyboard_evidence?: ArtifactEvidence | null;
  storyboard_status?: StoryboardStatus | null;
  /** 逐镜生成耗时，键为 shot_no；累计全部重试迭代。 */
  shot_timings?: Record<string, ShotTiming>;
  /** 整集视频生成的总计时。 */
  video_task_timing?: TaskTiming;
  active_storyboard_run_id?: string | null;
  active_video_run_id?: string | null;
  video_completion_mode?: string | null;
  /**
   * 本集绑定的视频生成供应商 key（'hiagent' = Seedance 2.0 / 'minimax_h3' =
   * MiniMax H3）。与生成台强绑定：提交视频生成时后端会拿这个值和模型中心当前
   * 生效供应商比对，不一致即拒绝。切换走 POST .../video-model，见 BoardPage。
   */
  target_video_model?: string | null;
  video_supervisor?: {
    phase?: string | null;
    run_id?: string | null;
    run_status?: string | null;
    outcome?: string | null;
    task_running?: boolean;
    running?: boolean;
    active_media_jobs?: number;
    finished_at?: number | null;
    last_plan?: Record<string, unknown> | null;
  } | null;
  video_budget?: {
    used_cny: number;
    claimed_current_shots: number;
    shots_total: number;
    unclaimed_first_pass_cny: number;
    required_completion_cap_cny: number;
  } | null;
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
  /** 服务端 run 的起止时间（秒）。任务计时以此为准，不用前端本地起点。 */
  task_started_at?: number | null;
  task_finished_at?: number | null;
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
  /** 继续任务是生成后续镜头、修复现有完整分镜，还是只签发发布证据。 */
  resume_mode?: "continue_generation" | "repair_existing" | "finalize_evidence" | null;
  final_shot_valid: boolean;
  hard_gates_passed: boolean;
  hard_gate_issue_count?: number;
  hard_gate_issues?: string[];
  system_error?: string | null;
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
  first_episode?: number | null;
  required_views?: string[];
  discovery_sources?: string[];
}

export interface Bible {
  characters: Character[];
  world: { era: string; genre: string; visual_style_canonical: string };
  scenes?: Scene[];
}

/** 服务端 run 的起止时间（秒）；缺失表示该任务从未跑过。 */
export interface TaskTiming {
  started_at?: number | null;
  finished_at?: number | null;
}

/** 单个镜头的生成耗时。已完成迭代累计在 elapsed_ms，仍在跑的那轮给起点。 */
export interface ShotTiming {
  elapsed_ms: number;
  running_since?: number | null;
  iterations: number;
}

/** 世界书/映射台/分镜台分环节文本模型下拉的单个可选项；只包含已配凭据的模型，
 *  不会出现选了就必然失败的条目（见 app/model_registry.py::text_model_choices）。*/
export interface TextModelChoice {
  provider: string;
  label: string;
  model: string;
}

export interface Project {
  id: string;
  name: string;
  status: string;
  novel_chars: number;
  /** 世界书环节专属文本模型（provider key）；空串＝未设置，回落到系统默认文本模型。 */
  bible_text_provider?: string;
  /** 映射台环节专属文本模型；含义同上。 */
  script_text_provider?: string;
  /** 分镜台环节专属文本模型；含义同上。与「视频模型」是两回事：视频模型控制
   *  实际提交视频生成用哪个供应商，这里控制分镜台自身生成分镜内容用哪个文本模型。 */
  board_text_provider?: string;
  /** 当前可选的文本模型清单，三个环节共用同一份（凭据配置是全局的）。 */
  text_model_choices?: TextModelChoice[];
  /** 各项目级任务的服务端计时。前端本地起点会在刷新后搁浅，一律以此为准。 */
  task_timings?: {
    bible?: TaskTiming;
    refs?: TaskTiming;
    scene_refs?: TaskTiming;
    screenplay_batch?: TaskTiming;
    storyboard_batch?: TaskTiming;
  };
  bible_status: string;
  bible_error?: string;
  bible_style_name?: string | null;
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
    screenplay_queued: number;
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
  /** 以下仅在 ?view=picker&episode_limit>0 的窗口模式下返回。
   *  整份分集在千集项目里未压缩 250KB，而切换器最多只展示 60 条，
   *  故服务端只回一个窗口，另外把窗口外仍需要的信息单独带上。 */
  episode_total?: number;
  episode_match_total?: number;
  episode_offset?: number;
  episode_index?: number | null;
  episode_current?: Episode | null;
  episode_prev?: Pick<Episode, "id" | "episode_no" | "title"> | null;
  episode_next?: Pick<Episode, "id" | "episode_no" | "title"> | null;
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
