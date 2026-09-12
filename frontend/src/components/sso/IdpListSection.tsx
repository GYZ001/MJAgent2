import { useEffect, useState } from "react";
import { ApiError, deleteIdp, listIdps, updateIdp, type IdpRow } from "../../api";
import IdpFormDialog from "./IdpFormDialog";

/** IdP 配置列表：新增/编辑/启停/删除，以及每一行都显式标出
 *  `interop_verified`——企业微信/飞书/钉钉三家目前是 false（未经真实互联
 *  验证，见 app/sso/profiles.py 模块文档），界面不许让它们看起来像已经
 *  验证过。 */
export default function IdpListSection() {
  const [items, setItems] = useState<IdpRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<IdpRow | null | "new">(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const reload = () => {
    listIdps()
      .then((data) => setItems(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : "加载失败"));
  };
  useEffect(reload, []);

  const toggleEnabled = async (idp: IdpRow) => {
    setBusyId(idp.id);
    setError(null);
    try {
      await updateIdp(idp.id, { enabled: !idp.enabled });
      reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "切换失败，请稍后重试");
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (idp: IdpRow) => {
    if (!window.confirm(`确认删除「${idp.name}」？已绑定该 IdP 的用户将无法再用它登录。`)) return;
    setBusyId(idp.id);
    setError(null);
    try {
      await deleteIdp(idp.id);
      reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "删除失败，请稍后重试");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="card">
      <div className="card-heading-row">
        <h3>身份提供方</h3>
        <div className="card-heading-actions">
          <button type="button" className="btn small primary" onClick={() => setEditing("new")}>新增</button>
        </div>
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
      {items === null && <p className="hint">正在加载…</p>}
      {items?.length === 0 && <p className="hint">尚未配置任何身份提供方，登录页不会出现企业登录入口。</p>}
      {items?.map((idp) => (
        <IdpRowView
          key={idp.id}
          idp={idp}
          busy={busyId === idp.id}
          onToggle={() => toggleEnabled(idp)}
          onEdit={() => setEditing(idp)}
          onDelete={() => remove(idp)}
        />
      ))}
      {editing !== null && (
        <IdpFormDialog
          idp={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
    </div>
  );
}

function IdpRowView({
  idp, busy, onToggle, onEdit, onDelete,
}: {
  idp: IdpRow; busy: boolean; onToggle: () => void; onEdit: () => void; onDelete: () => void;
}) {
  return (
    <div className="login-field sso-idp-row">
      <div>
        <b>{idp.name}</b>
        <span className="hint"> · {idp.kind} · {idp.enabled ? "已启用" : "已停用"}</span>
        <p className={idp.interop_verified ? "hint" : "field-error"}>
          {idp.interop_verified ? "已验证" : "⚠ 未经验证"}：{idp.interop_note}
        </p>
      </div>
      <div className="dialog-actions">
        <button type="button" className="btn small" disabled={busy} onClick={onToggle}>
          {idp.enabled ? "停用" : "启用"}
        </button>
        <button type="button" className="btn small" disabled={busy} onClick={onEdit}>编辑</button>
        <button type="button" className="btn small danger" disabled={busy} onClick={onDelete}>删除</button>
      </div>
    </div>
  );
}
