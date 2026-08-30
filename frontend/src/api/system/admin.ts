import { get, mutate } from "../client";

/** 三档会员，见 app/quota.py::TIER_TABLE。is_system_admin=1 的账号不受此字段
 *  限制——tier 对系统管理员而言只是个未使用的历史值，界面必须把这点显式标出，
 *  否则「明明是 free 档为什么不限速」会反复被问。 */
export type UserTier = "free" | "pro" | "max";

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
}

export function listUsers(): Promise<{ items: UserRow[] }> {
  return get("/system/users");
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

/** 启停账号 / 重置密码 / 设为系统管理员 / 改档位 / 重置配额周期锚点共用同一个
 *  端点，body 各自只带变更字段（见 app/auth/admin_api.py::update_user）。 */
export function updateUser(
  userId: string,
  body: {
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
