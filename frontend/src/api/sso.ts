import { get, mutate, request, setSessionToken } from "./client";

/** 登录页可见的 IdP 入口，字段与 `GET /api/auth/sso/providers` 逐字对应
 *  （后端只回 id/name/kind，不回任何密文，见 app/sso/api.py::list_providers）。 */
export interface SsoProviderOption {
  id: string;
  name: string;
  kind: "oidc" | "wecom" | "feishu" | "dingtalk";
}

/** 未登录时拉取启用中的 IdP 列表；一个都没有时 `items` 为空数组，调用方应
 *  完全不渲染该区块，不留空壳。 */
export function listSsoProviders(): Promise<{ items: SsoProviderOption[] }> {
  return get("/auth/sso/providers");
}

/** 发起登录用的真实跳转地址（302 到 IdP），不经 fetch()——调用方直接
 *  `window.location.href = ssoStartUrl(...)`。`redirectTo` 只允许本站相对
 *  路径，后端会再校验一遍（PRD EP-02 §10 陷阱 3：开放重定向）。 */
export function ssoStartUrl(idpId: string, redirectTo: string): string {
  const params = new URLSearchParams({ redirect_to: redirectTo || "/" });
  return `/api/auth/sso/${encodeURIComponent(idpId)}/start?${params.toString()}`;
}

/** `POST /api/auth/sso/exchange` 的响应体：只有 session_token/header，不含
 *  用户信息（与 `login()` 的响应形状不同）——调用方拿到令牌后应立即调
 *  `AuthContext.refresh()`（即 `GET /auth/me`）补齐用户信息。 */
export interface SsoExchangeResponse {
  session_token: string;
  header: string;
}

/** 用回跳 URL 上的一次性交换码换取真会话令牌，并立刻记进内存/localStorage
 *  （与 `login()` 同一套会话存储，见 client.ts 的 `setSessionToken`）。
 *  交换码 60 秒 TTL、单次消费，失败（过期/重放/无效）统一 400，不区分原因
 *  ——与后端 `sso_exchange()` 的口径一致，调用方不应尝试区分展示。 */
export async function exchangeSsoCode(code: string): Promise<SsoExchangeResponse> {
  const data = (await request("POST", "/auth/sso/exchange", { code })) as SsoExchangeResponse;
  setSessionToken(data.session_token);
  return data;
}

/** 已登录用户当前绑定的 IdP 身份（`GET /api/auth/sso/my-identities`，
 *  2026-09-12 前端接入时补的读接口——没有它，绑定/解绑界面只能对状态说谎）。 */
export interface MyIdentity {
  idp_id: string;
  idp_name: string;
  idp_kind: SsoProviderOption["kind"] | null;
  linked_at: number;
  last_login_at: number | null;
}

export function listMyIdentities(): Promise<{ items: MyIdentity[] }> {
  return get("/auth/sso/my-identities");
}

/** 已登录用户发起「绑定 IdP」：与匿名登录复用同一条 callback，返回的是
 *  JSON `{authorize_url}` 而不是直接 302——调用方自己 `window.location.href`
 *  跳过去（见 app/sso/api.py 模块文档"与 PRD 字面描述有一处实现差异"）。 */
export function startSsoLink(idpId: string, redirectTo: string): Promise<{ authorize_url: string }> {
  return mutate("POST", "/auth/sso/link", { idp_id: idpId, redirect_to: redirectTo });
}

/** 解绑；最后一个登录方式解绑会被后端 422 拒绝，调用方要把原因原样显示
 *  （CLAUDE.md「拦人必须给出路」）。 */
export function unlinkSso(idpId: string): Promise<{ ok: boolean }> {
  return mutate("DELETE", `/auth/sso/link/${encodeURIComponent(idpId)}`);
}
