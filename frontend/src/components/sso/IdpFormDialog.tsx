import { useId, useState, type FormEvent } from "react";
import { ApiError, createIdp, updateIdp, type IdpKind, type IdpRow, type IdpWriteBody } from "../../api";
import { useFocusTrap } from "../../hooks/useFocusTrap";

const KIND_OPTIONS: { value: IdpKind; label: string }[] = [
  { value: "oidc", label: "通用 OIDC（Entra ID / Okta / Keycloak / Authing 等）" },
  { value: "wecom", label: "企业微信" },
  { value: "feishu", label: "飞书" },
  { value: "dingtalk", label: "钉钉" },
];

/** 未经真实互联验证的 kind——见 app/sso/profiles.py 模块文档"互通验证状态
 *  是诚实标注,不是宣传"。界面不许在没验证的地方看起来像验证过。 */
const UNVERIFIED_KINDS = new Set<IdpKind>(["wecom", "feishu", "dingtalk"]);

interface FormState {
  kind: IdpKind;
  name: string;
  client_id: string;
  client_secret: string;
  issuer: string;
  discovery_url: string;
  authorize_url: string;
  token_url: string;
  userinfo_url: string;
  jwks_url: string;
  scopes: string;
  allowed_domains: string;
  claim_map: string;
  provision: string;
  enabled: boolean;
}

function initialState(idp: IdpRow | null): FormState {
  return {
    kind: idp?.kind ?? "oidc",
    name: idp?.name ?? "",
    client_id: idp?.client_id ?? "",
    client_secret: "",
    issuer: idp?.issuer ?? "",
    discovery_url: idp?.discovery_url ?? "",
    authorize_url: idp?.authorize_url ?? "",
    token_url: idp?.token_url ?? "",
    userinfo_url: idp?.userinfo_url ?? "",
    jwks_url: idp?.jwks_url ?? "",
    scopes: idp?.scopes ?? "",
    allowed_domains: idp?.allowed_domains ?? "",
    claim_map: idp?.claim_map_json ?? "",
    provision: idp?.provision_json ?? "",
    enabled: idp?.enabled ?? false,
  };
}

/** 把表单状态转成写请求体；空字符串一律不带上，好让后端用该 kind 的
 *  profile 默认值兜底（新增一家走既有协议族的 IdP 不需要新代码，见
 *  app/sso/profiles.py）。`client_secret` 留空则完全不带这个 key——创建时
 *  等于"不设置"，编辑时等于"保持原值不变"（后端按 key 是否存在判断）。 */
function buildBody(form: FormState): IdpWriteBody | { jsonError: string } {
  let claimMap: Record<string, string> | undefined;
  let provision: Record<string, unknown> | undefined;
  try {
    claimMap = form.claim_map.trim() ? JSON.parse(form.claim_map) : undefined;
  } catch {
    return { jsonError: "Claim Map 不是合法 JSON" };
  }
  try {
    provision = form.provision.trim() ? JSON.parse(form.provision) : undefined;
  } catch {
    return { jsonError: "开户映射规则不是合法 JSON" };
  }
  const body: IdpWriteBody = {
    kind: form.kind, name: form.name.trim(), client_id: form.client_id.trim(),
    issuer: form.issuer || undefined, discovery_url: form.discovery_url || undefined,
    authorize_url: form.authorize_url || undefined, token_url: form.token_url || undefined,
    userinfo_url: form.userinfo_url || undefined, jwks_url: form.jwks_url || undefined,
    scopes: form.scopes || undefined, allowed_domains: form.allowed_domains || undefined,
    claim_map: claimMap, provision, enabled: form.enabled,
  };
  if (form.client_secret.trim()) body.client_secret = form.client_secret.trim();
  return body;
}

/** 新增/编辑 IdP 的表单弹窗。 */
export default function IdpFormDialog({
  idp, onClose, onSaved,
}: { idp: IdpRow | null; onClose: () => void; onSaved: (row: IdpRow) => void }) {
  const titleId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [form, setForm] = useState<FormState>(() => initialState(idp));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;
    if (!form.name.trim() || !form.client_id.trim()) {
      setError("名称与 Client ID 不能为空");
      return;
    }
    const built = buildBody(form);
    if ("jsonError" in built) {
      setError(built.jsonError);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const saved = idp ? await updateIdp(idp.id, built) : await createIdp(built);
      onSaved(saved);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败，请检查网络后重试");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="evidence-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section ref={trapRef} className="impact-dialog decision-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <form onSubmit={submit}>
          <h3 id={titleId}>{idp ? `编辑身份提供方：${idp.name}` : "新增身份提供方"}</h3>
          <div className="login-field">
            <label className="f" htmlFor="idp-kind">类型</label>
            <select id="idp-kind" value={form.kind} disabled={saving} onChange={(e) => set("kind", e.target.value as IdpKind)}>
              {KIND_OPTIONS.map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
            </select>
          </div>
          {UNVERIFIED_KINDS.has(form.kind) && (
            <p className="field-error" role="note">
              ⚠ 未经真实互联验证：本环境没有可用的企业微信/飞书/钉钉沙箱账号，接入前请先用真实测试账号跑通一次完整登录再上生产。
            </p>
          )}
          <TextRow id="idp-name" label="名称" value={form.name} onChange={(v) => set("name", v)} disabled={saving} />
          <TextRow id="idp-client-id" label="Client ID" value={form.client_id} onChange={(v) => set("client_id", v)} disabled={saving} />
          <TextRow
            id="idp-client-secret"
            label={idp?.has_client_secret ? "Client Secret（已配置，留空则不变）" : "Client Secret（可留空）"}
            value={form.client_secret} onChange={(v) => set("client_secret", v)} disabled={saving} type="password"
          />
          <TextRow id="idp-issuer" label="Issuer" value={form.issuer} onChange={(v) => set("issuer", v)} disabled={saving} />
          <TextRow id="idp-discovery" label="Discovery URL" value={form.discovery_url} onChange={(v) => set("discovery_url", v)} disabled={saving} />
          <TextRow id="idp-authorize" label="Authorize URL（无 discovery 时必填）" value={form.authorize_url} onChange={(v) => set("authorize_url", v)} disabled={saving} />
          <TextRow id="idp-token" label="Token URL" value={form.token_url} onChange={(v) => set("token_url", v)} disabled={saving} />
          <TextRow id="idp-userinfo" label="Userinfo URL" value={form.userinfo_url} onChange={(v) => set("userinfo_url", v)} disabled={saving} />
          <TextRow id="idp-jwks" label="JWKS URL" value={form.jwks_url} onChange={(v) => set("jwks_url", v)} disabled={saving} />
          <TextRow id="idp-scopes" label="Scopes（留空用该类型默认值）" value={form.scopes} onChange={(v) => set("scopes", v)} disabled={saving} />
          <TextRow id="idp-domains" label="允许的邮箱域（留空 = 不限）" value={form.allowed_domains} onChange={(v) => set("allowed_domains", v)} disabled={saving} />
          <div className="login-field">
            <label className="f" htmlFor="idp-claim-map">Claim Map（JSON，留空用该类型默认值）</label>
            <textarea id="idp-claim-map" rows={3} disabled={saving} value={form.claim_map} onChange={(e) => set("claim_map", e.target.value)} />
          </div>
          <div className="login-field">
            <label className="f" htmlFor="idp-provision">开户映射规则（JSON，留空默认 auto_create）</label>
            <textarea id="idp-provision" rows={4} disabled={saving} value={form.provision} onChange={(e) => set("provision", e.target.value)} />
          </div>
          <div className="login-field">
            <label className="f">
              <input type="checkbox" checked={form.enabled} disabled={saving} onChange={(e) => set("enabled", e.target.checked)} /> 启用
            </label>
          </div>
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="dialog-actions">
            <button type="button" className="btn" onClick={onClose} disabled={saving}>取消</button>
            <button type="submit" className="btn primary" disabled={saving}>{saving ? "保存中…" : "保存"}</button>
          </div>
        </form>
      </section>
    </div>
  );
}

function TextRow({
  id, label, value, onChange, disabled, type = "text",
}: {
  id: string; label: string; value: string; onChange: (v: string) => void; disabled: boolean; type?: string;
}) {
  return (
    <div className="login-field">
      <label className="f" htmlFor={id}>{label}</label>
      <input id={id} type={type} disabled={disabled} value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
