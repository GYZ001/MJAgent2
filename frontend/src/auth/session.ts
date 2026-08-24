/**
 * RBAC 会话/角色纯逻辑层。
 *
 * 刻意不引入 DOM/React：这样才能在 `environment: 'node'` 的 vitest 下直接单测。
 * 角色 -> scope 的映射与中文文案是这里唯一的“业务知识”，其余（发请求、hooks）
 * 留给 api.ts / AuthContext.tsx。
 *
 * scope 常量与后端 `app/authz/resolve.py` 的 ROLE_SCOPES / ALL_SCOPES 逐字对应，
 * 不要各写各的——前端这份只用来决定“要不要显示某个入口”，真正的授权闸门在后端，
 * 两边不同步只会造成 UI 误导，不会造成越权。
 */

/** `workspace_members.role` 只有这 4 个 ASCII 枚举值（系统管理员不是某个角色）。 */
export type WorkspaceRole = "workspace_admin" | "production" | "review" | "readonly";

export type Scope =
  | "manju:read"
  | "manju:project-write"
  | "manju:generation-text"
  | "manju:generation-media"
  | "manju:delivery"
  | "manju:admin";

export interface AuthUser {
  id: string;
  username: string;
  display_name: string;
}

export interface WorkspaceMembership {
  id: string;
  name: string;
  role: WorkspaceRole | string;
}

/** `/api/auth/me`（或登录响应去掉 session_token 后）落地的纯状态。 */
export interface AuthState {
  user: AuthUser | null;
  workspaces: WorkspaceMembership[];
  isSystemAdmin: boolean;
}

/** 系统管理员隐式拥有全部 scope，与 `Principal.is_system_admin` 一致。 */
export const ALL_SCOPES: readonly Scope[] = [
  "manju:read",
  "manju:project-write",
  "manju:generation-text",
  "manju:generation-media",
  "manju:delivery",
  "manju:admin",
];

/** 与后端 `ROLE_SCOPES` 逐字对应。 */
export const ROLE_SCOPES: Record<WorkspaceRole, readonly Scope[]> = {
  workspace_admin: [
    "manju:read",
    "manju:project-write",
    "manju:generation-text",
    "manju:generation-media",
    "manju:delivery",
  ],
  production: [
    "manju:read",
    "manju:project-write",
    "manju:generation-text",
    "manju:generation-media",
  ],
  review: ["manju:read", "manju:delivery"],
  readonly: ["manju:read"],
};

const ROLE_LABELS: Record<WorkspaceRole, string> = {
  workspace_admin: "空间管理员",
  production: "制作",
  review: "审校",
  readonly: "只读",
};

/** 角色的中文展示名；未知角色（理论上不该出现）原样返回，不崩页面。 */
export function roleLabel(role: string): string {
  return ROLE_LABELS[role as WorkspaceRole] ?? role;
}

/** 某个团队（workspace）下当前用户拥有的 scope 集合；不在该团队则为空集。 */
export function scopesFor(state: AuthState, workspaceId: string): ReadonlySet<Scope> {
  if (state.isSystemAdmin) return new Set(ALL_SCOPES);
  const membership = state.workspaces.find((item) => item.id === workspaceId);
  if (!membership) return new Set();
  return new Set(ROLE_SCOPES[membership.role as WorkspaceRole] ?? []);
}

/** 是否系统管理员——顶层布尔位，不是某个团队里的角色。 */
export function isSystemAdmin(state: AuthState): boolean {
  return state.isSystemAdmin;
}

/** 能否看到「系统设置」入口：只看 is_system_admin，与团队内角色无关。 */
export function canSeeSystemSettings(state: AuthState): boolean {
  return state.isSystemAdmin;
}
