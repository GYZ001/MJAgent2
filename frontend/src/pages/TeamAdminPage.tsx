import { useEffect, useId, useState, type FormEvent } from "react";
import { api, ApiError, type UserRow, type WorkspaceRow } from "../api";
import { roleLabel, type WorkspaceRole } from "../auth/session";
import { useFocusTrap } from "../hooks/useFocusTrap";
import DecisionDialog from "../components/DecisionDialog";
import "../styles/TeamAdminPage.css";

/** 「成员与团队」——系统管理员专属，是「管理员开户、无自助注册」在产品里的
 *  唯一落地入口；此前只有 scripts/create_admin.py 一条命令行路径。
 *  独立成页而不挂进 MonitorPage：那个文件当前改动频繁，新功能单独放一处
 *  减少冲突面，代价是与总览/模型中心视觉上略有割裂，可接受。
 *
 *  页面分「成员」「团队」两栏，各自只有一张表；新建动作收进弹窗，
 *  免得三块常驻表单把整页顶得很长——真正频繁的操作是查、改、启停，不是新建。 */

const ROLES: WorkspaceRole[] = ["workspace_admin", "production", "review", "readonly"];

/** 一次待确认的破坏性操作：文案与真正要跑的动作绑在一起，避免两处写岔。 */
interface ConfirmRequest {
  title: string;
  summary: string;
  message: string;
  confirmLabel: string;
  danger?: boolean;
  run: () => void;
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

  const [tab, setTab] = useState<"members" | "teams">("members");
  const [teamDialogOpen, setTeamDialogOpen] = useState(false);
  const [userDialogOpen, setUserDialogOpen] = useState(false);
  // 存 id 而不是整行：每次动作后都会 load()，按 id 取才拿得到最新的团队列表。
  const [manageUserId, setManageUserId] = useState<string | null>(null);
  const [resetTarget, setResetTarget] = useState<UserRow | null>(null);
  // 原生 window.confirm 在移动端是系统弹窗，跟站内视觉完全两套，而且没法写清楚
  // 「停用会波及多少人」这种上下文。收敛成一个确认态，交给现成的 DecisionDialog。
  const [confirming, setConfirming] = useState<ConfirmRequest | null>(null);

  const load = async () => {
    setError(null);
    try {
      const [u, w] = await Promise.all([
        api.listUsers(),
        api.listWorkspaces(),
      ]);
      setUsers(u.items);
      setWorkspaces(w.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const notify = (text: string, isErr = false) => {
    setToast({ text, err: isErr });
    window.setTimeout(() => setToast(null), isErr ? 6000 : 2600);
  };

  /** 返回是否成功：弹窗要靠它决定「关掉」还是「留在原地让用户改」。 */
  const runAction = async (fn: () => Promise<unknown>, doneMsg: string) => {
    setBusy(true);
    try {
      await fn();
      notify(doneMsg);
      await load();
      return true;
    } catch (err) {
      notify(err instanceof ApiError ? err.message : String(err), true);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const createTeam = async (name: string) => {
    // 失败时不关弹窗：重名、后端拒绝都在这条路上，关掉就得让用户重打一遍。
    if (await runAction(() => api.createWorkspace(name), `团队「${name}」已创建`)) {
      setTeamDialogOpen(false);
    }
  };

  const createUser = async (draft: NewUserDraft) => {
    const ok = await runAction(
      () => api.createUser({
        username: draft.username,
        password: draft.password,
        display_name: draft.displayName || undefined,
        workspace_id: draft.workspaceId || undefined,
        role: draft.role,
      }),
      `账号「${draft.username}」已创建`,
    );
    if (ok) setUserDialogOpen(false);
  };

  const toggleTeamStatus = (w: WorkspaceRow) => {
    const next = w.status === "active" ? "disabled" : "active";
    const apply = () => void runAction(
      () => api.updateWorkspace(w.id, { status: next }),
      `团队「${w.name}」已${next === "active" ? "启用" : "停用"}`,
    );
    if (next === "active") { apply(); return; }
    setConfirming({
      title: `停用团队「${w.name}」`,
      summary: `${w.member_count} 名成员会立即失去这个团队下的访问权`,
      message: w.project_count > 0
        ? `该团队下有 ${w.project_count} 个项目。停用后成员打不开这些项目，系统管理员不受影响。随时可以再启用。`
        : "该团队下还没有项目。停用只影响成员的访问权，随时可以再启用。",
      confirmLabel: "停用团队",
      danger: true,
      run: apply,
    });
  };

  const changeRole = (workspaceId: string, userId: string, role: string) => {
    void runAction(
      () => api.updateWorkspaceMember(workspaceId, userId, { role }),
      "角色已更新",
    );
  };

  const removeMember = (workspaceId: string, userId: string, username: string, teamName: string) => {
    setConfirming({
      title: `把「${username}」移出「${teamName}」`,
      summary: "他会立即失去这个团队下所有项目的访问权",
      message: "账号本身不受影响，其它团队的成员关系也保留。之后可以再加回来。",
      confirmLabel: "移出团队",
      danger: true,
      run: () => void runAction(
        () => api.removeWorkspaceMember(workspaceId, userId),
        `已把「${username}」移出团队`,
      ),
    });
  };

  const addMember = (userId: string, username: string, workspaceId: string, role: string) => {
    void runAction(
      () => api.updateWorkspaceMember(workspaceId, userId, { role }),
      `已把「${username}」加入团队`,
    );
  };

  const toggleStatus = (u: UserRow) => {
    const next = u.status === "active" ? "disabled" : "active";
    const apply = () => void runAction(
      () => api.updateUser(u.id, { status: next }),
      `账号「${u.username}」已${next === "active" ? "启用" : "禁用"}`,
    );
    if (next === "active") { apply(); return; }
    setConfirming({
      title: `禁用账号「${u.username}」`,
      summary: "该账号当前的登录会立即失效",
      message: "他会被踢回登录页，且无法再登录，直到重新启用。已产生的数据不受影响。",
      confirmLabel: "禁用账号",
      danger: true,
      run: apply,
    });
  };

  const resetPassword = async (user: UserRow, password: string) => {
    const ok = await runAction(
      () => api.updateUser(user.id, { password }),
      `「${user.username}」的密码已重置`,
    );
    if (ok) setResetTarget(null);
  };

  const toggleSystemAdmin = (u: UserRow) => {
    const next = !u.is_system_admin;
    setConfirming({
      title: next ? `把「${u.username}」设为系统管理员` : `取消「${u.username}」的系统管理员身份`,
      summary: next
        ? "系统管理员不受团队边界限制，能看到并操作全部团队与项目"
        : "他将退回按团队角色授权，只能看到自己所属团队的项目",
      message: next
        ? "包括开户、启停账号、改全局模型策略。只给确实需要的人。"
        : "如果他是当前唯一的系统管理员，取消后将没有人能再进入这一页。",
      confirmLabel: next ? "设为系统管理员" : "取消管理员",
      danger: !next,
      run: () => void runAction(
        () => api.updateUser(u.id, { is_system_admin: next }),
        "已更新",
      ),
    });
  };

  const manageUser = manageUserId ? (users ?? []).find((u) => u.id === manageUserId) ?? null : null;
  const memberCount = users?.length ?? 0;
  const teamCount = workspaces?.length ?? 0;

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

      <div className="team-admin-bar">
        <div className="team-admin-tabs" role="tablist" aria-label="成员与团队">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "members"}
            className={tab === "members" ? "active" : ""}
            onClick={() => setTab("members")}
          >
            成员{users && <em>{memberCount}</em>}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "teams"}
            className={tab === "teams" ? "active" : ""}
            onClick={() => setTab("teams")}
          >
            团队{workspaces && <em>{teamCount}</em>}
          </button>
        </div>
        <div className="team-admin-bar-actions">
          <button type="button" className="btn" disabled={busy} onClick={() => setTeamDialogOpen(true)}>
            新建团队
          </button>
          <button type="button" className="btn primary" disabled={busy} onClick={() => setUserDialogOpen(true)}>
            创建账号
          </button>
        </div>
      </div>

      {tab === "teams" ? (
        <section className="card team-admin-section" role="tabpanel" aria-label="团队">
          <table className="team-admin-table">
            <thead><tr><th>团队</th><th>状态</th><th>成员数</th><th>项目数</th><th>操作</th></tr></thead>
            <tbody>
              {(workspaces ?? []).map((w) => (
                <tr key={w.id} className={w.status === "disabled" ? "team-admin-row-disabled" : undefined}>
                  <td>{w.name}</td>
                  <td>{w.status === "active" ? "启用中" : "已停用"}</td>
                  <td>{w.member_count}</td>
                  <td>{w.project_count}</td>
                  <td>
                    <button type="button" className={`btn small ${w.status === "active" ? "danger ghost" : ""}`}
                      disabled={busy} onClick={() => toggleTeamStatus(w)}>
                      {w.status === "active" ? "停用" : "启用"}
                    </button>
                  </td>
                </tr>
              ))}
              {!workspaces && !error && <tr><td colSpan={5} className="team-admin-muted">载入中…</td></tr>}
              {workspaces && !workspaces.length && (
                <tr><td colSpan={5} className="team-admin-muted">还没有团队，先「新建团队」。</td></tr>
              )}
            </tbody>
          </table>
        </section>
      ) : (
        <section className="card team-admin-section" role="tabpanel" aria-label="成员">
          <table className="team-admin-table team-admin-members">
            <thead>
              <tr>
                <th>账号</th><th>状态</th><th>团队与角色</th><th>最后登录</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {(users ?? []).map((u) => (
                <tr key={u.id} className={u.status === "disabled" ? "team-admin-row-disabled" : undefined}>
                  <td>
                    <div className="team-admin-identity">
                      <b>{u.username}</b>
                      <span>
                        {u.display_name || "—"}
                        {u.is_system_admin && <i className="team-admin-tag">系统管理员</i>}
                      </span>
                    </div>
                  </td>
                  <td>
                    <span className={`team-admin-state ${u.status}`}>
                      {u.status === "active" ? "启用中" : "已禁用"}
                    </span>
                  </td>
                  <td>
                    <div className="team-admin-teams">
                      {u.workspaces.length === 0
                        ? <span className="team-admin-muted">未加入任何团队</span>
                        : u.workspaces.map((m) => (
                            <span key={m.id} className="team-chip">
                              {m.name}<i>{roleLabel(m.role as WorkspaceRole)}</i>
                            </span>
                          ))}
                      <button type="button" className="text-action" disabled={busy}
                        onClick={() => setManageUserId(u.id)}>
                        管理
                      </button>
                    </div>
                  </td>
                  <td className="team-admin-time">{formatTime(u.last_login_at)}</td>
                  <td>
                    <div className="team-admin-actions">
                      <button type="button" className="btn small" disabled={busy} onClick={() => setResetTarget(u)}>重置密码</button>
                      <button type="button" className="btn small ghost" disabled={busy} onClick={() => toggleSystemAdmin(u)}>
                        {u.is_system_admin ? "取消管理员" : "设为管理员"}
                      </button>
                      <button type="button" className={`btn small ${u.status === "active" ? "danger ghost" : ""}`}
                        disabled={busy} onClick={() => toggleStatus(u)}>
                        {u.status === "active" ? "禁用" : "启用"}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {!users && !error && <tr><td colSpan={5} className="team-admin-muted">载入中…</td></tr>}
              {users && !users.length && (
                <tr><td colSpan={5} className="team-admin-muted">还没有账号，先「创建账号」。</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {teamDialogOpen && (
        <CreateTeamDialog busy={busy} onClose={() => setTeamDialogOpen(false)} onSubmit={createTeam} />
      )}
      {userDialogOpen && (
        <CreateUserDialog
          busy={busy}
          workspaces={workspaces ?? []}
          onClose={() => setUserDialogOpen(false)}
          onSubmit={createUser}
        />
      )}

      {manageUser && (
        <MemberTeamsDialog
          user={manageUser}
          workspaces={workspaces ?? []}
          busy={busy}
          suspended={Boolean(confirming)}
          onClose={() => setManageUserId(null)}
          onChangeRole={(workspaceId, role) => changeRole(workspaceId, manageUser.id, role)}
          onRemove={(workspaceId, teamName) => removeMember(workspaceId, manageUser.id, manageUser.username, teamName)}
          onAdd={(workspaceId, role) => addMember(manageUser.id, manageUser.username, workspaceId, role)}
        />
      )}

      {resetTarget && (
        <ResetPasswordDialog
          user={resetTarget}
          busy={busy}
          onClose={() => setResetTarget(null)}
          onSubmit={(password) => resetPassword(resetTarget, password)}
        />
      )}

      {confirming && (
        <DecisionDialog
          title={confirming.title}
          summary={confirming.summary}
          message={confirming.message}
          confirmLabel={confirming.confirmLabel}
          cancelLabel="取消"
          danger={confirming.danger}
          onConfirm={() => {
            const run = confirming.run;
            setConfirming(null);
            run();
          }}
          onClose={() => setConfirming(null)}
        />
      )}

      {toast && <div role="status" className={`toast ${toast.err ? "err" : ""}`}>{toast.text}</div>}
    </div>
  );
}

interface NewUserDraft {
  username: string;
  password: string;
  displayName: string;
  workspaceId: string;
  role: WorkspaceRole;
}

/** 新建团队：字段只有一个，但仍走弹窗——和「创建账号」同一条动线，
 *  页面上就只留两张表。 */
function CreateTeamDialog({ busy, onClose, onSubmit }: {
  busy: boolean;
  onClose: () => void;
  onSubmit: (name: string) => Promise<void>;
}) {
  const titleId = useId();
  const nameId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const trimmed = name.trim();
    if (!trimmed) {
      setError("团队名称不能为空");
      return;
    }
    void onSubmit(trimmed);
  };

  return (
    <div className="evidence-backdrop" role="presentation"
      onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <section ref={trapRef} className="impact-dialog decision-dialog" role="dialog"
        aria-modal="true" aria-labelledby={titleId}>
        <form onSubmit={submit}>
          <h3 id={titleId}>新建团队</h3>
          <div className="login-field">
            <label className="f" htmlFor={nameId}>团队名称</label>
            <input id={nameId} value={name} autoFocus disabled={busy} placeholder="例如：制作二组"
              onChange={(event) => { setName(event.target.value); setError(null); }} />
          </div>
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="dialog-actions">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? "创建中…" : "创建团队"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

/** 创建账号：初始密码由管理员定，用户首次登录会被强制改密
 *  （ForcePasswordChangePage），所以这里明文显示便于当面交接。 */
function CreateUserDialog({ busy, workspaces, onClose, onSubmit }: {
  busy: boolean;
  workspaces: WorkspaceRow[];
  onClose: () => void;
  onSubmit: (draft: NewUserDraft) => Promise<void>;
}) {
  const titleId = useId();
  const usernameId = useId();
  const displayNameId = useId();
  const passwordId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [draft, setDraft] = useState<NewUserDraft>({
    username: "",
    password: "",
    displayName: "",
    workspaceId: workspaces[0]?.id ?? "",
    role: "readonly",
  });
  const [error, setError] = useState<string | null>(null);
  const patch = (next: Partial<NewUserDraft>) => {
    setDraft((current) => ({ ...current, ...next }));
    setError(null);
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const username = draft.username.trim();
    if (!username) {
      setError("用户名不能为空");
      return;
    }
    if (draft.password.length < 8) {
      setError("初始密码至少 8 位");
      return;
    }
    void onSubmit({ ...draft, username, displayName: draft.displayName.trim() });
  };

  return (
    <div className="evidence-backdrop" role="presentation"
      onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <section ref={trapRef} className="impact-dialog decision-dialog" role="dialog"
        aria-modal="true" aria-labelledby={titleId}>
        <form onSubmit={submit}>
          <h3 id={titleId}>创建账号</h3>
          <div className="login-field">
            <label className="f" htmlFor={usernameId}>用户名</label>
            <input id={usernameId} value={draft.username} autoFocus disabled={busy}
              autoComplete="off" onChange={(event) => patch({ username: event.target.value })} />
          </div>
          <div className="login-field">
            <label className="f" htmlFor={displayNameId}>显示名（可选）</label>
            <input id={displayNameId} value={draft.displayName} disabled={busy}
              autoComplete="off" onChange={(event) => patch({ displayName: event.target.value })} />
          </div>
          <div className="login-field">
            <label className="f" htmlFor={passwordId}>初始密码（至少 8 位）</label>
            <input id={passwordId} type="text" value={draft.password} disabled={busy}
              autoComplete="off" onChange={(event) => patch({ password: event.target.value })} />
          </div>
          <div className="team-admin-dialog-row">
            <div className="login-field">
              <label className="f">加入团队</label>
              <select value={draft.workspaceId} disabled={busy}
                onChange={(event) => patch({ workspaceId: event.target.value })}>
                <option value="">（暂不加入，之后再分配）</option>
                {workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            </div>
            <div className="login-field">
              <label className="f">初始角色</label>
              <select value={draft.role} disabled={busy || !draft.workspaceId}
                onChange={(event) => patch({ role: event.target.value as WorkspaceRole })}>
                {ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
              </select>
            </div>
          </div>
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="dialog-actions">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? "创建中…" : "创建账号"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

/** 重置密码。原来是 window.prompt——移动端是系统弹窗，输入框还是明文单行，
 *  既没法校验位数，也说不清「对方会被踢下线」。 */
function ResetPasswordDialog({ user, busy, onClose, onSubmit }: {
  user: UserRow;
  busy: boolean;
  onClose: () => void;
  onSubmit: (password: string) => Promise<void>;
}) {
  const titleId = useId();
  const passwordId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    if (password.length < 8) {
      setError("密码至少 8 位");
      return;
    }
    void onSubmit(password);
  };

  return (
    <div className="evidence-backdrop" role="presentation"
      onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <section ref={trapRef} className="impact-dialog decision-dialog" role="dialog"
        aria-modal="true" aria-labelledby={titleId}>
        <form onSubmit={submit}>
          <h3 id={titleId}>重置「{user.username}」的密码</h3>
          <p className="team-admin-muted">
            改完他当前的登录会立即失效，需要用新密码重新登录。明文显示以便当面交接。
          </p>
          <div className="login-field">
            <label className="f" htmlFor={passwordId}>新密码（至少 8 位）</label>
            <input id={passwordId} type="text" value={password} autoFocus disabled={busy}
              autoComplete="off"
              onChange={(event) => { setPassword(event.target.value); setError(null); }} />
          </div>
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="dialog-actions">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
            <button type="submit" className="btn danger" disabled={busy}>
              {busy ? "重置中…" : "重置密码"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

/** 管理某个成员的团队与角色。原来这些下拉框直接铺在表格单元格里，
 *  一行塞两排控件，列宽又被别的列挤着——挪进弹窗后表格只剩只读徽章。 */
function MemberTeamsDialog({ user, workspaces, busy, suspended, onClose, onChangeRole, onRemove, onAdd }: {
  user: UserRow;
  workspaces: WorkspaceRow[];
  busy: boolean;
  /** 上面还压着确认弹窗时挂起：Esc 和 Tab 该归最上层那个管。 */
  suspended: boolean;
  onClose: () => void;
  onChangeRole: (workspaceId: string, role: string) => void;
  onRemove: (workspaceId: string, teamName: string) => void;
  onAdd: (workspaceId: string, role: string) => void;
}) {
  const titleId = useId();
  const trapRef = useFocusTrap(true, onClose, { suspended });
  const candidates = workspaces.filter((w) => !user.workspaces.some((m) => m.id === w.id));

  return (
    <div className="evidence-backdrop" role="presentation"
      onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <section ref={trapRef} className="impact-dialog decision-dialog" role="dialog"
        aria-modal="true" aria-labelledby={titleId}>
        <h3 id={titleId}>{user.username} 的团队与角色</h3>
        <p className="team-admin-muted">
          角色决定这个人在该团队下能做什么；移出后他会立即失去该团队所有项目的访问权。
        </p>

        <div className="member-team-list">
          {user.workspaces.length === 0 && (
            <p className="team-admin-muted">还没有加入任何团队。</p>
          )}
          {user.workspaces.map((m) => (
            <div key={m.id} className="member-team-row">
              <b>{m.name}</b>
              <select value={m.role} disabled={busy}
                onChange={(event) => onChangeRole(m.id, event.target.value)}>
                {ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
              </select>
              <button type="button" className="btn small ghost" disabled={busy}
                onClick={() => onRemove(m.id, m.name)}>移出</button>
            </div>
          ))}
        </div>

        {candidates.length > 0 && (
          <div className="member-team-add">
            <span className="f">加入新团队</span>
            <AddToTeamControl candidates={candidates} disabled={busy} onAdd={onAdd} />
          </div>
        )}

        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>完成</button>
        </div>
      </section>
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
    <div className="member-team-row">
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
