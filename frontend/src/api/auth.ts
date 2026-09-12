import type { AuthUser } from "../auth/session";
import { request, setSessionToken } from "./client";

export interface AuthMeResponse {
  user: AuthUser;
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
  setSessionToken(data.session_token);
  return data;
}

/** 登出：无论后端调用是否成功，本地内存里的令牌都要清掉。 */
export async function logout(): Promise<void> {
  try {
    await request("POST", "/auth/logout");
  } finally {
    setSessionToken(null);
  }
}

/** 「我是否已登录」探针；401 由 request() 统一处理（触发 onUnauthenticated）。 */
export function me(): Promise<AuthMeResponse> {
  return request("GET", "/auth/me");
}

/** 自删账号（不可恢复，立即级联清空全部项目）。不带 confirm 只是预检：返回前
 *  会以 422 抛出 `{code:"confirmation_required", message, project_count}`，
 *  调用方拿它填确认弹窗的文案；带 confirm=true 才真正执行。 */
export function deleteMyAccount(confirm: boolean): Promise<unknown> {
  return request("DELETE", `/auth/me${confirm ? "?confirm=true" : ""}`);
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
  setSessionToken(data.session_token);
  return data;
}

/* ── 邀请链接接受流程（EP-03 第二阶段，公开端点，不挂会话闸门） ── */

export interface InvitationPreview {
  status: "pending" | "accepted" | "revoked" | "expired";
  username: string;
  display_name: string;
  org_name: string | null;
  team_name: string | null;
  role_name: string | null;
  expires_at: number;
}

export interface AcceptInvitationResponse {
  user_id: string;
  username: string;
  session_token: string;
  header: string;
}

/** 接受页在用户输入口令前先展示"这个邀请是谁的、还有效吗"；查无此 token 404。
 *  token 放请求体、不放 URL——与 POST /api/auth/sso/exchange 同一条已有教训
 *  （避免一次性凭证明文落进 nginx access log / 浏览器历史 / 服务端审计行的
 *  path 字段），后端路由是 POST /invite/preview，不是 GET /invite/{token}。 */
export function previewInvitation(token: string): Promise<InvitationPreview> {
  return request("POST", "/invite/preview", { token });
}

/** 设口令 → 建账号 → 直接登录，成功后把签发的会话令牌记进内存。密码策略
 *  不满足时后端抛 422，`err.detail.violations` 带逐条具体原因。同上，token
 *  放请求体。 */
export async function acceptInvitation(token: string, password: string): Promise<AcceptInvitationResponse> {
  const data = (await request(
    "POST", "/invite/accept", { token, password },
  )) as AcceptInvitationResponse;
  setSessionToken(data.session_token);
  return data;
}
