import { get, mutate } from "../client";

/** 五档会员，见 app/quota.py::TIER_TABLE。is_system_admin=1 的账号不受此字段
 *  限制——tier 对系统管理员而言只是个未使用的历史值，界面必须把这点显式标出，
 *  否则「明明是 free 档为什么不限速」会反复被问。 */
export type UserTier = "free" | "starter" | "standard" | "pro" | "max";

export interface UserRow {
  id: string;
  username: string;
  display_name: string;
  status: "active" | "disabled";
  is_system_admin: boolean;
  must_change_password: boolean;
  tier: UserTier;
  /** 当前 30 天配额周期的锚点时间（epoch 秒）；后端始终会填，理论上不为 null。 */
  quota_period_started_at: number | null;
  created_at: number;
  last_login_at: number | null;
  /** 软删除时间戳；活跃账号列表（GET /system/users）里恒为 null，只有回收站
   *  列表（GET /system/users/deleted）会真正带值。 */
  deleted_at: number | null;
}

/** 回收站条目：活跃账号的全部字段 + 到期清理时间与剩余保留秒数。 */
export interface DeletedUserRow extends UserRow {
  purge_at: number;
  retention_seconds_remaining: number;
}

/** 账号软删/恢复级联影响的项目清单；单项失败不阻塞其余，`failed` 必须显示
 *  给管理员看，不能吞掉（CLAUDE.md「失败列表不能吞掉」）。 */
export interface AccountCascadeOutcome {
  soft_deleted?: string[];
  soft_deleted_count?: number;
  restored?: string[];
  restored_count?: number;
  failed: { project_id: string; error_id: string; error: string }[];
}

export interface AdminDeleteUserResult {
  ok: boolean;
  deleted_user_id: string;
  deleted_at: number;
  purge_at: number;
  projects: AccountCascadeOutcome;
}

export interface AdminRestoreUserResult {
  ok: boolean;
  restored_user_id: string;
  projects: AccountCascadeOutcome;
}

export interface GrantVideoAddonResult {
  user_id: string;
  packages: number;
  package_seconds: number;
  price_cny: number;
  attempt_key: string;
  seconds_granted: number;
  idempotent_replay: boolean;
  addon_balance_s: number;
}

export function listUsers(): Promise<{ items: UserRow[] }> {
  return get("/system/users");
}

/** 回收站：已软删除、还在 30 天保留期内（或还没被后台巡检彻底清理）的账号。 */
export function listDeletedUsers(): Promise<{ items: DeletedUserRow[] }> {
  return get("/system/users/deleted");
}

export function createUser(body: {
  username: string;
  password: string;
  display_name?: string;
  is_system_admin?: boolean;
  tier?: UserTier;
  must_change_password?: boolean;
}) {
  return mutate("POST", "/system/users", body);
}

/** 启停账号 / 重置密码 / 设为系统管理员 / 改档位 / 改显示名 / 重置配额周期锚点
 *  共用同一个端点，body 各自只带变更字段（见 app/auth/admin_api.py::update_user）。 */
export function updateUser(
  userId: string,
  body: {
    display_name?: string;
    status?: string;
    password?: string;
    must_change_password?: boolean;
    is_system_admin?: boolean;
    tier?: UserTier;
    reset_quota_period?: boolean;
  },
) {
  return mutate("PUT", `/system/users/${userId}`, body);
}

/** 管理员软删账号：30 天保留期，期间可 restoreUser() 恢复；名下当前活跃项目
 *  一并移入回收站。不可对最后一个系统管理员执行（后端 422 拦截）。 */
export function deleteUser(userId: string): Promise<AdminDeleteUserResult> {
  return mutate("DELETE", `/system/users/${userId}`) as Promise<AdminDeleteUserResult>;
}

/** 30 天保留期内恢复被软删除的账号，级联恢复其被这次删除带出的项目。 */
export function restoreUser(userId: string): Promise<AdminRestoreUserResult> {
  return mutate("POST", `/system/users/${userId}/restore`) as Promise<AdminRestoreUserResult>;
}

/** 管理员手工发放视频加量包：每包 10 分钟、¥199，不随 30 天配额周期重置。
 *  `idempotencyKey` 缺省时后端会生成一次性 key——重复调用会重复发放，重放
 *  保护要靠调用方自己传稳定的 key。 */
export function grantVideoAddon(
  userId: string,
  packages: number,
  idempotencyKey?: string,
): Promise<GrantVideoAddonResult> {
  return mutate("POST", `/system/users/${userId}/video-addons`, {
    packages,
    idempotency_key: idempotencyKey,
  }) as Promise<GrantVideoAddonResult>;
}
