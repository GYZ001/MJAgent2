import {
  lazy,
  Suspense,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type RefObject,
} from "react";
import { api, ApiError, changePassword, Episode, Project } from "./api";
import Studio from "./pages/Studio";
import LoginPage from "./pages/LoginPage";
import ForcePasswordChangePage from "./pages/ForcePasswordChangePage";
import DecisionDialog from "./components/DecisionDialog";
import ErrorBoundary from "./components/ErrorBoundary";
import EpisodeCrumb from "./components/EpisodeCrumb";
import SearchField from "./components/SearchField";
import { useFocusTrap } from "./hooks/useFocusTrap";
import { useScrollContainment } from "./useScrollContainment";
import { AdaptivePoller, type PollInterval } from "./adaptivePoller";
import { pickerWindowParams, resolveWindowedEpisodeId } from "./episodePicker";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { canSeeSystemSettings } from "./auth/session";
import ThemeSwitch from "./theme/ThemeSwitch";
import {
  loadAccountAdminPage,
  loadBiblePage,
  loadBoardPage,
  loadCinemaPage,
  loadEpisodesPage,
  loadMonitorPage,
  loadReaderPage,
  loadScenesPage,
  loadScriptPage,
  loadSeriesPage,
  loadWallPage,
  PAGE_LOADERS,
  SECTIONS,
  SYSTEM_SECTIONS,
  type View,
} from "./appSections";
export type { View } from "./appSections";

const BiblePage = lazy(loadBiblePage);
const ScenesPage = lazy(loadScenesPage);
const EpisodesPage = lazy(loadEpisodesPage);
const ScriptPage = lazy(loadScriptPage);
const BoardPage = lazy(loadBoardPage);
const WallPage = lazy(loadWallPage);
const CinemaPage = lazy(loadCinemaPage);
const SeriesPage = lazy(loadSeriesPage);
const MonitorPage = lazy(loadMonitorPage);
const ReaderPage = lazy(loadReaderPage);
const AccountAdminPage = lazy(loadAccountAdminPage);

/** 项目清单拉取失败后的重试退避区间。 */
const PROJECTS_RETRY_MIN_MS = 2000;
const PROJECTS_RETRY_MAX_MS = 30000;

const prefetchedViews = new Set<View>();

/** 鼠标悬停/聚焦即开始拉该页 chunk，把等待挪到点击之前。
 *
 * dev 服务器还要为首次请求现场编译（大页 0.3~1.6s），预取同样能把这笔开销提前。
 * 失败时撤销标记，留给点击后的 lazy() 正常重试。
 */
export function prefetchView(view: View): void {
  const load = PAGE_LOADERS[view];
  if (!load || prefetchedViews.has(view)) return;
  prefetchedViews.add(view);
  void load().catch(() => {
    prefetchedViews.delete(view);
  });
}

export interface NavigationGuardPrompt {
  title: string;
  summary: string;
  message: string;
  details?: string[];
  confirmLabel: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm?: () => void;
  onCancel?: () => void;
}

interface Nav {
  view: View;
  projectId: string | null;
  episodeId: string | null;
  chapterIdx: number | null;
  go: (
    v: View,
    projectId?: string | null,
    episodeId?: string | null,
    chapterIdx?: number | null,
    historyAction?: "push" | "replace",
  ) => void;
  requestNavigation: (target: string, commit: () => void) => void;
  toast: (msg: string, isErr?: boolean) => void;
  registerNavigationGuard: (
    guard: NavigationGuardPrompt | null,
    unsaved?: boolean,
  ) => void;
}

interface PendingNavigation {
  view: View;
  projectId: string | null;
  episodeId: string | null;
  chapterIdx: number | null;
  target: string;
  historyAction: "push" | "replace";
  prompt: NavigationGuardPrompt;
  commit?: () => void;
}

const NavCtx = createContext<Nav | null>(null);

export function useNav(): Nav {
  const context = useContext(NavCtx);
  const fallback = useMemo<Nav>(() => {
    const route = readLocation();
    return {
      ...route,
      go: (view, projectId, episodeId, chapterIdx) => {
        window.location.assign(locationFor(
          view,
          projectId === undefined ? route.projectId : projectId,
          episodeId === undefined ? route.episodeId : episodeId,
          chapterIdx === undefined ? route.chapterIdx : chapterIdx,
        ));
      },
      requestNavigation: (_target, commit) => commit(),
      toast: () => undefined,
      registerNavigationGuard: () => undefined,
    };
  }, []);
  return context ?? fallback;
}

const decodePart = (value?: string) =>
  value ? decodeURIComponent(value) : null;

export function routeFromPath(pathname: string): Pick<
  Nav,
  "view" | "projectId" | "episodeId" | "chapterIdx"
> {
  const parts = pathname.split("/").filter(Boolean);
  if (parts[0] === "system")
    return { view: "system", projectId: null, episodeId: null, chapterIdx: null };
  if (parts[0] === "workspaces")
    return { view: "studio", projectId: null, episodeId: null, chapterIdx: null };
  if (parts[0] !== "projects" || !parts[1]) {
    return {
      view: "studio",
      projectId: null,
      episodeId: null,
      chapterIdx: null,
    };
  }
  const projectId = decodePart(parts[1]);
  if (parts[2] === "observability")
    return { view: "observability", projectId, episodeId: null, chapterIdx: null };
  if (parts[2] === "series")
    return { view: "series", projectId, episodeId: decodePart(parts[3]), chapterIdx: null };
  if (parts[2] === "reader") {
    const idx = Number(parts[3]);
    return {
      view: "reader",
      projectId,
      episodeId: null,
      chapterIdx: Number.isFinite(idx) ? idx : 1,
    };
  }
  if (parts[2] === "episodes" && parts[3]) {
    const episodeId = decodePart(parts[3]);
    const page = parts[4];
    const view: View =
      page === "board" || page === "wall" || page === "cinema"
        ? page
        : "script";
    return { view, projectId, episodeId, chapterIdx: null };
  }
  if (
    parts[2] === "script" ||
    parts[2] === "board" ||
    parts[2] === "wall" ||
    parts[2] === "cinema"
  ) {
    return {
      view: parts[2],
      projectId,
      episodeId: null,
      chapterIdx: null,
    };
  }
  const view: View =
    parts[2] === "scenes" || parts[2] === "episodes" || parts[2] === "bible"
      ? parts[2]
      : "bible";
  return { view, projectId, episodeId: null, chapterIdx: null };
}

function readLocation() {
  return routeFromPath(window.location.pathname);
}

/** go() 的分集槽位解析：``undefined`` = 沿用当前选中的集（映射台/分镜台之间来回
 *  切换要记住分集），``null``/字符串 = 显式指定。连播台是唯一的例外：它的这个槽位
 *  承载的是**任务 id**（/projects/{pid}/series/{taskId}），只能来自列表→详情的显式
 *  导航；从别的工作台带过来的粘性分集 id 对它没有意义——2026-09-02 实测，从选中了
 *  某一集的分镜台点侧栏进连播台，那个集 id 被编进 URL、当成任务 id 去查详情，
 *  用户看到的是「资源不存在或不属于当前账号」。 */
export function resolveNavEpisodeId(
  view: View,
  requested: string | null | undefined,
  current: string | null,
): string | null {
  if (view === "studio" || view === "system") return null;
  if (requested === undefined) return view === "series" ? null : current;
  return requested;
}

export function locationFor(
  view: View,
  projectId: string | null,
  episodeId: string | null,
  chapterIdx: number | null,
) {
  if (view === "studio") return "/workspaces";
  if (view === "system") return "/system/overview";
  if (!projectId) return "/workspaces";
  const project = `/projects/${encodeURIComponent(projectId)}`;
  if (view === "observability") return `${project}/observability/jobs`;
  if (view === "series")
    return episodeId ? `${project}/series/${encodeURIComponent(episodeId)}` : `${project}/series`;
  if (view === "reader") return `${project}/reader/${chapterIdx ?? 1}`;
  if (
    view === "script" ||
    view === "board" ||
    view === "wall" ||
    view === "cinema"
  ) {
    return episodeId
      ? `${project}/episodes/${encodeURIComponent(episodeId)}/${view}`
      : `${project}/${view}`;
  }
  return `${project}/${view}`;
}

/** 应用真正的默认导出：先过登录闸门，未登录/校验中都不挂载下面的工作台外壳。 */
export default function App() {
  return (
    <AuthProvider>
      <AuthGate />
    </AuthProvider>
  );
}

function AuthGate() {
  const { status, mustChangePassword } = useAuth();
  if (status === "loading") {
    return (
      <div className="auth-loading">
        <div className="empty" role="status">正在校验登录状态…</div>
      </div>
    );
  }
  if (status === "anonymous") return <LoginPage />;
  // 管理员开户设的初始密码经过管理员之手，不换掉审计署名从第一天就不可信。
  // 置位期间整个工作台外壳都不挂载，只留改密与登出两条路。
  if (mustChangePassword) return <ForcePasswordChangePage />;
  return <AppShell />;
}

function AppShell() {
  const auth = useAuth();
  const isSystemAdminUser = canSeeSystemSettings(auth);
  const initial = readLocation();
  const initialWasRootRef = useRef(window.location.pathname === "/");
  const [view, setView] = useState<View>(initial.view);
  const [projectId, setProjectId] = useState<string | null>(initial.projectId);
  const [episodeId, setEpisodeId] = useState<string | null>(initial.episodeId);
  const [chapterIdx, setChapterIdx] = useState<number | null>(
    initial.chapterIdx,
  );
  const [toastMsg, setToastMsg] = useState<{
    text: string;
    err: boolean;
  } | null>(null);
  const [spineCollapsed, setSpineCollapsed] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [projectSwitcherOpen, setProjectSwitcherOpen] = useState(false);
  const [projectSearch, setProjectSearch] = useState("");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [routeRevision, setRouteRevision] = useState(0);
  const [pendingNavigation, setPendingNavigation] =
    useState<PendingNavigation | null>(null);
  const navigationGuardRef = useRef<NavigationGuardPrompt | null>(null);
  const lastLocationRef = useRef(
    `${window.location.pathname}${window.location.search}`,
  );
  const toastTimerRef = useRef<number>();
  const projectsRetryTimerRef = useRef<number>();
  const projectsRetryDelayRef = useRef(PROJECTS_RETRY_MIN_MS);
  const spineRef = useRef<HTMLElement | null>(null);
  const workspaceSwitcherRef = useRef<HTMLDivElement | null>(null);
  const closeMobileNav = useCallback(() => setMobileNavOpen(false), []);
  const mobileNavTrapRef = useFocusTrap(mobileNavOpen, closeMobileNav);
  const bindSpineRef = useCallback(
    (node: HTMLElement | null) => {
      spineRef.current = node;
      mobileNavTrapRef.current = node;
    },
    [mobileNavTrapRef],
  );
  useScrollContainment(spineRef, true);
  useEffect(() => {
    if (window.location.pathname === "/") {
      window.history.replaceState({}, "", "/workspaces");
      lastLocationRef.current = "/workspaces";
    }
  }, []);
  useEffect(() => {
    if (!projectSwitcherOpen) return;
    const closeOutside = (event: MouseEvent) => {
      if (!workspaceSwitcherRef.current?.contains(event.target as Node)) {
        setProjectSwitcherOpen(false);
        setProjectSearch("");
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setProjectSwitcherOpen(false);
        setProjectSearch("");
      }
    };
    document.addEventListener("mousedown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [projectSwitcherOpen]);

  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [changePasswordOpen, setChangePasswordOpen] = useState(false);
  const userMenuRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!userMenuOpen) return;
    const closeOutside = (event: MouseEvent) => {
      if (!userMenuRef.current?.contains(event.target as Node)) {
        setUserMenuOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setUserMenuOpen(false);
    };
    document.addEventListener("mousedown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [userMenuOpen]);

  const refreshProjects = useCallback(() => {
    if (projectsRetryTimerRef.current) {
      window.clearTimeout(projectsRetryTimerRef.current);
      projectsRetryTimerRef.current = undefined;
    }
    void api.listProjects().then((items: Project[]) => {
      setProjects(items);
      setProjectsLoaded(true);
      projectsRetryDelayRef.current = PROJECTS_RETRY_MIN_MS;
    }).catch(() => {
      setProjectsLoaded(true);
      // 后端长时间不可达时，固定 2s 重试会一直空转打链路。改成退避到 30s 封顶，
      // 成功后立刻复位，恢复瞬间仍然跟得上。
      const delay = projectsRetryDelayRef.current;
      projectsRetryDelayRef.current = Math.min(delay * 2, PROJECTS_RETRY_MAX_MS);
      projectsRetryTimerRef.current = window.setTimeout(refreshProjects, delay);
    });
  }, []);
  useEffect(() => {
    refreshProjects();
    window.addEventListener("manju:projects-changed", refreshProjects);
    return () => {
      window.removeEventListener("manju:projects-changed", refreshProjects);
      if (projectsRetryTimerRef.current) {
        window.clearTimeout(projectsRetryTimerRef.current);
        projectsRetryTimerRef.current = undefined;
      }
    };
  }, [refreshProjects]);
  useEffect(() => {
    if (!projectsLoaded || !initialWasRootRef.current) return;
    initialWasRootRef.current = false;
    const lastProjectId = window.localStorage.getItem("manju:last-project-id");
    if (!lastProjectId || !projects.some((project) => project.id === lastProjectId)) return;
    const saved = window.localStorage.getItem("manju:last-project-route") || "";
    const parsed = routeFromPath(saved.split("?")[0] || "/");
    const target = parsed.projectId === lastProjectId
      ? saved
      : `/projects/${encodeURIComponent(lastProjectId)}/bible`;
    window.history.replaceState({}, "", target);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, [projects, projectsLoaded]);
  useEffect(() => {
    if (projectId) window.localStorage.setItem("manju:last-project-id", projectId);
  }, [projectId]);
  useEffect(() => {
    setProjectSwitcherOpen(false);
    setProjectSearch("");
  }, [view]);

  useEffect(() => {
    const mobile = window.matchMedia("(max-width: 720px)");
    const closeOnDesktop = (event: MediaQueryListEvent) => {
      if (!event.matches) closeMobileNav();
    };
    mobile.addEventListener("change", closeOnDesktop);
    return () => mobile.removeEventListener("change", closeOnDesktop);
  }, [closeMobileNav]);

  useEffect(() => {
    const syncLocation = () => {
      lastLocationRef.current = `${window.location.pathname}${window.location.search}`;
      if (window.location.pathname.startsWith("/projects/")) {
        window.localStorage.setItem("manju:last-project-route", lastLocationRef.current);
      }
      setRouteRevision((revision) => revision + 1);
    };
    window.addEventListener("manju:locationchange", syncLocation);
    return () => window.removeEventListener("manju:locationchange", syncLocation);
  }, []);

  const toast = useCallback((text: string, isErr = false) => {
    setToastMsg({ text, err: isErr });
    if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(
      () => setToastMsg(null),
      isErr ? 8000 : 3000,
    );
  }, []);

  const registerNavigationGuard = useCallback(
    (guard: NavigationGuardPrompt | null) => {
      navigationGuardRef.current = guard;
    },
    [],
  );

  const go = useCallback(
    (
      v: View,
      pid?: string | null,
      eid?: string | null,
      cidx?: number | null,
      historyAction: "push" | "replace" = "push",
    ) => {
      const globalView = v === "studio" || v === "system";
      const nextProjectId = globalView
        ? null
        : pid === undefined
          ? projectId
          : pid;
      const nextEpisodeId = resolveNavEpisodeId(v, eid, episodeId);
      const nextChapterIdx = globalView
        ? null
        : cidx === undefined
          ? chapterIdx
          : cidx;
      const target = locationFor(
        v,
        nextProjectId,
        nextEpisodeId,
        nextChapterIdx,
      );
      const currentLocation = `${window.location.pathname}${window.location.search}`;
      if (currentLocation !== target && navigationGuardRef.current) {
        setPendingNavigation({
          view: v,
          projectId: nextProjectId,
          episodeId: nextEpisodeId,
          chapterIdx: nextChapterIdx,
          target,
          historyAction,
          prompt: navigationGuardRef.current,
        });
        setMobileNavOpen(false);
        return;
      }
      if (currentLocation !== target) {
        window.history[
          historyAction === "replace" ? "replaceState" : "pushState"
        ]({}, "", target);
        lastLocationRef.current = target;
        if (target.startsWith("/projects/")) {
          window.localStorage.setItem("manju:last-project-route", target);
        }
      }
      setProjectId(nextProjectId);
      setEpisodeId(nextEpisodeId);
      setChapterIdx(nextChapterIdx);
      setView(v);
      setMobileNavOpen(false);
      window.scrollTo({ top: 0, behavior: "auto" });
    },
    [chapterIdx, episodeId, projectId],
  );

  // 前端隐藏只是体验层面；真正的边界在后端（403/404）。这里只兜底手输
  // /system/overview 之类的地址——非系统管理员一律不该停在系统设置页上。
  useEffect(() => {
    if (view === "system" && !isSystemAdminUser) {
      go("studio", null, null, null, "replace");
    }
  }, [view, isSystemAdminUser, go]);

  const requestNavigation = useCallback(
    (target: string, commit: () => void) => {
      const currentLocation = `${window.location.pathname}${window.location.search}`;
      if (currentLocation !== target && navigationGuardRef.current) {
        setPendingNavigation({
          view,
          projectId,
          episodeId,
          chapterIdx,
          target,
          historyAction: "push",
          prompt: navigationGuardRef.current,
          commit,
        });
        setMobileNavOpen(false);
        return;
      }
      commit();
    },
    [chapterIdx, episodeId, projectId, view],
  );

  const cancelPendingNavigation = () => {
    pendingNavigation?.prompt.onCancel?.();
    setPendingNavigation(null);
  };

  const confirmPendingNavigation = () => {
    if (!pendingNavigation) return;
    const next = pendingNavigation;
    next.prompt.onConfirm?.();
    navigationGuardRef.current = null;
    if (next.commit) {
      setPendingNavigation(null);
      next.commit();
      return;
    }
    window.history[next.historyAction === "replace" ? "replaceState" : "pushState"](
      {},
      "",
      next.target,
    );
    lastLocationRef.current = next.target;
    if (next.target.startsWith("/projects/")) {
      window.localStorage.setItem("manju:last-project-route", next.target);
    }
    if (next.historyAction === "replace") {
      window.dispatchEvent(new PopStateEvent("popstate"));
    }
    setView(next.view);
    setProjectId(next.projectId);
    setEpisodeId(next.episodeId);
    setChapterIdx(next.chapterIdx);
    setPendingNavigation(null);
    setMobileNavOpen(false);
    window.scrollTo({ top: 0, behavior: "auto" });
  };

  useEffect(() => {
    const onPopState = () => {
      const attemptedTarget = `${window.location.pathname}${window.location.search}`;
      const next = readLocation();
      if (navigationGuardRef.current) {
        window.history.pushState({}, "", lastLocationRef.current);
        setPendingNavigation({
          ...next,
          target: attemptedTarget,
          historyAction: "replace",
          prompt: navigationGuardRef.current,
        });
        setMobileNavOpen(false);
        return;
      }
      lastLocationRef.current = attemptedTarget;
      if (window.location.pathname.startsWith("/projects/")) {
        window.localStorage.setItem("manju:last-project-route", attemptedTarget);
      }
      setRouteRevision((revision) => revision + 1);
      setView(next.view);
      setProjectId(next.projectId);
      setEpisodeId(next.episodeId);
      setChapterIdx(next.chapterIdx);
      setMobileNavOpen(false);
      window.scrollTo({ top: 0, behavior: "auto" });
    };
    window.addEventListener("popstate", onPopState, { capture: true });
    return () => window.removeEventListener("popstate", onPopState, { capture: true });
  }, [chapterIdx, episodeId, projectId, view]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [view, projectId, episodeId, chapterIdx]);

  useEffect(() => {
    if (!projectId) {
      setEpisodeId(null);
      return;
    }
    const location = readLocation();
    const requestedEpisodeId =
      location.projectId === projectId ? location.episodeId : null;
    let cancelled = false;
    // 这里只要解析出一个有效分集 id，不需要整份清单：用窗口模式取 1 条即可，
    // 千集项目下 payload 从 250KB 降到不足 1KB。
    api
      .getProject(projectId, `view=picker&${pickerWindowParams(1, requestedEpisodeId ?? episodeId)}`)
      .then((project: Project) => {
        if (cancelled) return;
        setEpisodeId((current) =>
          resolveWindowedEpisodeId(project, current, requestedEpisodeId),
        );
      })
      .catch(() => {
        // 临时请求失败不能等同于“项目没有分集”。保留当前选择，侧栏进入工作台时会重试。
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const nav: Nav = {
    view,
    projectId,
    episodeId,
    chapterIdx,
    go,
    requestNavigation,
    toast,
    registerNavigationGuard,
  };
  const visibleSections = projectId ? SECTIONS : [];

  const openSection = (s: (typeof SECTIONS)[number]) => {
    if (!s.needEpisode || !projectId) {
      go(s.key);
      return;
    }

    // 立刻切页，不等分集校验：先用已知分集进场，校验结果回来后再纠正选中项。
    const openedWith = episodeId;
    const openedTarget = locationFor(s.key, projectId, openedWith, chapterIdx);
    go(s.key, projectId, openedWith);

    // 分集可能在项目打开后才生成，仍要重新校验，只是既不阻塞导航、也不拉整份清单。
    api
      .getProject(projectId, `view=picker&${pickerWindowParams(1, openedWith)}`)
      .then((project: Project) => {
        const nextEpisodeId = resolveWindowedEpisodeId(project, openedWith);
        if (!nextEpisodeId || nextEpisodeId === openedWith) return;
        // 用户可能已经切走或被离开守卫拦下；只在仍停在刚打开的地址时纠正。
        const current = `${window.location.pathname}${window.location.search}`;
        if (current !== openedTarget) return;
        // replace：纠正是补齐信息而非一次新导航，不该在回退栈里多压一格。
        go(s.key, projectId, nextEpisodeId, undefined, "replace");
      })
      .catch(() => {
        toast(
          "分集列表加载失败，已打开工作台入口；可点击顶部的分集切换器重试",
          true,
        );
      });
  };

  const groupedSections = visibleSections.reduce<
    Record<string, typeof visibleSections>
  >((groups, section) => {
    (groups[section.group] ??= []).push(section);
    return groups;
  }, {});
  const currentProject = projects.find((project) => project.id === projectId);
  const currentPathname = window.location.pathname;
  const filteredProjects = projects.filter((project) =>
    project.name.toLowerCase().includes(projectSearch.trim().toLowerCase()),
  );
  const switchProject = (nextProjectId: string) => {
    setProjectSwitcherOpen(false);
    setProjectSearch("");
    if (nextProjectId === projectId) return;
    const intent = new URLSearchParams(window.location.search);
    if (view === "studio" && intent.get("intent") === "observability") {
      const requestedTab = intent.get("tab") || "jobs";
      const tab = ["jobs", "calls"].includes(requestedTab) ? requestedTab : "jobs";
      const target = `/projects/${encodeURIComponent(nextProjectId)}/observability/${tab}`;
      requestNavigation(target, () => {
        window.history.pushState({}, "", target);
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
      return;
    }
    if (view === "observability") {
      const currentTab = window.location.pathname.split("/").filter(Boolean).at(-1);
      const tab = ["jobs", "calls"].includes(currentTab || "") ? currentTab : "jobs";
      const target = `/projects/${encodeURIComponent(nextProjectId)}/observability/${tab}`;
      requestNavigation(target, () => {
        window.history.pushState({}, "", target);
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
      return;
    }
    const targetView: View = view === "studio" || view === "system"
      ? "bible"
      : view;
    go(targetView, nextProjectId, null, null);
  };

  return (
    <NavCtx.Provider value={nav}>
      <button
        className="mobile-nav-trigger"
        type="button"
        aria-label="打开主菜单"
        aria-expanded={mobileNavOpen}
        aria-controls="main-navigation"
        onClick={() => setMobileNavOpen(true)}
      >
        ☰
      </button>
      {mobileNavOpen && (
        <button
          className="mobile-nav-backdrop"
          type="button"
          aria-label="点击空白处关闭导航"
          onClick={closeMobileNav}
        />
      )}
      <aside
        id="main-navigation"
        ref={bindSpineRef}
        className={`spine ${spineCollapsed ? "collapsed" : ""} ${mobileNavOpen ? "mobile-open" : ""}`}
        role={mobileNavOpen ? "dialog" : undefined}
        aria-modal={mobileNavOpen || undefined}
        aria-label="主导航"
      >
        <div className="spine-top">
          <button
            className="seal"
            type="button"
            aria-label={spineCollapsed ? "展开侧栏" : "收起侧栏"}
            title={spineCollapsed ? "展开侧栏" : "收起侧栏"}
            aria-expanded={!spineCollapsed}
            onClick={() => setSpineCollapsed((v) => !v)}
          >
            漫
          </button>
          <div className="brand-copy">
            <b>漫剧案头</b>
            <span>智能制作</span>
          </div>
          <button
            className="spine-close"
            type="button"
            aria-label="关闭导航"
            onClick={closeMobileNav}
          >
            ×
          </button>
        </div>
        {view !== "system" && (
          <div className="workspace-switcher" ref={workspaceSwitcherRef}>
            <button
              type="button"
              className="workspace-switcher-trigger"
              aria-expanded={projectSwitcherOpen}
              onClick={() => setProjectSwitcherOpen((open) => !open)}
            >
              <span className="workspace-avatar" aria-hidden="true">书</span>
              <span><b>{currentProject?.name || "选择项目空间"}</b><small>{currentProject ? "小说创作项目" : "上传小说以创建空间"}</small></span>
              <i aria-hidden="true">{projectSwitcherOpen ? "⌃" : "⌄"}</i>
            </button>
            {projectSwitcherOpen && (
              <div className="workspace-switcher-popover">
                <SearchField
                  value={projectSearch}
                  onChange={setProjectSearch}
                  placeholder="搜索项目空间"
                  ariaLabel="搜索项目空间"
                />
                <div className="workspace-switcher-list">
                  {filteredProjects.map((project) => (
                    <button
                      type="button"
                      key={project.id}
                      className={`workspace-switcher-option ${project.id === projectId ? "active" : ""}`}
                      onClick={() => switchProject(project.id)}
                    >
                      <span className="workspace-avatar" aria-hidden="true">书</span>
                      <span><b>{project.name}</b><small>项目空间</small></span>
                      {project.id === projectId && <i aria-label="当前项目">✓</i>}
                    </button>
                  ))}
                  {!filteredProjects.length && <p>没有匹配的项目空间</p>}
                </div>
                <button type="button" className="workspace-create" onClick={() => {
                  setProjectSwitcherOpen(false);
                  const target = "/workspaces/new";
                  requestNavigation(target, () => {
                    window.history.pushState({}, "", target);
                    window.dispatchEvent(new PopStateEvent("popstate"));
                  });
                }}>
                  <span className="workspace-create-icon" aria-hidden="true">＋</span>
                  <span>创建 / 管理项目空间</span>
                  <i aria-hidden="true">→</i>
                </button>
              </div>
            )}
          </div>
        )}
        {view === "system" && isSystemAdminUser ? (
          <nav aria-label="系统设置">
            <div className="spine-group">
              <div className="spine-group-label">系统设置</div>
              {SYSTEM_SECTIONS.map((section) => {
                const active = currentPathname.endsWith(`/${section.key}`);
                return (
                  <button
                    key={section.key}
                    type="button"
                    className={`spine-item ${active ? "active" : ""}`}
                    aria-current={active ? "page" : undefined}
                    onClick={() => {
                      const target = `/system/${section.key}`;
                      requestNavigation(target, () => {
                        window.history.pushState({}, "", target);
                        window.dispatchEvent(new PopStateEvent("popstate"));
                      });
                    }}
                  >
                    <span className="spine-icon" aria-hidden="true">{section.icon}</span>
                    <span className="spine-label">{section.label}</span>
                  </button>
                );
              })}
            </div>
          </nav>
        ) : (
        <nav aria-label="项目空间工作台">
          {Object.entries(groupedSections).map(([group, sections]) => (
            <div className="spine-group" key={group}>
              <div className="spine-group-label">{group}</div>
              {sections.map((s) => {
                const active = s.matchViews
                  ? s.matchViews.includes(view)
                  : view === s.key;
                return (
                  <button
                    key={s.key}
                    type="button"
                    className={`spine-item ${active ? "active" : ""}`}
                    aria-label={s.label}
                    aria-current={active ? "page" : undefined}
                    onMouseEnter={() => prefetchView(s.key)}
                    onFocus={() => prefetchView(s.key)}
                    onTouchStart={() => prefetchView(s.key)}
                    onClick={() => {
                      openSection(s);
                    }}
                    title={s.label}
                  >
                    <span className="spine-icon" aria-hidden="true">
                      {s.icon}
                    </span>
                    <span className="spine-label">{s.label}</span>
                  </button>
                );
              })}
            </div>
          ))}
        </nav>
        )}
        <div className="spine-foot">
          {view === "system" && isSystemAdminUser ? (
            <button className="spine-foot-action" type="button" aria-label="返回项目空间"
              onMouseEnter={() => prefetchView("bible")}
              onFocus={() => prefetchView("bible")}
              onClick={() => {
              const last = window.localStorage.getItem("manju:last-project-id");
              const saved = window.localStorage.getItem("manju:last-project-route") || "";
              if (last && projects.some((item) => item.id === last)) {
                const parsed = routeFromPath(saved.split("?")[0] || "/");
                const target = parsed.projectId === last
                  ? saved
                  : `/projects/${encodeURIComponent(last)}/bible`;
                requestNavigation(target, () => {
                  window.history.pushState({}, "", target);
                  window.dispatchEvent(new PopStateEvent("popstate"));
                });
              } else go("studio", null, null);
            }}>
              <span className="spine-foot-icon" aria-hidden="true">返</span>
              <span className="spine-foot-copy"><b>返回项目空间</b><span>继续当前小说创作</span></span>
              <i className="spine-foot-arrow" aria-hidden="true">←</i>
            </button>
          ) : isSystemAdminUser ? (
            <button
              className="spine-foot-action"
              type="button"
              aria-label="系统设置"
              onMouseEnter={() => prefetchView("system")}
              onFocus={() => prefetchView("system")}
              onTouchStart={() => prefetchView("system")}
              onClick={() => go("system", null, null)}
            >
              <span className="spine-foot-icon" aria-hidden="true">设</span>
              <span className="spine-foot-copy"><b>系统设置</b><span>模型与全局策略</span></span>
              <i className="spine-foot-arrow" aria-hidden="true">→</i>
            </button>
          ) : null}
          <UserMenu
            auth={auth}
            open={userMenuOpen}
            onToggle={() => setUserMenuOpen((open) => !open)}
            onChangePassword={() => {
              setUserMenuOpen(false);
              setChangePasswordOpen(true);
            }}
            onLogout={() => {
              setUserMenuOpen(false);
              void auth.logout();
            }}
            menuRef={userMenuRef}
          />
          <small>漫剧案头 · 2.0</small>
        </div>
      </aside>
      <main className={`desk ${view === "board" ? "board-desk" : ""} ${view === "system" ? "system-desk" : ""}`}>
        <ErrorBoundary
          resetKey={`${view}:${projectId}:${episodeId}:${chapterIdx}`}
          actions={
            <button type="button" className="btn" onClick={() => go("studio", null, null)}>
              返回项目空间
            </button>
          }
        >
        <Suspense
          fallback={
            <div className="empty route-loading" role="status">
              正在打开工作台…
            </div>
          }
        >
        {view === "studio" && <Studio />}
        {view === "bible" && projectId && <BiblePage key={projectId} />}
        {view === "scenes" && projectId && <ScenesPage key={projectId} />}
        {view === "episodes" && projectId && <EpisodesPage key={projectId} />}
        {view === "reader" && projectId && <ReaderPage key={projectId} />}
        {view === "script" &&
          (episodeId ? (
            <ScriptPage key={episodeId} />
          ) : (
            <WorkspaceEmpty label="映射台" view="script" />
          ))}
        {view === "board" &&
          (episodeId ? (
            <BoardPage key={episodeId} />
          ) : (
            <WorkspaceEmpty label="分镜台" view="board" />
          ))}
        {view === "wall" &&
          (episodeId ? (
            <WallPage key={episodeId} />
          ) : (
            <WorkspaceEmpty label="生成台" view="wall" />
          ))}
        {view === "cinema" &&
          (episodeId ? (
            <CinemaPage key={episodeId} />
          ) : (
            <WorkspaceEmpty label="成片台" view="cinema" />
          ))}
        {view === "series" && projectId && <SeriesPage key={projectId} />}
        {view === "observability" && projectId && (
          <MonitorPage mode="project" projectId={projectId} projectName={currentProject?.name} />
        )}
        {view === "system" && isSystemAdminUser && (
          currentPathname.endsWith("/accounts")
            ? <AccountAdminPage />
            : <MonitorPage mode="system" />
        )}
        </Suspense>
        </ErrorBoundary>
      </main>
      {pendingNavigation && (
        <DecisionDialog
          title={pendingNavigation.prompt.title}
          summary={pendingNavigation.prompt.summary}
          message={pendingNavigation.prompt.message}
          details={pendingNavigation.prompt.details}
          confirmLabel={pendingNavigation.prompt.confirmLabel}
          cancelLabel={pendingNavigation.prompt.cancelLabel || "继续编辑"}
          danger={pendingNavigation.prompt.danger}
          onConfirm={confirmPendingNavigation}
          onClose={cancelPendingNavigation}
        />
      )}
      {changePasswordOpen && (
        <ChangePasswordDialog
          onClose={() => setChangePasswordOpen(false)}
          toast={toast}
        />
      )}
      {toastMsg && (
        <div role="status" className={`toast ${toastMsg.err ? "err" : ""}`}>
          {toastMsg.text}
        </div>
      )}
    </NavCtx.Provider>
  );
}

/** 侧栏底部的用户菜单：显示当前用户名 + 是否系统管理员，收纳登出/改密。
 *  账号即项目空间：不再有团队/角色概念，账号本身就是独立空间。 */
function UserMenu({
  auth,
  open,
  onToggle,
  onChangePassword,
  onLogout,
  menuRef,
}: {
  auth: ReturnType<typeof useAuth>;
  open: boolean;
  onToggle: () => void;
  onChangePassword: () => void;
  onLogout: () => void;
  menuRef: RefObject<HTMLDivElement>;
}) {
  const subtitle = auth.isSystemAdmin ? "系统管理员" : "";
  return (
    <div className="user-menu" ref={menuRef}>
      <button
        type="button"
        className="workspace-switcher-trigger user-menu-trigger"
        aria-expanded={open}
        aria-label="用户菜单"
        onClick={onToggle}
      >
        <span className="workspace-avatar" aria-hidden="true">人</span>
        <span><b>{auth.user?.username || "未登录"}</b><small>{subtitle}</small></span>
        <i aria-hidden="true">{open ? "⌃" : "⌄"}</i>
      </button>
      {open && (
        <div className="user-menu-popover">
          <ThemeSwitch />
          <button type="button" onClick={onChangePassword}>修改密码</button>
          <button type="button" className="danger" onClick={onLogout}>登出</button>
        </div>
      )}
    </div>
  );
}

/** 修改密码：后端成功后会吊销其余会话并签发新 token（api.ts 的 changePassword
 *  已经把新 token 记进内存），这里只负责表单与提示。 */
function ChangePasswordDialog({
  onClose,
  toast,
}: {
  onClose: () => void;
  toast: (message: string, isErr?: boolean) => void;
}) {
  const titleId = useId();
  const oldId = useId();
  const newId = useId();
  const confirmId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    if (newPassword.length < 8) {
      setError("新口令至少 8 位");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("两次输入的新口令不一致");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await changePassword(oldPassword, newPassword);
      toast("密码已修改");
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "修改失败，请检查网络后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="evidence-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section
        ref={trapRef}
        className="impact-dialog decision-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
      <form onSubmit={submit}>
        <h3 id={titleId}>修改密码</h3>
        <div className="login-field">
          <label className="f" htmlFor={oldId}>原密码</label>
          <input
            id={oldId}
            type="password"
            autoComplete="current-password"
            disabled={busy}
            value={oldPassword}
            onChange={(event) => setOldPassword(event.target.value)}
          />
        </div>
        <div className="login-field">
          <label className="f" htmlFor={newId}>新密码（至少 8 位）</label>
          <input
            id={newId}
            type="password"
            autoComplete="new-password"
            disabled={busy}
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
          />
        </div>
        <div className="login-field">
          <label className="f" htmlFor={confirmId}>确认新密码</label>
          <input
            id={confirmId}
            type="password"
            autoComplete="new-password"
            disabled={busy}
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
          />
        </div>
        {error && <p className="field-error" role="alert">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? "提交中…" : "确认修改"}
          </button>
        </div>
      </form>
      </section>
    </div>
  );
}

function WorkspaceEmpty({ label, view }: { label: string; view: View }) {
  const { projectId, go } = useNav();
  const titleId = useId();
  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label={label} view={view} />
        <h1>
          {label} <span className="sub">请选择或创建分集后进入</span>
        </h1>
        <hr className="rule" />
      </header>
      <section className="empty workspace-empty" aria-labelledby={titleId}>
        <div className="big" aria-hidden="true">集</div>
        <h2 id={titleId}>尚未进入具体分集</h2>
        <p>前往分集规划检查并选择已有分集；若项目尚无分集，可在那里创建。</p>
        <div className="workspace-empty-actions">
          {projectId && (
            <button
              type="button"
              className="btn primary"
              onClick={() => go("episodes", projectId, null)}
            >
              查看分集并选择
            </button>
          )}
          <button
            type="button"
            className="btn"
            onClick={() => go("studio", null, null)}
          >
            返回项目空间
          </button>
        </div>
      </section>
    </>
  );
}

/** 轮询某资源；interval=0 或函数返回 0 不轮询。intervalMs 传函数时可按最新数据动态调间隔。
 *  手动 refresh 会重新唤醒并计算轮询间隔，覆盖 idle → running 的异步任务状态切换。
 *  内置单飞、卸载后响应保护；页面重新获得焦点时立即追平一次后端状态。 */
export function shouldRetryPollError(error: unknown): boolean {
  return Number((error as { status?: number } | null)?.status) !== 404;
}

export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: PollInterval<T>,
  deps: unknown[] = [],
  options: { refreshOnFocus?: boolean } = {},
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  // ApiError 的 status（403/404/…）单独存一份，供 QueryState 判断「无权访问」/
  // 「跨账号资源不存在」——error 本身只是拼好的展示文案，不该反过来解析它。
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const pollerRef = useRef<AdaptivePoller<T>>();
  const onData = (next: T) => {
    setData(next);
    setError(null);
    setErrorStatus(null);
    setLoading(false);
  };
  const onError = (e: unknown) => {
    setError(String((e as Error).message || e));
    setErrorStatus(Number((e as { status?: number } | null)?.status ?? NaN) || null);
    setLoading(false);
    return shouldRetryPollError(e);
  };
  if (!pollerRef.current) {
    pollerRef.current = new AdaptivePoller(fetcher, intervalMs, { onData, onError });
  }
  pollerRef.current.update(fetcher, intervalMs, { onData, onError });

  const refresh = useCallback(
    (options?: { force?: boolean }): Promise<T | null> =>
      pollerRef.current!.refresh(options),
    [],
  );

  useEffect(() => {
    if (deps.some((d) => d == null)) return;
    const poller = pollerRef.current!;
    const syncVisibility = () => {
      if (document.visibilityState === "hidden") {
        poller.stop();
      } else {
        void poller.start();
      }
    };
    const catchUp = () => {
      if (document.visibilityState === "visible") void poller.refresh();
    };
    void poller.start().finally(() => {
      if (document.visibilityState === "hidden") poller.stop();
    });
    document.addEventListener("visibilitychange", syncVisibility);
    if (options.refreshOnFocus !== false) {
      window.addEventListener("focus", catchUp);
    }
    return () => {
      window.removeEventListener("focus", catchUp);
      document.removeEventListener("visibilitychange", syncVisibility);
      poller.stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, status: errorStatus, loading, refresh };
}

/** 项目是否处于运行态——空闲时停轮询，避免反复拉取数 MB 的项目 payload。 */
const projectBusy = (p: Project | null): boolean => {
  if (!p) return true;
  if (p.bible_status === "running" || p.plan_status === "running") return true;
  if (p.refs_status === "running" || p.scene_refs_status === "running")
    return true;
  if (
    p.episodes?.some(
      (ep) =>
        ep.screenplay_production?.task_active === true ||
        (ep.screenplay_production == null && (
          ep.screenplay_status === "queued" ||
          ep.screenplay_status === "running"
        )) ||
        ep.status === "scripting" ||
        ep.status === "generating",
    )
  )
    return true;
  return false;
};

export const useProject = (
  projectId: string,
  intervalMs: PollInterval<Project> = (p) => (projectBusy(p) ? 3000 : 0),
  view?: "bible" | "scenes" | "episodes" | "picker" | "picker_generation",
) =>
  usePoll<Project>(
    () => api.getProject(projectId, view ? `view=${view}` : undefined),
    intervalMs,
    [projectId, view],
  );

/** 全片补齐 Supervisor 仍在协调时视为忙碌（即使镜头队列暂时为空）。 */
function videoSupervisorBusy(ep: Episode): boolean {
  if (
    ep.video_supervisor?.task_running === true ||
    ep.video_supervisor?.running === true ||
    Number(ep.video_supervisor?.active_media_jobs || 0) > 0
  )
    return true;
  return false;
}

/** 分集是否处于运行态（编剧/分镜/参考图视频）—— 决定是否需要高频轮询。
 *  空闲时彻底停轮询，避免反复拉取 1MB+ 的分集 payload 拖垮页面。 */
export const episodeBusy = (ep: Episode | null): boolean => {
  if (!ep) return true; // 首次未拿到数据时，按可能忙碌处理触发首次拉取后的轮询
  if (ep.screenplay_production?.task_active === true) return true;
  if (!ep.screenplay_production && (
    ep.screenplay_status === "queued" || ep.screenplay_status === "running"
  )) return true;
  // storyboard_status is the run-aware authority.  A stale episode.status can
  // remain "scripting" after a FAILED run; treating that projection as live
  // makes the board poll forever and keeps showing the old running state.
  if (ep.storyboard_status?.state === "running") return true;
  if (!ep.storyboard_status && (ep.status === "scripting" || ep.status === "drafting")) return true;
  if (videoSupervisorBusy(ep)) return true;
  if (
    ep.shots?.some(
      (s) =>
        s.versions?.some(
          (v) =>
            v.status === "queued" ||
            v.status === "running" ||
            v.status === "waiting_provider",
        ) ||
        (s.pipeline != null &&
          [
            "queued",
            "running",
            "waiting",
            "waiting_provider",
            "blocked",
          ].includes(s.pipeline.pipeline_status)),
    )
  )
    return true;
  return false;
};

export const useEpisode = (
  episodeId: string,
  view?: "script" | "board" | "wall" | "cinema",
  intervalMs: PollInterval<Episode> = (ep) => (episodeBusy(ep) ? 2000 : 0),
) =>
  usePoll<Episode>(
    () => api.getEpisode(episodeId, view ? `view=${view}` : undefined),
    intervalMs,
    [episodeId, view],
  );

type ScreenplayLightStatus = Partial<Episode> & { id: string; active: boolean };

export function screenplayStatusPollInterval(
  value: ScreenplayLightStatus | null,
): number {
  return value?.active ? 2000 : 15000;
}

/**
 * 映射台初始/终态拉详情，运行中高频、终态低频轮询轻量快照。
 * 1646 集项目不再每 2s 重复传输正文和全部台词。
 */
/**
 * 轻量状态轮询的启动条件。
 *
 * 详情响应已经带着同一份 screenplay_production / screenplay_state / shot_count，
 * 两个端点跑的是**同一套**已发布权威校验。首屏并发拉两次就是一次纯重复请求，
 * 而轻量状态的职责本来只是“详情之后发生的变化”。所以详情未落地前返回 null，
 * usePoll 见到 null 依赖就不启动。
 */
export function screenplayStatusPollDeps(
  episodeId: string,
  detailLoaded: boolean,
): (string | null)[] {
  return [detailLoaded ? episodeId : null];
}

export function useScriptEpisode(episodeId: string) {
  const detail = useEpisode(episodeId, "script", 0);
  const status = usePoll<ScreenplayLightStatus>(
    () => api.getScreenplayStatus(episodeId),
    screenplayStatusPollInterval,
    screenplayStatusPollDeps(episodeId, Boolean(detail.data)),
  );
  const lastTerminalRef = useRef("");

  useEffect(() => {
    const light = status.data;
    if (!light || light.active || !detail.data) return;
    const terminalKey = `${light.screenplay_status}:${light.status}:${light.shot_count}:${light.screenplay_state?.version ?? 0}`;
    const detailKey = `${detail.data.screenplay_status}:${detail.data.status}:${detail.data.shot_count}:${detail.data.screenplay_state?.version ?? 0}`;
    if (terminalKey !== detailKey && lastTerminalRef.current !== terminalKey) {
      lastTerminalRef.current = terminalKey;
      void detail.refresh();
    }
  }, [detail.data, detail.refresh, status.data]);

  const light = status.data;
  const full = detail.data;
  const lightIsCurrent = Boolean(
    light &&
      full &&
      Number(light.screenplay_updated_at ?? 0) >=
        Number(full.screenplay_updated_at ?? 0),
  );
  const data = useMemo(
    () => (full && lightIsCurrent ? ({ ...full, ...light } as Episode) : full),
    [full, light, lightIsCurrent],
  );
  return {
    data,
    error: detail.error || (!full ? status.error : null),
    status: detail.status ?? (!full ? status.status : null),
    loading: detail.loading,
    refresh: async (options?: { force?: boolean }) => {
      const [next] = await Promise.all([
        detail.refresh(options),
        status.refresh(options),
      ]);
      return next;
    },
  };
}
