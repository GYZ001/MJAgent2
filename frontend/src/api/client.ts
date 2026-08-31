
// 传输层：会话管理、HTTP 动词与错误规范化。这是 api/ 包内部的实现细节——
// `request`/`get`/`mutate`/`download` 只在 api/*.ts 之间可见，不从包入口
// （api/index.ts）对外导出。页面代码应该只看到 api/index.ts 组装出的
// 具名方法或 `api.get/post/put/del/upload` 这几个保留的逃生口本身，
// 不应该直接 import 这个文件。

export class ApiError extends Error {
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

/** waiting_approval 载荷里的 preflight 字段（后端 PreflightResult 的 JSON 形状，
 *  这里只声明前端实际会读的几个字段）。 */
export interface ApprovalPreflight {
  summary: string;
  risk?: string;
  confirmation_policy?: string;
  affected?: {
    projects?: string[];
    episodes?: string[];
    shots?: string[];
    shot_count?: number;
    versions?: string[];
    packages?: string[];
  };
}

/**
 * 「删除资源」类命令在等待批准：与其余命令不同，这一档不会被 request() 自动
 * 用 approval_token 重放掉（2026-08-30 产品拍板：除了删除资源，否则不需要
 * 弹窗）。调用方（通常经 hooks/useDeleteConfirm）捕获后向用户展示
 * `preflight.summary`，用户确认时调用 `retry()` 重放同一次请求。
 */
export class ApprovalRequiredError extends Error {
  constructor(
    public preflight: ApprovalPreflight,
    public retry: () => Promise<any>,
  ) {
    super(preflight?.summary || "需要确认后才能继续，该操作不可撤销");
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

/** 供 api/auth.ts 在登录/改密成功后写入新签发的会话令牌。 */
export function setSessionToken(token: string | null): void {
  sessionToken = token;
  writeStoredToken(token);
}

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

export async function request(
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
      const resubmit = () => request(method, path, body, {
        ...options,
        approvalToken: String(payload.approval_token),
        _retried: true,
        _sessionRefreshed: true,
      });
      // 浏览器端不再就用户自己刚点下的动作二次追问（2026-08-29 用户拍板：
      // 生成前的批准/确认弹窗一律下线）——但「删除资源」除外（2026-08-30
      // 产品追加拍板：除了删除资源，否则不需要弹窗）。这一档必须让用户真的
      // 看到确认界面，不能像其余命令一样在这里被自动重放掉——那正是本次要
      // 修的问题：catalog 登记了 confirmation=ALWAYS，但对浏览器调用方从来
      // 就是空头承诺。判据挂在服务端返回的 preflight.confirmation_policy 上
      // （源自 CommandSpec.confirmation；后端闸门
      // app.capabilities.coverage.find_confirmation_policy_mismatches 保证
      // 这个值只在真删除资源时才是 "always"），不挂前端硬编码的路径名单——
      // 新登记的删除类能力自动落进这一档，不需要改这个文件。
      // agent/MCP 那条人在环路的闸门走 app/agent/approvals.py 与
      // app/mcp/server.py，自成一套 approval_id，不经过本文件，不受影响。
      if (payload.preflight?.confirmation_policy === "always") {
        throw new ApprovalRequiredError(payload.preflight, resubmit);
      }
      return resubmit();
    }
    // 异步受理（如单视角重做）：直接返回 payload，不再走 handle
    return payload;
  }

  return handle(resp);
}

export function get(path: string): Promise<any> {
  const active = inflightGets.get(path);
  if (active) return active;
  const pending = request("GET", path).finally(() => {
    if (inflightGets.get(path) === pending) inflightGets.delete(path);
  });
  inflightGets.set(path, pending);
  return pending;
}

export function mutate(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
) {
  return request(method, path, body);
}

export async function download(path: string, sessionRefreshed = false): Promise<Blob> {
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
