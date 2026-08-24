import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { logout as apiLogout, me as apiMe, onUnauthenticated } from "../api";
import type { AuthUser, WorkspaceMembership } from "./session";

export type AuthStatus = "loading" | "authed" | "anonymous";

export interface AuthContextValue {
  status: AuthStatus;
  user: AuthUser | null;
  workspaces: WorkspaceMembership[];
  isSystemAdmin: boolean;
  /** 管理员开户时置位；为 true 时应用壳不挂载，先强制改密。 */
  mustChangePassword: boolean;
  /** 当前展示用的团队 id：多数用户只属于一个团队，取第一个即可；
   *  本阶段没有团队切换器，需要时再扩展。 */
  currentWorkspaceId: string | null;
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
  const [workspaces, setWorkspaces] = useState<WorkspaceMembership[]>([]);
  const [isSystemAdmin, setIsSystemAdmin] = useState(false);
  const [mustChangePassword, setMustChangePassword] = useState(false);

  const goAnonymous = useCallback(() => {
    setUser(null);
    setWorkspaces([]);
    setIsSystemAdmin(false);
    setMustChangePassword(false);
    setStatus("anonymous");
  }, []);

  const refresh = useCallback(async () => {
    try {
      const data = await apiMe();
      setUser(data.user);
      setWorkspaces(data.workspaces);
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
    void refresh();
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

  const currentWorkspaceId = workspaces[0]?.id ?? null;

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      workspaces,
      isSystemAdmin,
      mustChangePassword,
      currentWorkspaceId,
      refresh,
      logout,
    }),
    [status, user, workspaces, isSystemAdmin, mustChangePassword, currentWorkspaceId, refresh, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}
