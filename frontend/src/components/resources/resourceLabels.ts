/** 资源看板共用的中文文案映射，从各 Section 抽出避免重复（三个文件都要用）。 */
export const RESOURCE_LABELS: Record<string, string> = {
  token: "文本 token",
  video_seconds: "视频时长（秒）",
  image: "定妆照/场景图",
  storage_bytes: "存储占用",
  projects: "项目数",
  concurrency: "并发",
  project_concurrency: "项目并发",
};

export const SCOPE_LABELS: Record<string, string> = {
  org: "组织", team: "团队", user: "个人", project: "项目",
};

export function formatResourceValue(resource: string, value: number): string {
  if (resource === "storage_bytes") return formatBytes(value);
  if (resource === "token" || resource === "image") return formatCompactNumber(value);
  return String(Math.round(value));
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

export function formatCompactNumber(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.round(value));
}

export function formatSampledAt(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "尚未采样";
  return `截至 ${new Date(epochSeconds * 1000).toLocaleString("zh-CN", { hour12: false })}`;
}
