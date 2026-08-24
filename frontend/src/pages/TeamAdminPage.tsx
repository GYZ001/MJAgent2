import { useEffect, useId, useState } from "react";
import { api, ApiError } from "../api";
import { roleLabel, type WorkspaceRole } from "../auth/session";

/** 「成员与团队」——系统管理员专属，是「管理员开户、无自助注册」在产品里的
 *  唯一落地入口；此前只有 scripts/create_admin.py 一条命令行路径。
 *  独立成页而不挂进 MonitorPage：那个文件当前改动频繁，新功能单独放一处
 *  减少冲突面，代价是与总览/模型中心视觉上略有割裂，可接受。 */

const ROLES: WorkspaceRole[] = ["workspace_admin", "production", "review", "readonly"];

interface WorkspaceMembershipRow {
  id: string;
  name: string;
  role: string;
}

interface UserRow {
  id: string;
  username: string;
  display_name: string;
  status: "active" | "disabled";
  is_system_admin: boolean;
  must_change_password: boolean;
  created_at: number;
  last_login_at: number | null;
  workspaces: WorkspaceMembershipRow[];
}

interface WorkspaceRow {
  id: string;
  name: string;
  member_count: number;
  project_count: number;
}

function formatTime(epochSeconds: number | null): string {
  if (!epochSeconds) return "从未";
  return new Date(epochSeconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

export default function TeamAdminPage() {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ text: string; err: boolean } | null>(null);

  const [newTeamName, setNewTeamName] = useState("");
  const [form, setForm] = useState({
    username: "", password: "", displayName: "", workspaceId: "", role: "readonly" as WorkspaceRole,
  });

  const usernameId = useId();
  const passwordId = useId();
  const displayNameId = useId();
  const teamNameId = useId();

  const load = async () => {
    setError(null);
    try {
      const [u, w] = await Promise.all([
        api.get("/system/users") as Promise<{ items: UserRow[] }>,
        api.get("/system/workspaces") as Promise<{ items: WorkspaceRow[] }>,
      ]);
      setUsers(u.items);
      setWorkspaces(w.items);
      if (!form.workspaceId && w.items.length) {
        setForm((current) => ({ ...current, workspaceId: w.items[0].id }));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const notify = (text: string, isErr = false) => {
    setToast({ text, err: isErr });
    window.setTimeout(() => setToast(null), isErr ? 6000 : 2600);
  };

  const runAction = async (fn: () => Promise<unknown>, doneMsg: string) => {
    setBusy(true);
    try {
      await fn();
      notify(doneMsg);
      await load();
    } catch (err) {
      notify(err instanceof ApiError ? err.message : String(err), true);
    } finally {
      setBusy(false);
    }
  };

  const createTeam = () => {
    const name = newTeamName.trim();
    if (!name) { notify("团队名称不能为空", true); return; }
    void runAction(() => api.post("/system/workspaces", { name }), `团队「${name}」已创建`).then(() => setNewTeamName(""));
  };

  const createUser = () => {
    if (!form.username.trim() || form.password.length < 8) {
      notify("用户名不能为空，密码至少 8 位", true);
      return;
    }
    void runAction(
      () => api.post("/system/users", {
        username: form.username.trim(),
        password: form.password,
        display_name: form.displayName.trim() || undefined,
        workspace_id: form.workspaceId || undefined,
        role: form.role,
      }),
      `账号「${form.username.trim()}」已创建`,
    ).then(() => setForm((current) => ({ ...current, username: "", password: "", displayName: "" })));
  };

  const changeRole = (workspaceId: string, userId: string, role: string) => {
    void runAction(
      () => api.put(`/system/workspaces/${workspaceId}/members/${userId}`, { role }),
      "角色已更新",
    );
  };

  const removeMember = (workspaceId: string, userId: string, username: string) => {
    if (!window.confirm(`把「${username}」移出这个团队？`)) return;
    void runAction(
      () => api.del(`/system/workspaces/${workspaceId}/members/${userId}`),
      `已把「${username}」移出团队`,
    );
  };

  const addMember = (userId: string, username: string, workspaceId: string, role: string) => {
    void runAction(
      () => api.put(`/system/workspaces/${workspaceId}/members/${userId}`, { role }),
      `已把「${username}」加入团队`,
    );
  };

  const toggleStatus = (u: UserRow) => {
    const next = u.status === "active" ? "disabled" : "active";
    if (next === "disabled" && !window.confirm(`禁用账号「${u.username}」？该账号的现有登录会立即失效。`)) return;
    void runAction(() => api.put(`/system/users/${u.id}`, { status: next }), `账号「${u.username}」已${next === "active" ? "启用" : "禁用"}`);
  };

  const resetPassword = (u: UserRow) => {
    const next = window.prompt(`为「${u.username}」设置新密码（至少 8 位，对方需重新登录）：`);
    if (!next) return;
    if (next.length < 8) { notify("密码至少 8 位", true); return; }
    void runAction(() => api.put(`/system/users/${u.id}`, { password: next }), "密码已重置");
  };

  const toggleSystemAdmin = (u: UserRow) => {
    const next = !u.is_system_admin;
    if (!window.confirm(next ? `把「${u.username}」设为系统管理员？` : `取消「${u.username}」的系统管理员身份？`)) return;
    void runAction(() => api.put(`/system/users/${u.id}`, { is_system_admin: next }), "已更新");
  };

  return (
    <div className="team-admin">
      <header className="desk-head">
        <h1>成员与团队</h1>
        <p className="sub">开户、分配团队角色、启停账号——只有系统管理员能看到这一页。</p>
        <hr className="rule" />
      </header>

      {error && (
        <div className="empty query-error" role="alert">
          <strong>加载失败</strong>
          <p>{error}</p>
          <button type="button" className="btn" onClick={() => void load()}>重试</button>
        </div>
      )}

      <section className="card team-admin-section">
        <h2>新建团队</h2>
        <div className="team-admin-inline-form">
          <label className="f" htmlFor={teamNameId}>
            团队名称
            <input id={teamNameId} value={newTeamName} onChange={(e) => setNewTeamName(e.target.value)}
              placeholder="例如：制作二组" disabled={busy} />
          </label>
          <button type="button" className="btn primary" disabled={busy} onClick={createTeam}>创建团队</button>
        </div>
        <table className="team-admin-table">
          <thead><tr><th>团队</th><th>成员数</th><th>项目数</th></tr></thead>
          <tbody>
            {(workspaces ?? []).map((w) => (
              <tr key={w.id}><td>{w.name}</td><td>{w.member_count}</td><td>{w.project_count}</td></tr>
            ))}
            {workspaces && !workspaces.length && <tr><td colSpan={3}>暂无团队</td></tr>}
          </tbody>
        </table>
      </section>

      <section className="card team-admin-section">
        <h2>新建账号</h2>
        <div className="team-admin-form-grid">
          <label className="f" htmlFor={usernameId}>
            用户名
            <input id={usernameId} value={form.username} disabled={busy}
              onChange={(e) => setForm((c) => ({ ...c, username: e.target.value }))} />
          </label>
          <label className="f" htmlFor={displayNameId}>
            显示名（可选）
            <input id={displayNameId} value={form.displayName} disabled={busy}
              onChange={(e) => setForm((c) => ({ ...c, displayName: e.target.value }))} />
          </label>
          <label className="f" htmlFor={passwordId}>
            初始密码（至少 8 位）
            <input id={passwordId} type="text" value={form.password} disabled={busy}
              onChange={(e) => setForm((c) => ({ ...c, password: e.target.value }))} />
          </label>
          <label className="f">
            加入团队
            <select value={form.workspaceId} disabled={busy}
              onChange={(e) => setForm((c) => ({ ...c, workspaceId: e.target.value }))}>
              <option value="">（暂不加入，之后再分配）</option>
              {(workspaces ?? []).map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
          </label>
          <label className="f">
            初始角色
            <select value={form.role} disabled={busy || !form.workspaceId}
              onChange={(e) => setForm((c) => ({ ...c, role: e.target.value as WorkspaceRole }))}>
              {ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
            </select>
          </label>
        </div>
        <button type="button" className="btn primary" disabled={busy} onClick={createUser}>创建账号</button>
      </section>

      <section className="card team-admin-section">
        <h2>全部账号</h2>
        <table className="team-admin-table">
          <thead>
            <tr>
              <th>用户名</th><th>显示名</th><th>状态</th><th>系统管理员</th>
              <th>所属团队 / 角色</th><th>最后登录</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {(users ?? []).map((u) => (
              <tr key={u.id} className={u.status === "disabled" ? "team-admin-row-disabled" : undefined}>
                <td>{u.username}</td>
                <td>{u.display_name}</td>
                <td>{u.status === "active" ? "启用中" : "已禁用"}</td>
                <td>{u.is_system_admin ? "是" : "否"}</td>
                <td>
                  {u.workspaces.length === 0 && <span className="team-admin-muted">未加入任何团队</span>}
                  {u.workspaces.map((m) => (
                    <div key={m.id} className="team-admin-member-row">
                      <span>{m.name}</span>
                      <select value={m.role} disabled={busy}
                        onChange={(e) => changeRole(m.id, u.id, e.target.value)}>
                        {ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
                      </select>
                      <button type="button" className="btn small ghost" disabled={busy}
                        onClick={() => removeMember(m.id, u.id, u.username)}>移出</button>
                    </div>
                  ))}
                  {(workspaces ?? []).filter((w) => !u.workspaces.some((m) => m.id === w.id)).length > 0 && (
                    <AddToTeamControl
                      candidates={(workspaces ?? []).filter((w) => !u.workspaces.some((m) => m.id === w.id))}
                      disabled={busy}
                      onAdd={(workspaceId, role) => addMember(u.id, u.username, workspaceId, role)}
                    />
                  )}
                </td>
                <td>{formatTime(u.last_login_at)}</td>
                <td className="team-admin-actions">
                  <button type="button" className="btn small" disabled={busy} onClick={() => resetPassword(u)}>重置密码</button>
                  <button type="button" className="btn small ghost" disabled={busy} onClick={() => toggleSystemAdmin(u)}>
                    {u.is_system_admin ? "取消管理员" : "设为管理员"}
                  </button>
                  <button type="button" className={`btn small ${u.status === "active" ? "danger ghost" : ""}`}
                    disabled={busy} onClick={() => toggleStatus(u)}>
                    {u.status === "active" ? "禁用" : "启用"}
                  </button>
                </td>
              </tr>
            ))}
            {users && !users.length && <tr><td colSpan={7}>暂无账号</td></tr>}
          </tbody>
        </table>
      </section>

      {toast && <div role="status" className={`toast ${toast.err ? "err" : ""}`}>{toast.text}</div>}
    </div>
  );
}

function AddToTeamControl({ candidates, disabled, onAdd }: {
  candidates: WorkspaceRow[];
  disabled: boolean;
  onAdd: (workspaceId: string, role: string) => void;
}) {
  const [workspaceId, setWorkspaceId] = useState(candidates[0]?.id ?? "");
  const [role, setRole] = useState<WorkspaceRole>("readonly");
  useEffect(() => {
    if (!candidates.some((c) => c.id === workspaceId)) setWorkspaceId(candidates[0]?.id ?? "");
  }, [candidates, workspaceId]);
  if (!workspaceId) return null;
  return (
    <div className="team-admin-member-row team-admin-add-row">
      <select value={workspaceId} disabled={disabled} onChange={(e) => setWorkspaceId(e.target.value)}>
        {candidates.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
      </select>
      <select value={role} disabled={disabled} onChange={(e) => setRole(e.target.value as WorkspaceRole)}>
        {ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
      </select>
      <button type="button" className="btn small" disabled={disabled} onClick={() => onAdd(workspaceId, role)}>加入</button>
    </div>
  );
}
