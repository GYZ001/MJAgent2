import { download, get, mutate, request } from "../client";

/** CSV 批量导入逐行结果的四种判定，EP-03 §4 明确要求不许合并成"成功/失败"
 *  两类——企业管理员反复导入同一份花名册是常态，"更新"和"跳过"被混报成
 *  "成功"会掩盖真实变更面。 */
export type ImportRowAction = "create" | "update" | "skip" | "error";

export interface ImportRowResult {
  line_no: number;
  username: string;
  action: ImportRowAction;
  reason: string | null;
  column: string | null;
  display_name: string;
  email: string;
  employee_no: string;
  team_id: string | null;
  team_name: string;
  role_id: string | null;
  role_name: string;
  tier: string | null;
  /** 仅 create 行、且仅在 apply 响应与 report CSV 首次下载时出现一次；
   *  预检阶段（preview）恒为 undefined，重复下载 report 后变成 "***"。 */
  initial_password?: string | null;
}

export interface ImportCounts {
  create: number;
  update: number;
  skip: number;
  error: number;
}

export interface ImportPreviewResult {
  batch_id: string;
  encoding: string;
  counts: ImportCounts;
  rows: ImportRowResult[];
}

export interface ImportApplyResult {
  batch_id: string;
  counts: ImportCounts;
  rows: ImportRowResult[];
  applied_at: number;
}

/** 第一步：上传 CSV，逐行判定，返回 batch_id + 报告，不写库。 */
export function importPreview(file: File): Promise<ImportPreviewResult> {
  const form = new FormData();
  form.append("file", file);
  return request("POST", "/system/users/import/preview", undefined, { form });
}

/** 第二步：只应用预检通过的行；随机初始口令只在这次响应里出现一次。 */
export function importApply(batchId: string): Promise<ImportApplyResult> {
  return mutate("POST", `/system/users/import/${batchId}/apply`);
}

/** 第三步：CSV 报告下载。同一批次的初始口令列只在第一次下载时可见。 */
export function importReportBlob(batchId: string): Promise<Blob> {
  return download(`/system/users/import/${batchId}/report`);
}

/** 离职前置检查：名下项目数/在途任务数/占用存储/团队授权（EP-03 §5）。 */
export interface UserOwnedProject {
  id: string;
  name: string;
  created_at: number;
}

export interface UserTeamMembership {
  team_id: string;
  team_name: string;
  role_id: string;
  role_name: string;
}

export interface UserAssets {
  user_id: string;
  owned_projects: UserOwnedProject[];
  owned_projects_count: number;
  in_flight_jobs_count: number;
  storage_bytes: number;
  team_memberships: UserTeamMembership[];
}

export function getUserAssets(userId: string): Promise<UserAssets> {
  return get(`/system/users/${userId}/assets`);
}

export interface HandoverResult {
  from_user_id: string;
  to_user_id: string | null;
  to_team_id: string | null;
  transferred_projects: string[];
  transferred_count: number;
  transferred_at: number;
}

/** 移交名下全部活跃项目给 to_user_id 或 to_team_id（二选一），连带迁移
 *  project_grants；移交后 deleteUser() 才可能放行。 */
export function handoverUser(
  userId: string,
  target: { toUserId: string } | { toTeamId: string },
): Promise<HandoverResult> {
  const body = "toUserId" in target ? { to_user_id: target.toUserId } : { to_team_id: target.toTeamId };
  return mutate("POST", `/system/users/${userId}/handover`, body);
}

/** DELETE /system/users/{id} 命中 409（仍有未处置资产）时，后端响应体的
 *  detail 形状——直接带上调用 handoverUser() 所需的全部参数。 */
export interface UnresolvedAssetsDetail {
  message: string;
  assets: UserAssets;
  handover_endpoint: string;
  handover_params: { to_user_id: string; to_team_id: string };
}

/* ── 邀请链接（EP-03 第二阶段，见 app/provisioning/invitations.py） ── */

export type InvitationStatus = "pending" | "accepted" | "revoked" | "expired";

export interface InvitationRow {
  id: string;
  status: InvitationStatus;
  username: string;
  display_name: string;
  email: string | null;
  org_name: string | null;
  team_id: string | null;
  team_name: string | null;
  role_id: string | null;
  role_name: string | null;
  expires_at: number;
  created_by: string | null;
  created_at: number;
  accepted_user_id: string | null;
  revoked_by: string | null;
}

/** 签发响应比 InvitationRow 多一个字段：明文 token，只在这次响应里出现一次
 *  ——前端必须当场把完整的 `/invite/{token}` 链接展示给管理员复制走。 */
export interface CreatedInvitation extends InvitationRow {
  token: string;
}

export function createInvitation(body: {
  username: string;
  display_name?: string;
  email?: string;
  team_id?: string;
  role_id?: string;
}): Promise<CreatedInvitation> {
  return mutate("POST", "/system/invitations", body);
}

export function listInvitations(): Promise<{ items: InvitationRow[] }> {
  return get("/system/invitations");
}

export function revokeInvitation(invitationId: string): Promise<InvitationRow> {
  return mutate("POST", `/system/invitations/${invitationId}/revoke`);
}
