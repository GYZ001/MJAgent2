import { useState } from "react";
import { api, ApiError, type RoleRow, type TeamRow } from "../../api";

/** 单个团队卡片：改名/停用、成员增删。拆出 TeamsTab.tsx 之外，是因为一张卡片
 *  自己就要管一份「正在编辑的成员表单」局部状态，塞进列表页会让父组件同时
 *  追踪 N 份表单状态，按团队拆分后每张卡片只关心自己。 */
export default function TeamCard({
  team, roles, busy, onChanged, onError,
}: {
  team: TeamRow;
  roles: RoleRow[];
  busy: boolean;
  onChanged: (team: TeamRow) => void;
  onError: (message: string) => void;
}) {
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(team.name);
  const [memberUserId, setMemberUserId] = useState("");
  const [memberRoleId, setMemberRoleId] = useState("");
  const [localBusy, setLocalBusy] = useState(false);

  const disabled = busy || localBusy;
  const roleName = (id: string) => roles.find((r) => r.id === id)?.name ?? id;

  const run = async (fn: () => Promise<TeamRow>) => {
    setLocalBusy(true);
    try {
      onChanged(await fn());
    } catch (err) {
      onError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLocalBusy(false);
    }
  };

  const saveName = () => {
    setEditingName(false);
    if (nameDraft.trim() && nameDraft !== team.name) {
      void run(() => api.updateTeam(team.id, { name: nameDraft.trim() }));
    } else {
      setNameDraft(team.name);
    }
  };

  const toggleStatus = () => {
    const next = team.status === "active" ? "disabled" : "active";
    void run(() => api.updateTeam(team.id, { status: next }));
  };

  const addMember = () => {
    if (!memberUserId.trim() || !memberRoleId) {
      onError("请填写账号 ID 并选择角色");
      return;
    }
    void run(() => api.addTeamMembers(team.id, [{ user_id: memberUserId.trim(), role_id: memberRoleId }]));
    setMemberUserId("");
  };

  const removeMember = (userId: string) => {
    void run(() => api.removeTeamMember(team.id, userId));
  };

  return (
    <div className="card team-card">
      <div className="team-card-head">
        {editingName ? (
          <input
            autoFocus value={nameDraft} disabled={disabled}
            onChange={(e) => setNameDraft(e.target.value)}
            onBlur={saveName}
            onKeyDown={(e) => { if (e.key === "Enter") saveName(); }}
          />
        ) : (
          <h3 onClick={() => setEditingName(true)} title="点击改名">{team.name}</h3>
        )}
        <span className={`stamp ${team.status === "active" ? "green" : "grey"}`}>
          {team.status === "active" ? "启用中" : "已停用"}
        </span>
        <button type="button" className="btn small ghost" disabled={disabled} onClick={toggleStatus}>
          {team.status === "active" ? "停用" : "启用"}
        </button>
      </div>
      {team.description && <p className="sub">{team.description}</p>}

      <ul className="team-member-list">
        {team.members.map((m) => (
          <li key={m.user_id}>
            <b>{m.user_id}</b>
            <span>{roleName(m.role_id)}</span>
            <button type="button" className="btn small ghost danger" disabled={disabled}
              onClick={() => removeMember(m.user_id)}>移除</button>
          </li>
        ))}
        {!team.members.length && <li className="account-admin-muted">还没有成员</li>}
      </ul>

      <div className="team-member-form">
        <input placeholder="账号 ID" value={memberUserId} disabled={disabled}
          onChange={(e) => setMemberUserId(e.target.value)} />
        <select aria-label="选择角色" value={memberRoleId} disabled={disabled}
          onChange={(e) => setMemberRoleId(e.target.value)}>
          <option value="">选择角色…</option>
          {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
        <button type="button" className="btn small primary" disabled={disabled} onClick={addMember}>加入</button>
      </div>
    </div>
  );
}
