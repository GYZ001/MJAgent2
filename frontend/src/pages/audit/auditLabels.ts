import type { AuditOutcome, AuditSource } from "../../api";

/** 操作审计域的中文标签与时间预设——从 AuditFilters/AuditTable/AccountCard 三处
 *  复用，避免「成功/失败」这类文案在多个组件里各写一份、改一处漏一处。 */

const OUTCOME_LABELS: Record<AuditOutcome, string> = {
  ok: "成功",
  failed: "失败",
  rejected: "被拒",
  waiting_approval: "待确认",
  error: "异常",
};

export function outcomeLabel(outcome: string): string {
  return (OUTCOME_LABELS as Record<string, string>)[outcome] || outcome;
}

const SOURCE_LABELS: Record<AuditSource, string> = {
  ui: "页面",
  agent: "AI 代理",
  mcp: "MCP",
  system: "系统",
};

export function sourceLabel(source: string): string {
  return (SOURCE_LABELS as Record<string, string>)[source] || source;
}

export function identityLabel(isSystemAdmin: boolean | null | undefined): string {
  if (isSystemAdmin == null) return "—";
  return isSystemAdmin ? "系统管理员" : "普通用户";
}

export interface TimePreset {
  key: string;
  label: string;
  /** 距今天数；null 表示「全部」，不带 since 下限。 */
  days: number | null;
}

export const TIME_PRESETS: TimePreset[] = [
  { key: "7d", label: "近 7 天", days: 7 },
  { key: "30d", label: "近 30 天", days: 30 },
  { key: "90d", label: "近 90 天", days: 90 },
  { key: "180d", label: "近 180 天", days: 180 },
  { key: "all", label: "全部", days: null },
];

export const DEFAULT_TIME_PRESET = "7d";

/** 按预设 key 算出查询区间；未知 key 或「全部」都不带下限（后端按 365 天留存
 *  兜底，不由前端猜测起点）。 */
export function presetRange(key: string, nowSeconds: number = Date.now() / 1000): { since?: number } {
  const preset = TIME_PRESETS.find((item) => item.key === key);
  if (!preset || preset.days === null) return {};
  return { since: Math.floor(nowSeconds) - preset.days * 86400 };
}
