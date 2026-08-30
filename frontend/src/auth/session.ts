/**
 * 账号会话纯逻辑层。
 *
 * 刻意不引入 DOM/React：这样才能在 `environment: 'node'` 的 vitest 下直接单测。
 * 账号即项目空间落地后，团队角色（workspace_admin/production/review/
 * readonly）与逐角色 scope 映射一并退场——一个账号对自己名下的项目天然拥有
 * 全部操作权限，唯一还有意义的区分只剩「系统管理员 / 普通账号」。这里只保留
 * 这一个判断；真正的授权闸门在后端（app/auth/principal.py），两边不同步只会
 * 造成 UI 误导，不会造成越权。
 */

export interface AuthUser {
  id: string;
  username: string;
  display_name: string;
}

/** `/api/auth/me`（或登录响应去掉 session_token 后）落地的纯状态。 */
export interface AuthState {
  user: AuthUser | null;
  isSystemAdmin: boolean;
}

/** 能否看到「系统设置」入口：只看 is_system_admin。 */
export function canSeeSystemSettings(state: AuthState): boolean {
  return state.isSystemAdmin;
}
