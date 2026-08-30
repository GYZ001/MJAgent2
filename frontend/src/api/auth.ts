import type { AuthUser, WorkspaceMembership } from "../auth/session";
import { request, setSessionToken } from "./client";

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
