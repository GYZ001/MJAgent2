import { useEffect, useState } from "react";
import { api, ApiError, type RoleRow, type TeamRow } from "../../api";
import TeamCard from "./TeamCard";

/** 账号管理——「团队」标签页（EP-01 第二阶段）：建团队 + 团队内成员/角色管理。
 *  写操作全部要求组织管理员，非管理员会拿到后端 403——本页不做额外隐藏，直接
 *  把错误亮出来（CLAUDE.md「拦住用户时必须给出路」，不是静默失败）。 */
export default function TeamsTab() {
  const [teams, setTeams] = useState<TeamRow[] | null>(null);
  const [roles, setRoles] = useState<RoleRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");

  const load = async () => {
    setError(null);
    try {
      const [teamsRes, rolesRes] = await Promise.all([api.listTeams(), api.listRoles()]);
      setTeams(teamsRes.items);
      setRoles(rolesRes.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => { void load(); }, []);

  const createTeam = async () => {
    if (!newName.trim()) { setError("团队名称不能为空"); return; }
    setBusy(true);
    setError(null);
    try {
      const team = await api.createTeam({ name: newName.trim(), description: newDescription.trim() || undefined });
      setTeams((prev) => [...(prev ?? []), team]);
      setNewName("");
      setNewDescription("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const updateTeamInList = (team: TeamRow) => {
    setTeams((prev) => (prev ?? []).map((t) => (t.id === team.id ? team : t)));
  };

  return (
    <>
      {error && (
        <div className="empty query-error" role="alert">
          <strong>操作失败</strong>
          <p>{error}</p>
        </div>
      )}

      <div className="card team-create-form">
        <h3>新建团队</h3>
        <div className="team-member-form">
          <input placeholder="团队名称" value={newName} disabled={busy} onChange={(e) => setNewName(e.target.value)} />
          <input placeholder="说明（可选）" value={newDescription} disabled={busy}
            onChange={(e) => setNewDescription(e.target.value)} />
          <button type="button" className="btn primary" disabled={busy} onClick={() => void createTeam()}>
            新建团队
          </button>
        </div>
      </div>

      {!teams && !error && <p className="account-admin-muted">载入中…</p>}
      {teams && !teams.length && <p className="account-admin-muted">还没有团队，先「新建团队」。</p>}
      <div className="account-admin-cards">
        {(teams ?? []).map((team) => (
          <TeamCard
            key={team.id} team={team} roles={roles} busy={busy}
            onChanged={updateTeamInList} onError={setError}
          />
        ))}
      </div>
    </>
  );
}
