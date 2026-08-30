import { get, mutate } from "../client";

export interface UserRow {
  id: string;
  username: string;
  display_name: string;
  status: "active" | "disabled";
  is_system_admin: boolean;
  must_change_password: boolean;
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
}) {
  return mutate("POST", "/system/users", body);
}

/** 启停账号 / 重置密码 / 设为系统管理员共用同一个端点，body 各自只带变更字段。 */
export function updateUser(
  userId: string,
  body: { status?: string; password?: string; is_system_admin?: boolean },
) {
  return mutate("PUT", `/system/users/${userId}`, body);
}
