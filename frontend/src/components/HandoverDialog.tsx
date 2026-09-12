import { useEffect, useId, useState } from "react";
import { api, ApiError, type UserAssets, type UserRow } from "../api";
import type { TeamRow } from "../api/system/orgs";
import { useFocusTrap } from "../hooks/useFocusTrap";

/** 离职资产查询 + 移交弹窗（EP-03 §5）。两个打开入口：
 *  1. 账号卡片上的「资产 / 移交」按钮，管理员主动查看/处置；
 *  2. 删除账号命中 409（仍有未处置资产）时自动弹出，`initialAssets` 直接用
 *     409 响应体里已经带的资产清单，不再多打一次 GET。
 *  界面文案与后端行为对齐：移交只搬"活跃项目"的归属与协作授权，不碰在途
 *  任务（继续跑）、不碰团队成员资格本身（CLAUDE.md「界面承诺必须与实际行为
 *  一致」）。 */

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(1)} ${units[i]}`;
}

export function HandoverDialog({
  user,
  initialAssets,
  onClose,
  onHandedOver,
}: {
  user: UserRow;
  initialAssets?: UserAssets;
  onClose: () => void;
  onHandedOver: () => void;
}) {
  const titleId = useId();
  const trapRef = useFocusTrap(true, onClose);

  const [assets, setAssets] = useState<UserAssets | null>(initialAssets ?? null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [teams, setTeams] = useState<TeamRow[]>([]);
  const [targetKind, setTargetKind] = useState<"user" | "team">("user");
  const [targetId, setTargetId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    if (!initialAssets) {
      api.getUserAssets(user.id).then(setAssets).catch((err) => {
        setLoadError(err instanceof ApiError ? err.message : String(err));
      });
    }
    api.listUsers().then((r) => setUsers(r.items.filter((u) => u.id !== user.id))).catch(() => undefined);
    api.listTeams().then((r) => setTeams(r.items)).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = async () => {
    if (!targetId) { setError("请选择接收账号或团队"); return; }
    setBusy(true);
    setError(null);
    try {
      const result = targetKind === "user"
        ? await api.handoverUser(user.id, { toUserId: targetId })
        : await api.handoverUser(user.id, { toTeamId: targetId });
      setDone(`已移交 ${result.transferred_count} 个项目`);
      onHandedOver();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="evidence-backdrop" role="presentation"
      onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <section ref={trapRef} className="impact-dialog decision-dialog" role="dialog"
        aria-modal="true" aria-labelledby={titleId}>
        <h3 id={titleId}>「{user.username}」的资产与移交</h3>

        {loadError && <p className="field-error" role="alert">{loadError}</p>}
        {!assets && !loadError && <p className="account-admin-muted">载入中…</p>}

        {assets && (
          <>
            <p className="account-admin-tier-hint">
              名下活跃项目 {assets.owned_projects_count} 个　·　在途任务 {assets.in_flight_jobs_count} 个（移交后
              继续跑，不受影响）　·　占用存储 {formatBytes(assets.storage_bytes)}　·　团队授权{" "}
              {assets.team_memberships.length} 条
            </p>
            {assets.owned_projects.length > 0 && (
              <ul className="import-dialog-table-wrap">
                {assets.owned_projects.map((p) => <li key={p.id}>{p.name}（{p.id}）</li>)}
              </ul>
            )}
            {assets.owned_projects_count === 0 && (
              <p className="account-admin-muted">名下已无活跃项目，可以直接删除账号，不需要移交。</p>
            )}
          </>
        )}

        {assets && assets.owned_projects_count > 0 && !done && (
          <>
            <div className="account-admin-checkbox-field">
              <label>
                <input type="radio" name="handover-kind" checked={targetKind === "user"}
                  onChange={() => { setTargetKind("user"); setTargetId(""); }} /> 移交给某个账号
              </label>
              <label>
                <input type="radio" name="handover-kind" checked={targetKind === "team"}
                  onChange={() => { setTargetKind("team"); setTargetId(""); }} /> 移交给某个团队
              </label>
            </div>
            <div className="login-field">
              <label className="f">{targetKind === "user" ? "接收账号" : "接收团队"}</label>
              <select value={targetId} disabled={busy} onChange={(event) => setTargetId(event.target.value)}>
                <option value="">请选择…</option>
                {targetKind === "user"
                  ? users.map((u) => <option key={u.id} value={u.id}>{u.display_name}（{u.username}）</option>)
                  : teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </div>
          </>
        )}

        {error && <p className="field-error" role="alert">{error}</p>}
        {done && <p className="account-admin-tier-hint">{done}</p>}

        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>关闭</button>
          {assets && assets.owned_projects_count > 0 && !done && (
            <button type="button" className="btn primary" disabled={busy || !targetId} onClick={() => void submit()}>
              {busy ? "移交中…" : "确认移交"}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
