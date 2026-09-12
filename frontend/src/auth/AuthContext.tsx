import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { exchangeSsoCode, logout as apiLogout, me as apiMe, onUnauthenticated } from "../api";
import { runSsoBootExchange } from "./ssoExchange";
import type { AuthUser } from "./session";

export type AuthStatus = "loading" | "authed" | "anonymous";

export interface AuthContextValue {
  status: AuthStatus;
  user: AuthUser | null;
  isSystemAdmin: boolean;
  /** 管理员开户时置位；为 true 时应用壳不挂载，先强制改密。 */
  mustChangePassword: boolean;
  /** 应用启动时检测到 `?sso_code=` 但交换失败时的可读文案；登录页展示，
   *  成功交换或走本地密码登录后应保持为 null。 */
  ssoLoginError: string | null;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** 应用外壳的登录态：挂载时探测 `GET /api/auth/me`，401 时判定匿名。
 *  同时订阅 api.ts 的「登录已失效」信号，任何请求在中途遇到 401 都能把整个
 *  应用切回登录页，而不必等下一次手动刷新。 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isSystemAdmin, setIsSystemAdmin] = useState(false);
  const [mustChangePassword, setMustChangePassword] = useState(false);
  const [ssoLoginError, setSsoLoginError] = useState<string | null>(null);

  const goAnonymous = useCallback(() => {
    setUser(null);
    setIsSystemAdmin(false);
    setMustChangePassword(false);
    setStatus("anonymous");
  }, []);

  const refresh = useCallback(async () => {
    try {
      const data = await apiMe();
      setUser(data.user);
      setIsSystemAdmin(data.is_system_admin);
      setMustChangePassword(Boolean(data.must_change_password));
      setStatus("authed");
    } catch {
      // 401（未登录/会话过期）与网络错误统一按匿名处理：都不该让应用卡在
      // loading 态——匿名时登录页本身就是可操作的重试入口。
      goAnonymous();
    }
  }, [goAnonymous]);

  useEffect(() => {
    // 启动时先看地址栏有没有 SSO 回跳留下的一次性交换码：有就先换会话令牌
    // （成功或失败都会立刻清掉地址栏，见 runSsoBootExchange），再走既有的
    // `GET /auth/me` 探测；没有就直接探测，行为与改造前一致。
    void (async () => {
      const boot = await runSsoBootExchange({
        search: window.location.search,
        pathname: window.location.pathname,
        hash: window.location.hash,
        replaceUrl: (url) => window.history.replaceState({}, "", url),
        exchange: exchangeSsoCode,
      });
      if (boot.attempted && !boot.ok) setSsoLoginError(boot.errorMessage ?? null);
      else if (boot.attempted) setSsoLoginError(null);
      await refresh();
    })();
  }, [refresh]);

  useEffect(() => {
    onUnauthenticated(goAnonymous);
    return () => onUnauthenticated(null);
  }, [goAnonymous]);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } finally {
      goAnonymous();
    }
  }, [goAnonymous]);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      isSystemAdmin,
      mustChangePassword,
      ssoLoginError,
      refresh,
      logout,
    }),
    [status, user, isSystemAdmin, mustChangePassword, ssoLoginError, refresh, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}
