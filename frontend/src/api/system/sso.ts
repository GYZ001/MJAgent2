import { get, mutate } from "../client";

/** 管理面 IdP 配置域：与 `app/sso/admin_api.py::_idp_public_payload` 逐字
 *  对应。`client_secret` 只写不读——后端从不回显明文，这里也没有承载明文的
 *  字段，只有 `has_client_secret` 这个布尔位。 */
export type IdpKind = "oidc" | "wecom" | "feishu" | "dingtalk";

export interface IdpRow {
  id: string;
  org_id: string | null;
  kind: IdpKind;
  name: string;
  enabled: boolean;
  issuer: string | null;
  client_id: string;
  has_client_secret: boolean;
  discovery_url: string | null;
  authorize_url: string | null;
  token_url: string | null;
  userinfo_url: string | null;
  jwks_url: string | null;
  scopes: string;
  claim_map_json: string;
  provision_json: string;
  allowed_domains: string | null;
  created_at: number;
  updated_at: number;
  /** 是否对接过真实 IdP 服务器验证过（见 app/sso/profiles.py 模块文档
   *  "互通验证状态是诚实标注,不是宣传"）：目前只有 oidc 为 true，企业微信/
   *  飞书/钉钉均为 false——界面必须把这一点显式标出，不能让"支持"两个字
   *  看起来像"已验证"。 */
  interop_verified: boolean;
  interop_note: string;
}

/** 新建/更新 IdP 的请求体；全部字段可选——留空的字段由后端按 `kind` 对应
 *  的 profile 默认值填充（新增一家走既有协议族的 IdP 不需要新代码，见
 *  app/sso/profiles.py 模块文档）。`client_secret` 缺省 = 创建时不设置 /
 *  更新时保持原值不变（后端按 body 里是否携带这个 key 判断，不是按值是否
 *  为空——调用方留空就完全不要带这个 key）。 */
export interface IdpWriteBody {
  kind?: IdpKind;
  name?: string;
  client_id?: string;
  client_secret?: string;
  issuer?: string;
  discovery_url?: string;
  authorize_url?: string;
  token_url?: string;
  userinfo_url?: string;
  jwks_url?: string;
  scopes?: string;
  claim_map?: Record<string, string>;
  provision?: Record<string, unknown>;
  allowed_domains?: string;
  enabled?: boolean;
  org_id?: string | null;
}

export function listIdps(): Promise<{ items: IdpRow[] }> {
  return get("/admin/sso/providers");
}

export function createIdp(body: IdpWriteBody): Promise<IdpRow> {
  return mutate("POST", "/admin/sso/providers", body) as Promise<IdpRow>;
}

export function updateIdp(idpId: string, body: IdpWriteBody): Promise<IdpRow> {
  return mutate("PUT", `/admin/sso/providers/${encodeURIComponent(idpId)}`, body) as Promise<IdpRow>;
}

export function deleteIdp(idpId: string): Promise<{ ok: boolean }> {
  return mutate("DELETE", `/admin/sso/providers/${encodeURIComponent(idpId)}`);
}

export type LocalLoginPolicy = "enabled" | "admin_only" | "disabled";

export function getLocalLoginPolicy(): Promise<{ policy: LocalLoginPolicy }> {
  return get("/admin/sso/local-login-policy");
}

/** 切到 `disabled` 前后端会真的走一遍连通性自检，失败会 422 并带
 *  `{message, failures: string[]}`——`ApiError.detail` 就是这个对象，
 *  调用方要把 `failures` 逐条显示，不能只说"保存失败"（CLAUDE.md 明令）。 */
export function setLocalLoginPolicy(policy: LocalLoginPolicy): Promise<{ policy: LocalLoginPolicy }> {
  return mutate("PUT", "/admin/sso/local-login-policy", { policy });
}
