import { useEffect, useState } from "react";
import { api, ApiError, type PermissionPoint, type RoleReferenceConflict, type RoleRow } from "../../api";
import RoleEditorDialog from "./RoleEditorDialog";

/** 账号管理——「角色」标签页（EP-01 第二阶段）：内置模板只读展示 + 自定义角色
 *  创建/编权限点/删除。内置模板改权限点/删除会被后端 422 拒绝——本页不隐藏
 *  这两个按钮，直接把后端拒绝原因显示出来，比"猜哪些角色可以点"更可靠。 */
export default function RolesTab() {
  const [roles, setRoles] = useState<RoleRow[] | null>(null);
  const [catalog, setCatalog] = useState<PermissionPoint[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<RoleReferenceConflict | null>(null);
  const [busy, setBusy] = useState(false);
  const [editorRole, setEditorRole] = useState<RoleRow | null>(null);
  const [creating, setCreating] = useState(false);

  const load = async () => {
    setError(null);
    try {
      const res = await api.listRoles();
      setRoles(res.items);
      setCatalog(res.permission_catalog);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => { void load(); }, []);

  const templates = (roles ?? []).filter((r) => r.builtin);

  const submitEditor = async (draft: {
    key: string; name: string; description: string; fromTemplate: string; permissionKeys: string[];
  }) => {
    setBusy(true);
    setError(null);
    try {
      if (editorRole) {
        await api.updateRolePermissions(editorRole.id, draft.permissionKeys);
      } else {
        if (!draft.key.trim() || !draft.name.trim()) {
          setError("角色 key 与名称不能为空");
          setBusy(false);
          return;
        }
        await api.createRole({
          key: draft.key.trim(), name: draft.name.trim(), description: draft.description.trim() || undefined,
          permission_keys: draft.permissionKeys, from_template: draft.fromTemplate || undefined,
        });
      }
      setEditorRole(null);
      setCreating(false);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const deleteRole = async (role: RoleRow) => {
    setBusy(true);
    setError(null);
    setConflict(null);
    try {
      await api.deleteRole(role.id);
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setConflict(err.detail as RoleReferenceConflict);
      } else {
        setError(err instanceof ApiError ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {error && <div className="empty query-error" role="alert"><strong>操作失败</strong><p>{error}</p></div>}
      {conflict && (
        <div className="empty query-error" role="alert">
          <strong>{conflict.message}</strong>
          <ul>
            {conflict.team_members.map((m) => (
              <li key={`${m.team_id}:${m.user_id}`}>团队「{m.team_name}」成员 {m.user_id}</li>
            ))}
            {conflict.project_grants.map((g) => (
              <li key={`${g.project_id}:${g.subject_id}`}>项目 {g.project_id} 授权给 {g.subject_type === "user" ? "账号" : "团队"} {g.subject_id}</li>
            ))}
          </ul>
          <button type="button" className="btn small" onClick={() => setConflict(null)}>知道了</button>
        </div>
      )}

      <div className="account-admin-bar">
        <h3>角色</h3>
        <button type="button" className="btn primary" disabled={busy} onClick={() => setCreating(true)}>新建角色</button>
      </div>

      {!roles && !error && <p className="account-admin-muted">载入中…</p>}
      <div className="account-admin-cards">
        {(roles ?? []).map((role) => (
          <div key={role.id} className="card role-card">
            <div className="team-card-head">
              <h3>{role.name}</h3>
              <span className={`stamp ${role.builtin ? "blue" : "grey"}`}>{role.builtin ? "内置模板" : "自定义"}</span>
            </div>
            {role.description && <p className="sub">{role.description}</p>}
            <p className="account-admin-muted">{role.permission_keys.length} 个权限点</p>
            <div className="dialog-actions">
              <button type="button" className="btn small ghost" disabled={busy} onClick={() => setEditorRole(role)}>
                {role.builtin ? "查看权限点" : "编辑权限点"}
              </button>
              {!role.builtin && (
                <button type="button" className="btn small ghost danger" disabled={busy}
                  onClick={() => void deleteRole(role)}>删除</button>
              )}
            </div>
          </div>
        ))}
      </div>

      {(creating || editorRole) && (
        <RoleEditorDialog
          role={editorRole}
          catalog={catalog}
          templates={templates}
          busy={busy}
          onClose={() => { setCreating(false); setEditorRole(null); }}
          onSubmit={(draft) => void submitEditor(draft)}
        />
      )}
    </>
  );
}
