import { get, mutate } from "../client";

/** EP-01 组织/团队/角色/项目授权域：与 `app/orgs/api.py` 的响应形状逐字对应，
 *  字段名保持后端原样（下划线命名），不在这一层做驼峰转换——与本仓库其余
 *  api/*.ts 的既有约定一致。 */

export interface OrgInfo {
  id: string;
  tenant_id: string;
  name: string;
  status: "active" | "disabled";
  created_at: number;
  created_by: string | null;
}

export interface CurrentOrgTeamMembership {
  team_id: string;
  team_name: string;
  role_id: string | null;
  role_name: string | null;
}

export interface CurrentOrgInfo {
  org: OrgInfo | null;
  is_system_admin: boolean;
  is_org_admin: boolean;
  role_governed: boolean;
  teams: CurrentOrgTeamMembership[];
  permission_keys: string[];
}

export interface TeamMemberRow {
  user_id: string;
  role_id: string;
  created_at: number;
}

export interface TeamRow {
  id: string;
  org_id: string;
  name: string;
  description: string | null;
  status: "active" | "disabled";
  members: TeamMemberRow[];
}

/** 权限点目录条目：字段全部透出自后端 CommandSpec（title/risk/side_effect），
 *  供角色编辑器展示，不是前端自己维护的第二张名单。 */
export interface PermissionPoint {
  key: string;
  title: string;
  risk: string;
  scopes: string[];
  side_effect: string;
  admin_only: boolean;
  tags: string[];
}

export interface RoleRow {
  id: string;
  org_id: string | null;
  key: string;
  name: string;
  description: string | null;
  builtin: boolean;
  permission_keys: string[];
}

export interface ProjectGrantRow {
  project_id: string;
  subject_type: "user" | "team";
  subject_id: string;
  role_id: string;
  created_at: number;
  expires_at: number | null;
}

/** DELETE /roles/{id} 409 时的响应体（ApiError.detail）：把引用方列出来，
 *  不只是一个数字（CLAUDE.md「拦住用户时必须给出路」）。 */
export interface RoleReferenceConflict {
  message: string;
  team_members: { team_id: string; team_name: string; user_id: string }[];
  project_grants: { project_id: string; subject_type: string; subject_id: string }[];
}

export function getCurrentOrg(): Promise<CurrentOrgInfo> {
  return get("/orgs/current");
}

export function listTeams(): Promise<{ items: TeamRow[] }> {
  return get("/teams");
}

export function createTeam(body: { name: string; description?: string }): Promise<TeamRow> {
  return mutate("POST", "/teams", body);
}

export function updateTeam(
  teamId: string,
  body: { name?: string; description?: string; status?: "active" | "disabled" },
): Promise<TeamRow> {
  return mutate("PUT", `/teams/${teamId}`, body);
}

export function addTeamMembers(
  teamId: string,
  members: { user_id: string; role_id: string }[],
): Promise<TeamRow> {
  return mutate("POST", `/teams/${teamId}/members`, members);
}

export function removeTeamMember(teamId: string, userId: string): Promise<TeamRow> {
  return mutate("DELETE", `/teams/${teamId}/members/${userId}`);
}

export function listRoles(): Promise<{ items: RoleRow[]; permission_catalog: PermissionPoint[] }> {
  return get("/roles");
}

export function listPermissions(): Promise<{ items: PermissionPoint[] }> {
  return get("/permissions");
}

export function createRole(body: {
  key: string;
  name: string;
  description?: string;
  permission_keys?: string[];
  from_template?: string;
}): Promise<RoleRow> {
  return mutate("POST", "/roles", body);
}

export function updateRolePermissions(roleId: string, permissionKeys: string[]): Promise<RoleRow> {
  return mutate("PUT", `/roles/${roleId}`, { permission_keys: permissionKeys });
}

export function deleteRole(roleId: string): Promise<{ ok: true }> {
  return mutate("DELETE", `/roles/${roleId}`) as Promise<{ ok: true }>;
}

export function listProjectGrants(projectId: string): Promise<{ items: ProjectGrantRow[] }> {
  return get(`/projects/${projectId}/grants`);
}

export function createProjectGrant(
  projectId: string,
  body: { subject_type: "user" | "team"; subject_id: string; role_id: string; expires_at?: number },
): Promise<{ items: ProjectGrantRow[] }> {
  return mutate("POST", `/projects/${projectId}/grants`, body);
}

export function deleteProjectGrant(
  projectId: string,
  subjectType: "user" | "team",
  subjectId: string,
): Promise<{ ok: true }> {
  return mutate("DELETE", `/projects/${projectId}/grants/${subjectType}/${subjectId}`) as Promise<{ ok: true }>;
}
