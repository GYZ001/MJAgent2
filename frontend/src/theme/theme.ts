/**
 * 主题（亮色 / 暗色 / 跟随时间）的纯逻辑层。
 *
 * 与 auth/session.ts 同样的约定：不碰 DOM、不引 React，这样才能在
 * `environment: 'node'` 的 vitest 下直接单测。挂 data-theme、起定时器、
 * 读写 localStorage 这些副作用都在 ThemeContext.tsx 里。
 *
 * 「跟随时间」按浏览器所在系统的本地时钟判定，不看 prefers-color-scheme：
 * 用户要的是「晚上别晃眼」，操作系统有没有开夜间模式与此无关。
 *
 * 注意：index.html 里有一段防白闪的引导脚本复制了这里的键名与时段常量，
 * 改动 THEME_STORAGE_KEY / NIGHT_START_HOUR / NIGHT_END_HOUR 必须同步改那段脚本
 * （theme.test.ts 会读 index.html 校验，改漏了测试会红）。
 */

/** 用户可选的三种模式；auto = 跟随本地时间。 */
export type ThemeMode = "light" | "dark" | "auto";

/** 真正落到 <html data-theme> 上的取值，只有两种。 */
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "manju:theme-mode";

/** 夜间时段 [22:00, 08:00)，跨零点。 */
export const NIGHT_START_HOUR = 22;
export const NIGHT_END_HOUR = 8;

/** 未选择过时的默认：跟随时间。用户的原始诉求就是「晚上太亮」。 */
export const DEFAULT_THEME_MODE: ThemeMode = "auto";

export const THEME_MODES: readonly ThemeMode[] = ["light", "dark", "auto"];

export const THEME_MODE_LABELS: Record<ThemeMode, string> = {
  light: "亮色",
  dark: "暗色",
  auto: "跟随时间",
};

/** 落地到 UI 上的补充说明，避免用户猜「跟随时间」到底跟随什么。 */
export const AUTO_MODE_HINT = `${String(NIGHT_START_HOUR).padStart(2, "0")}:00–${String(
  NIGHT_END_HOUR,
).padStart(2, "0")}:00 自动暗色`;

/** 落在夜间时段内（含 22:00，不含 08:00）。 */
export function isNightHour(date: Date): boolean {
  const hour = date.getHours();
  return hour >= NIGHT_START_HOUR || hour < NIGHT_END_HOUR;
}

export function resolveTheme(mode: ThemeMode, now: Date): ResolvedTheme {
  if (mode === "light" || mode === "dark") return mode;
  return isNightHour(now) ? "dark" : "light";
}

/** 把 localStorage 里的脏值（旧版本、手改、null）收敛回合法模式。 */
export function normalizeMode(raw: unknown): ThemeMode {
  return THEME_MODES.includes(raw as ThemeMode) ? (raw as ThemeMode) : DEFAULT_THEME_MODE;
}

/** 下一次昼夜切换的时刻：今天剩下的 08:00 / 22:00，都过了就是明天 08:00。 */
export function nextSwitchAt(now: Date): Date {
  for (const hour of [NIGHT_END_HOUR, NIGHT_START_HOUR]) {
    const at = new Date(now);
    at.setHours(hour, 0, 0, 0);
    if (at.getTime() > now.getTime()) return at;
  }
  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  tomorrow.setHours(NIGHT_END_HOUR, 0, 0, 0);
  return tomorrow;
}

/**
 * 距下一次切换的毫秒数。
 * 下限 1000ms：定时器早醒一点点（或系统时钟被回拨）时不至于空转成忙循环。
 */
export function msUntilNextSwitch(now: Date): number {
  return Math.max(1000, nextSwitchAt(now).getTime() - now.getTime());
}
