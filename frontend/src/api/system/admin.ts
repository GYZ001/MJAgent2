import { get, mutate } from "../client";

export interface WorkspaceMembershipRow {
  id: string;
  name: string;
  role: string;
}

export interface UserRow {
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

export interface WorkspaceRow {
  id: string;
  name: string;
  status: "active" | "disabled";
  member_count: number;
  project_count: number;
}

export function listUsers(): Promise<{ items: UserRow[] }> {
  return get("/system/users");
}

export function listWorkspaces(): Promise<{ items: WorkspaceRow[] }> {
  return get("/system/workspaces");
}

export function createWorkspace(name: string) {
  return mutate("POST", "/system/workspaces", { name });
}

export function createUser(body: {
  username: string;
  password: string;
  display_name?: string;
  workspace_id?: string;
  role?: string;
}) {
  return mutate("POST", "/system/users", body);
}

/** 团队启停走同一个端点，body 只带 { status }。 */
export function updateWorkspace(workspaceId: string, body: { status: string }) {
  return mutate("PUT", `/system/workspaces/${workspaceId}`, body);
}

/** 加入团队 / 改角色共用同一个端点（成员不存在则创建，存在则改角色）。 */
export function updateWorkspaceMember(
  workspaceId: string,
  userId: string,
  body: { role: string },
) {
  return mutate("PUT", `/system/workspaces/${workspaceId}/members/${userId}`, body);
}

export function removeWorkspaceMember(workspaceId: string, userId: string) {
  return mutate("DELETE", `/system/workspaces/${workspaceId}/members/${userId}`);
}

/** 启停账号 / 重置密码 / 设为系统管理员共用同一个端点，body 各自只带变更字段。 */
export function updateUser(
  userId: string,
  body: { status?: string; password?: string; is_system_admin?: boolean },
) {
  return mutate("PUT", `/system/users/${userId}`, body);
}
