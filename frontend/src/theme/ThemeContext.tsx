import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  DEFAULT_THEME_MODE,
  THEME_STORAGE_KEY,
  msUntilNextSwitch,
  normalizeMode,
  resolveTheme,
  type ResolvedTheme,
  type ThemeMode,
} from "./theme";

export interface ThemeContextValue {
  /** 用户选的模式（可能是 auto）。 */
  mode: ThemeMode;
  /** 实际生效的皮肤，UI 用它显示「当前暗色」之类的提示。 */
  resolved: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function readStoredMode(): ThemeMode {
  try {
    return normalizeMode(window.localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    // 隐私模式 / 禁用 storage 的浏览器：退回默认，不要让应用起不来。
    return DEFAULT_THEME_MODE;
  }
}

/** 真正改变外观的唯一出口：全站样式都挂在 <html data-theme> 上。 */
function applyTheme(resolved: ResolvedTheme) {
  const root = document.documentElement;
  root.dataset.theme = resolved;
  // 让浏览器原生控件（滚动条、表单默认外观、autofill 底色）跟着一起换。
  root.style.colorScheme = resolved;
}

/**
 * 主题状态：读盘 -> 解析 -> 挂到 <html>，并在昼夜边界自动翻面。
 *
 * 首屏的 data-theme 由 index.html 的引导脚本先写好（防白闪），这里挂载后
 * 再算一次并接管；两边算法一致，所以不会出现闪一下又跳回去。
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(readStoredMode);
  const [resolved, setResolved] = useState<ResolvedTheme>(() =>
    resolveTheme(readStoredMode(), new Date()),
  );

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;

    const sync = () => {
      const now = new Date();
      setResolved(resolveTheme(mode, now));
      if (mode !== "auto") return;
      // 只在下一个边界（08:00 / 22:00）醒一次，不做秒级轮询。
      timer = setTimeout(sync, msUntilNextSwitch(now));
    };

    // 笔记本合盖休眠时 setTimeout 不保证按时触发，回到前台补算一次。
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (timer) clearTimeout(timer);
      sync();
    };

    sync();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [mode]);

  useEffect(() => {
    applyTheme(resolved);
  }, [resolved]);

  // 多标签页：在一个标签改了主题，其它标签跟着变，不用逐个刷新。
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== THEME_STORAGE_KEY) return;
      setModeState(normalizeMode(event.newValue));
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // 存不下就只在本次会话生效，不影响当前页面的切换。
    }
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({ mode, resolved, setMode }),
    [mode, resolved, setMode],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme 必须在 ThemeProvider 内使用");
  return ctx;
}
