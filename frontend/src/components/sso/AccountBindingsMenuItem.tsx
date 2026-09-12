import { useEffect, useId, useState } from "react";
import {
  ApiError, listMyIdentities, listSsoProviders, startSsoLink, unlinkSso,
  type MyIdentity, type SsoProviderOption,
} from "../../api";
import { useFocusTrap } from "../../hooks/useFocusTrap";

/** UserMenu 里的「账号绑定」入口（EP-02 第二阶段）：零 IdP 配置时整个入口
 *  都不渲染，不留空壳（CLAUDE.md「不要留空壳」）；有 IdP 时才展示按钮，
 *  点开才去拉「我当前绑了哪些」（`GET /auth/sso/my-identities`，见该路由
 *  docstring——没有它，绑定/解绑界面只能对状态说谎或干脆不显示）。 */
export default function AccountBindingsMenuItem() {
  const [open, setOpen] = useState(false);
  const [providers, setProviders] = useState<SsoProviderOption[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    listSsoProviders()
      .then((data) => {
        if (!cancelled) setProviders(data.items);
      })
      .catch(() => {
        if (!cancelled) setProviders([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (providers !== null && providers.length === 0) return null;

  return (
    <>
      <button type="button" disabled={providers === null} onClick={() => setOpen(true)}>
        账号绑定
      </button>
      {open && providers && (
        <AccountBindingsDialog providers={providers} onClose={() => setOpen(false)} />
      )}
    </>
  );
}

function AccountBindingsDialog({
  providers,
  onClose,
}: {
  providers: SsoProviderOption[];
  onClose: () => void;
}) {
  const titleId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [identities, setIdentities] = useState<MyIdentity[] | null>(null);
  const [busyIdpId, setBusyIdpId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadIdentities = () => {
    listMyIdentities()
      .then((data) => setIdentities(data.items))
      .catch(() => setIdentities([]));
  };
  useEffect(loadIdentities, []);

  const boundIdpIds = new Set((identities ?? []).map((identity) => identity.idp_id));

  const bind = async (idpId: string) => {
    setBusyIdpId(idpId);
    setError(null);
    try {
      const { authorize_url } = await startSsoLink(idpId, `${window.location.pathname}${window.location.search}`);
      window.location.href = authorize_url;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "发起绑定失败，请稍后重试");
      setBusyIdpId(null);
    }
  };

  // 解绑最后一个登录方式后端会 422 拒绝——原样显示原因（CLAUDE.md「拦人
  // 必须给出路」），不吞掉、不说成通用的"操作失败"。
  const unbind = async (idpId: string) => {
    setBusyIdpId(idpId);
    setError(null);
    try {
      await unlinkSso(idpId);
      loadIdentities();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "解绑失败，请稍后重试");
    } finally {
      setBusyIdpId(null);
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
      <section
        ref={trapRef}
        className="impact-dialog decision-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <h3 id={titleId}>账号绑定</h3>
        <p className="hint">绑定后可以用企业账号登录；解绑最后一个登录方式会被拒绝。</p>
        {error && <p className="field-error" role="alert">{error}</p>}
        <ul className="sso-binding-list">
          {providers.map((provider) => {
            const bound = boundIdpIds.has(provider.id);
            const busy = busyIdpId === provider.id;
            return (
              <li key={provider.id}>
                <span>{provider.name}</span>
                {identities === null ? (
                  <span className="hint">加载中…</span>
                ) : bound ? (
                  <button type="button" className="btn small" disabled={busy} onClick={() => unbind(provider.id)}>
                    {busy ? "处理中…" : "解绑"}
                  </button>
                ) : (
                  <button type="button" className="btn small primary" disabled={busy} onClick={() => bind(provider.id)}>
                    {busy ? "跳转中…" : "绑定"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>关闭</button>
        </div>
      </section>
    </div>
  );
}
