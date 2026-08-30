import type { UserTier } from "../api";

/** 三档会员，顺序即页面下拉框顺序。系统管理员账号（is_system_admin=1）不受
 *  任何档位限制——tier 字段对他们只是历史遗留值，UI 侧要单独标出，不要跟着
 *  这张表走，见 AccountAdminPage 里 `is_system_admin` 的分支。 */
export const TIERS: UserTier[] = ["free", "pro", "max"];

export const TIER_LABELS: Record<UserTier, string> = {
  free: "Free",
  pro: "Pro",
  max: "Max",
};

/** 展示文案，与 app/quota.py::TIER_TABLE 的四类配额对齐；纯只读展示，
 *  改这里不影响后端实际限流数字。 */
export const TIER_HINTS: Record<UserTier, string> = {
  free: "1 个项目 · 每模块 1 并发 · 30 天 30 万 token · 30 天 5 分钟视频",
  pro: "3 个项目 · 每模块 3 并发 · 30 天 90 万 token · 30 天 15 分钟视频",
  max: "10 个项目 · 每模块 10 并发 · 30 天 300 万 token · 30 天 50 分钟视频",
};
