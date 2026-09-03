import { useId, useState } from "react";
import type { DeletedUserRow, UserRow, UserTier } from "../api";
import { ADDON_HINT, TIERS, TIER_HINTS, TIER_LABELS } from "../lib/tier";
import { outcomeLabel } from "../pages/audit/auditLabels";

/** 账号管理页的两种卡片。从 AccountAdminPage.tsx 抽出：该页当时 398/400 行，
 *  已经贴着前端单文件 400 行上限，再往里加东西就必然撞线（CLAUDE.md
 *  「装不下时先想怎么拆，不要先想加基线」）。 */


export function formatTime(epochSeconds: number | null): string {
  if (!epochSeconds) return "从未";
  return new Date(epochSeconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

function formatRetentionDays(seconds: number): string {
  const clamped = Math.max(0, seconds);
  const days = Math.floor(clamped / 86400);
  const hours = Math.floor((clamped % 86400) / 3600);
  if (days > 0) return `约 ${days} 天后彻底清理`;
  if (hours > 0) return `约 ${hours} 小时后彻底清理`;
  return "即将彻底清理";
}

type Act = (u: UserRow) => void;
interface AccountCardProps {
  user: UserRow; isSelf: boolean; busy: boolean;
  onSaveDisplayName: (u: UserRow, name: string) => void;
  onChangeTier: (u: UserRow, tier: UserTier) => void;
  onToggleAdmin: Act; onResetPassword: Act; onResetQuota: Act; onToggleStatus: Act; onSoftDelete: Act;
  onSelfDeleteOpen: () => void;
  onGrantAddon: (u: UserRow, packages: number) => void;
}

export function AccountCard(props: AccountCardProps) {
  const {
    user, isSelf, busy, onSaveDisplayName, onChangeTier, onToggleAdmin,
    onResetPassword, onResetQuota, onToggleStatus, onSoftDelete, onSelfDeleteOpen, onGrantAddon,
  } = props;
  const nameId = useId();
  const [name, setName] = useState(user.display_name);
  const [addonOpen, setAddonOpen] = useState(false);
  const [packages, setPackages] = useState(1);
  const dirty = name.trim().length > 0 && name.trim() !== user.display_name;

  return (
    <article className={`account-card ${user.status === "disabled" ? "account-card-disabled" : ""}`}>
      <div className="account-card-identity">
        <b>{user.username}</b>
        {isSelf && <span className="account-admin-tag account-admin-tag-self">你</span>}
        {user.is_system_admin && <span className="account-admin-tag">系统管理员</span>}
        {user.status === "disabled" && <span className="account-admin-tag account-admin-tag-off">已禁用</span>}
      </div>
      <div className="account-card-field">
        <label htmlFor={nameId}>显示名</label>
        <div className="account-card-field-row">
          <input id={nameId} value={name} disabled={busy} onChange={(event) => setName(event.target.value)} />
          <button type="button" className="btn small" disabled={busy || !dirty}
            onClick={() => onSaveDisplayName(user, name.trim())}>保存</button>
        </div>
      </div>
      <div className="account-card-field">
        <span className="account-card-field-label">档位</span>
        {user.is_system_admin ? (
          <span className="account-admin-tier-unlimited" title="系统管理员不受档位限制">不限（管理员）</span>
        ) : (
          <select value={user.tier} disabled={busy} title={TIER_HINTS[user.tier]}
            onChange={(event) => onChangeTier(user, event.target.value as UserTier)}>
            {TIERS.map((t) => <option key={t} value={t}>{TIER_LABELS[t]}</option>)}
          </select>
        )}
      </div>
      <p className="account-card-meta">创建于 {formatTime(user.created_at)} · 最近登录 {formatTime(user.last_login_at)} · 最近活跃 {formatTime(user.last_active_at)}</p>
      <p className="account-card-meta account-card-last-action">
        最近操作：{user.last_action
          ? `${user.last_action.event_label || user.last_action.event}（${outcomeLabel(user.last_action.outcome)}）· ${formatTime(user.last_action.ts)}`
          : "暂无记录"}
        <button type="button" className="btn small ghost" onClick={() => {
          const target = `/system/audit?user_id=${encodeURIComponent(user.id)}`;
          window.history.pushState({}, "", target);
          window.dispatchEvent(new PopStateEvent("popstate"));
        }}>查看操作记录</button>
      </p>
      <div className="account-card-actions">
        <button type="button" className="btn small" disabled={busy} onClick={() => onResetPassword(user)}>重置密码</button>
        <button type="button" className="btn small ghost" disabled={busy} onClick={() => onToggleAdmin(user)}>
          {user.is_system_admin ? "取消管理员" : "设为管理员"}
        </button>
        <button type="button" className="btn small ghost" disabled={busy} onClick={() => onResetQuota(user)}>重置配额周期</button>
        <button type="button" className={`btn small ${user.status === "active" ? "danger ghost" : ""}`}
          disabled={busy} onClick={() => onToggleStatus(user)}>{user.status === "active" ? "禁用" : "启用"}</button>
        <button type="button" className="btn small ghost" disabled={busy} onClick={() => setAddonOpen((v) => !v)}>加量包</button>
        {!isSelf && (
          <button type="button" className="btn small danger ghost" disabled={busy}
            onClick={() => onSoftDelete(user)}>删除（移入回收站）</button>
        )}
      </div>
      {addonOpen && (
        <div className="account-card-addon">
          <span className="account-card-addon-hint">{ADDON_HINT}</span>
          <div className="account-card-field-row">
            <input type="number" min={1} value={packages} disabled={busy}
              onChange={(event) => setPackages(Math.max(1, Math.floor(Number(event.target.value) || 1)))} />
            <button type="button" className="btn small primary" disabled={busy}
              onClick={() => { onGrantAddon(user, packages); setAddonOpen(false); setPackages(1); }}>发放</button>
          </div>
        </div>
      )}
      {isSelf && (
        <div className="account-card-danger-zone">
          <p>危险区：彻底删除你自己的账号会立即级联清空全部项目，不可恢复，也没有回收站兜底。</p>
          <button type="button" className="btn danger" disabled={busy} onClick={onSelfDeleteOpen}>彻底删除我的账号</button>
        </div>
      )}
    </article>
  );
}

export function DeletedAccountCard(
  { user, busy, onRestore }: { user: DeletedUserRow; busy: boolean; onRestore: (u: DeletedUserRow) => void },
) {
  return (
    <article className="account-card account-card-deleted">
      <div className="account-card-identity">
        <b>{user.username}</b>
        <span>{user.display_name || "—"}</span>
      </div>
      <p className="account-card-meta">
        删除于 {formatTime(user.deleted_at)} · {formatRetentionDays(user.retention_seconds_remaining)}
      </p>
      <div className="account-card-actions">
        <button type="button" className="btn small primary" disabled={busy} onClick={() => onRestore(user)}>恢复</button>
      </div>
    </article>
  );
}
