import type { UserTier } from "../api";

/** 五档会员，顺序即页面下拉框顺序，与 app/quota.py::TIER_TABLE 一一对应。
 *  系统管理员账号（is_system_admin=1）不受任何档位限制——tier 字段对他们只是
 *  历史遗留值，UI 侧要单独标出，不要跟着这张表走，见 AccountAdminPage 里
 *  `is_system_admin` 的分支。 */
export const TIERS: UserTier[] = ["free", "starter", "standard", "pro", "max"];

/** 中文档名取自后端 app/quota.py::_UPGRADE_PATH 的既有措辞（“升级到入门/标准/
 *  专业/旗舰档位”），不是前端另起的一套名字。 */
export const TIER_LABELS: Record<UserTier, string> = {
  free: "Free",
  starter: "入门",
  standard: "标准",
  pro: "专业",
  max: "旗舰",
};

/** 展示文案，与 app/quota.py::TIER_TABLE 的四类配额对齐；纯只读展示，
 *  改这里不影响后端实际限流数字——判据仍在后端。 */
export const TIER_HINTS: Record<UserTier, string> = {
  free: "1 个项目 · 每模块 1 并发 · 30 天 30 万 token · 30 天 1 分钟视频 · 300 万图像",
  starter: "2 个项目 · 每模块 2 并发 · 30 天 60 万 token · 30 天 5 分钟视频 · 600 万图像",
  standard: "3 个项目 · 每模块 3 并发 · 30 天 90 万 token · 30 天 15 分钟视频 · 900 万图像",
  pro: "6 个项目 · 每模块 6 并发 · 30 天 180 万 token · 30 天 30 分钟视频 · 1800 万图像",
  max: "10 个项目 · 每模块 10 并发 · 30 天 300 万 token · 30 天 50 分钟视频 · 3000 万图像",
};

/** 加量包展示文案，与 app/quota_addon.py 的常量对齐（¥199/包，10 分钟/包，
 *  不随 30 天周期重置）。纯展示；实际计费以后端返回的 price_cny 为准，本页
 *  从不自己算钱。 */
export const ADDON_HINT = "¥199 / 包 · 每包 10 分钟视频时长 · 不随 30 天周期重置";
