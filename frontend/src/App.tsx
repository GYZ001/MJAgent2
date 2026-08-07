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
} from "react";
import { api, Episode, Project } from "./api";
import Studio from "./pages/Studio";
import AgentDrawer from "./agent/AgentDrawer";
import type { ContextEnvelope } from "./agent/types";
import CapabilityApprovalHost from "./components/CapabilityApprovalHost";
import DecisionDialog from "./components/DecisionDialog";
import EpisodeCrumb from "./components/EpisodeCrumb";
import SearchField from "./components/SearchField";
import { useFocusTrap } from "./hooks/useFocusTrap";
import { useScrollContainment } from "./useScrollContainment";
import { AdaptivePoller, type PollInterval } from "./adaptivePoller";
import { resolveEpisodeId, resolveRoutedEpisodeId } from "./episodePicker";

const BiblePage = lazy(() => import("./pages/BiblePage"));
const ScenesPage = lazy(() => import("./pages/ScenesPage"));
const EpisodesPage = lazy(() => import("./pages/EpisodesPage"));
const ScriptPage = lazy(() => import("./pages/ScriptPage"));
const BoardPage = lazy(() => import("./pages/BoardPage"));
const WallPage = lazy(() => import("./pages/WallPage"));
const CinemaPage = lazy(() => import("./pages/CinemaPage"));
const MonitorPage = lazy(() => import("./pages/MonitorPage"));
const ReaderPage = lazy(() => import("./pages/ReaderPage"));

export type View =
  | "studio"
  | "bible"
  | "scenes"
  | "episodes"
  | "script"
  | "board"
  | "wall"
  | "cinema"
  | "observability"
  | "system"
  | "monitor"
  | "reader";

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

const SECTIONS: {
  key: View;
  label: string;
  icon: string;
  group: string;
  needProject?: boolean;
  needEpisode?: boolean;
  matchViews?: View[];
}[] = [
  {
    key: "bible",
    label: "前期准备",
    icon: "备",
    group: "前期准备",
    needProject: true,
    matchViews: ["bible", "scenes", "episodes"],
  },
  {
    key: "script",
    label: "剧本台",
    icon: "剧",
    group: "内容制作",
    needEpisode: true,
  },
  {
    key: "board",
    label: "分镜台",
    icon: "镜",
    group: "内容制作",
    needEpisode: true,
  },
  {
    key: "wall",
    label: "生成台",
    icon: "生",
    group: "质量交付",
    needEpisode: true,
  },
  {
    key: "cinema",
    label: "成片台",
    icon: "片",
    group: "质量交付",
    needEpisode: true,
  },
  { key: "observability", label: "观测台", icon: "观", group: "项目观测", needProject: true },
];

const SYSTEM_SECTIONS: Array<{ key: "overview" | "models" | "settings"; label: string; icon: string }> = [
  { key: "overview", label: "总览", icon: "总" },
  { key: "models", label: "模型中心", icon: "模" },
  { key: "settings", label: "系统设置", icon: "设" },
];

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
  if (parts[0] === "monitor")
    return {
      view: "monitor",
      projectId: null,
      episodeId: null,
      chapterIdx: null,
    };
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

export function locationFor(
  view: View,
  projectId: string | null,
  episodeId: string | null,
  chapterIdx: number | null,
) {
  if (view === "studio") return "/workspaces";
  if (view === "system") return "/system/overview";
  if (view === "monitor") return "/monitor";
  if (!projectId) return "/workspaces";
  const project = `/projects/${encodeURIComponent(projectId)}`;
  if (view === "observability") return `${project}/observability/runs`;
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

export default function App() {
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
  const [unsavedDraft, setUnsavedDraft] = useState(false);
  const [pendingNavigation, setPendingNavigation] =
    useState<PendingNavigation | null>(null);
  const navigationGuardRef = useRef<NavigationGuardPrompt | null>(null);
  const lastLocationRef = useRef(
    `${window.location.pathname}${window.location.search}`,
  );
  const [agentOpen, setAgentOpen] = useState(false);
  const [agentEnabled, setAgentEnabled] = useState(true);
  const agentToggleRef = useRef<HTMLButtonElement | null>(null);
  const restoreAgentFocusRef = useRef(false);
  const toastTimerRef = useRef<number>();
  const projectsRetryTimerRef = useRef<number>();
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

  const refreshProjects = useCallback(() => {
    if (projectsRetryTimerRef.current) {
      window.clearTimeout(projectsRetryTimerRef.current);
      projectsRetryTimerRef.current = undefined;
    }
    void api.get("/projects").then((items: Project[]) => {
      setProjects(items);
      setProjectsLoaded(true);
    }).catch(() => {
      setProjectsLoaded(true);
      projectsRetryTimerRef.current = window.setTimeout(refreshProjects, 2000);
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

  useEffect(() => {
    api
      .get("/settings")
      .then((settings: Record<string, string>) => {
        const raw = String(settings.agent_enabled ?? "true")
          .trim()
          .toLowerCase();
        setAgentEnabled(["1", "true", "yes", "on"].includes(raw));
      })
      .catch(() => setAgentEnabled(true));
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
    (guard: NavigationGuardPrompt | null, unsaved = false) => {
      navigationGuardRef.current = guard;
      setUnsavedDraft(unsaved);
    },
    [],
  );

  const closeAgent = useCallback(() => {
    restoreAgentFocusRef.current = true;
    setAgentOpen(false);
  }, []);

  useEffect(() => {
    if (agentOpen || !restoreAgentFocusRef.current) return;
    restoreAgentFocusRef.current = false;
    agentToggleRef.current?.focus();
  }, [agentOpen]);

  const go = useCallback(
    (
      v: View,
      pid?: string | null,
      eid?: string | null,
      cidx?: number | null,
    ) => {
      const globalView = v === "studio" || v === "monitor" || v === "system";
      const nextProjectId = globalView
        ? null
        : pid === undefined
          ? projectId
          : pid;
      const nextEpisodeId = globalView
        ? null
        : eid === undefined
          ? episodeId
          : eid;
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
          historyAction: "push",
          prompt: navigationGuardRef.current,
        });
        setMobileNavOpen(false);
        return;
      }
      if (currentLocation !== target) {
        window.history.pushState({}, "", target);
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
    setUnsavedDraft(false);
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
    const root = document.getElementById("root");
    root?.classList.toggle("agent-open", agentOpen);
    return () => {
      root?.classList.remove("agent-open");
    };
  }, [agentOpen]);

  useEffect(() => {
    if (!projectId) {
      setEpisodeId(null);
      return;
    }
    const location = readLocation();
    const requestedEpisodeId =
      location.projectId === projectId ? location.episodeId : null;
    let cancelled = false;
    api
      .get(`/projects/${projectId}?view=picker`)
      .then((project: Project) => {
        if (cancelled) return;
        const episodes = project.episodes ?? [];
        setEpisodeId((current) =>
          resolveRoutedEpisodeId(episodes, current, requestedEpisodeId),
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
  const agentContext: ContextEnvelope = {
    route: view,
    project_id: projectId,
    episode_id: episodeId,
    unsaved_draft: unsavedDraft,
  };

  const openSection = async (s: (typeof SECTIONS)[number]) => {
    if (!s.needEpisode || !projectId) {
      go(s.key);
      return;
    }

    try {
      // 分集可能在项目打开后才生成，进入制作工作台前必须重新读取，不能依赖首次加载的快照。
      const project = (await api.get(
        `/projects/${projectId}?view=picker`,
      )) as Project;
      const nextEpisodeId = resolveEpisodeId(project.episodes ?? [], episodeId);
      go(s.key, projectId, nextEpisodeId);
    } catch {
      toast("分集列表加载失败，已打开工作台入口；可点击顶部的分集切换器重试", true);
      go(s.key);
    }
  };

  const groupedSections = visibleSections.reduce<
    Record<string, typeof visibleSections>
  >((groups, section) => {
    (groups[section.group] ??= []).push(section);
    return groups;
  }, {});
  const currentProject = projects.find((project) => project.id === projectId);
  const currentPathname = routeRevision >= 0 ? window.location.pathname : "";
  const filteredProjects = projects.filter((project) =>
    project.name.toLowerCase().includes(projectSearch.trim().toLowerCase()),
  );
  const switchProject = (nextProjectId: string) => {
    setProjectSwitcherOpen(false);
    setProjectSearch("");
    if (nextProjectId === projectId) return;
    const intent = new URLSearchParams(window.location.search);
    if (view === "studio" && intent.get("intent") === "observability") {
      const requestedTab = intent.get("tab") || "runs";
      const tab = ["runs", "jobs", "calls"].includes(requestedTab) ? requestedTab : "runs";
      const target = `/projects/${encodeURIComponent(nextProjectId)}/observability/${tab}`;
      requestNavigation(target, () => {
        window.history.pushState({}, "", target);
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
      return;
    }
    if (view === "observability") {
      const currentTab = window.location.pathname.split("/").filter(Boolean).at(-1);
      const tab = ["runs", "jobs", "calls"].includes(currentTab || "") ? currentTab : "runs";
      const target = `/projects/${encodeURIComponent(nextProjectId)}/observability/${tab}`;
      requestNavigation(target, () => {
        window.history.pushState({}, "", target);
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
      return;
    }
    const targetView: View = view === "studio" || view === "system" || view === "monitor"
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
        {view === "system" ? (
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
                    onClick={() => {
                      void openSection(s);
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
          {view === "system" ? (
            <button className="spine-foot-action" type="button" aria-label="返回项目空间" onClick={() => {
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
          ) : (
            <button className="spine-foot-action" type="button" aria-label="系统设置" onClick={() => go("system", null, null)}>
              <span className="spine-foot-icon" aria-hidden="true">设</span>
              <span className="spine-foot-copy"><b>系统设置</b><span>模型与全局策略</span></span>
              <i className="spine-foot-arrow" aria-hidden="true">→</i>
            </button>
          )}
          <small>漫剧案头 · 2.0</small>
        </div>
      </aside>
      <main className={`desk ${view === "board" ? "board-desk" : ""} ${view === "system" ? "system-desk" : ""}`}>
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
            <WorkspaceEmpty label="剧本台" view="script" />
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
        {view === "observability" && projectId && (
          <MonitorPage mode="project" projectId={projectId} projectName={currentProject?.name} />
        )}
        {view === "system" && <MonitorPage mode="system" />}
        {view === "monitor" && <LegacyMonitorRedirect loaded={projectsLoaded} toast={toast} />}
        </Suspense>
      </main>
      {agentEnabled && !agentOpen && (
        <button
          ref={agentToggleRef}
          type="button"
          className="agent-toggle"
          aria-label="打开案头助手"
          aria-expanded={false}
          aria-controls="agent-drawer"
          title="打开案头助手"
          onClick={() => setAgentOpen(true)}
        >
          <svg
            className="agent-toggle-icon"
            viewBox="0 0 22 18"
            aria-hidden="true"
            focusable="false"
          >
            <rect
              x="1.25"
              y="1.25"
              width="19.5"
              height="15.5"
              rx="2.2"
              ry="2.2"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            />
            <path
              d="M15.25 1.25v15.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            />
          </svg>
        </button>
      )}
      {agentEnabled && (
        <AgentDrawer
          open={agentOpen}
          onClose={closeAgent}
          context={agentContext}
        />
      )}
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
      <CapabilityApprovalHost />
      {toastMsg && (
        <div role="status" className={`toast ${toastMsg.err ? "err" : ""}`}>
          {toastMsg.text}
        </div>
      )}
    </NavCtx.Provider>
  );
}

function LegacyMonitorRedirect({
  loaded,
  toast,
}: {
  loaded: boolean;
  toast: (message: string, isErr?: boolean) => void;
}) {
  useEffect(() => {
    if (!loaded) return;
    const params = new URLSearchParams(window.location.search);
    const section = params.get("section") || "overview";
    const move = (target: string) => {
      window.history.replaceState({}, "", target);
      window.dispatchEvent(new PopStateEvent("popstate"));
    };
    if (["overview", "models", "settings"].includes(section)) {
      params.delete("section");
      move(`/system/${section}${params.toString() ? `?${params}` : ""}`);
      return;
    }
    const objectQuery = params.get("run_id")
      ? `run_id=${encodeURIComponent(params.get("run_id")!)}`
      : params.get("job_id")
        ? `job_id=${encodeURIComponent(params.get("job_id")!)}&source=auto`
        : params.get("call_id")
          ? `call_id=${encodeURIComponent(params.get("call_id")!)}`
          : "";
    if (objectQuery) {
      void api.get(`/observability/resolve?${objectQuery}`).then((result: {
        project_id: string;
        section: "runs" | "jobs" | "calls";
      }) => {
        params.delete("section");
        move(`/projects/${encodeURIComponent(result.project_id)}/observability/${result.section}${params.toString() ? `?${params}` : ""}`);
      }).catch((error) => {
        toast(`旧观测链接无法迁移：${(error as Error).message}`, true);
        move("/workspaces");
      });
      return;
    }
    const nextSection = ["runs", "jobs", "calls"].includes(section) ? section : "runs";
    move(`/workspaces?intent=observability&tab=${nextSection}`);
  }, [loaded, toast]);
  return <div className="empty route-loading" role="status">正在迁移旧观测链接…</div>;
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
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const pollerRef = useRef<AdaptivePoller<T>>();
  if (!pollerRef.current) {
    pollerRef.current = new AdaptivePoller(fetcher, intervalMs, {
      onData: (next) => {
        setData(next);
        setError(null);
        setLoading(false);
      },
      onError: (e: unknown) => {
        setError(String((e as Error).message || e));
        setLoading(false);
        return shouldRetryPollError(e);
      },
    });
  }
  pollerRef.current.update(fetcher, intervalMs, {
    onData: (next) => {
      setData(next);
      setError(null);
      setLoading(false);
    },
    onError: (e: unknown) => {
      setError(String((e as Error).message || e));
      setLoading(false);
      return shouldRetryPollError(e);
    },
  });

  const refresh = useCallback(
    (): Promise<T | null> => pollerRef.current!.refresh(),
    [],
  );

  useEffect(() => {
    if (deps.some((d) => d == null)) return;
    const poller = pollerRef.current!;
    void poller.start();
    const catchUp = () => {
      if (document.visibilityState === "visible") void poller.refresh();
    };
    window.addEventListener("focus", catchUp);
    document.addEventListener("visibilitychange", catchUp);
    return () => {
      window.removeEventListener("focus", catchUp);
      document.removeEventListener("visibilitychange", catchUp);
      poller.stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, refresh };
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
        ep.screenplay_status === "queued" ||
        ep.screenplay_status === "running" ||
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
    () => api.get(`/projects/${projectId}${view ? `?view=${view}` : ""}`),
    intervalMs,
    [projectId, view],
  );

const VIDEO_SUPERVISOR_TERMINAL = new Set([
  "SUCCEEDED_COVERED",
  "COMPLETED_DEADLINE_FALLBACK",
  "PARTIAL_NO_USABLE_CANDIDATE",
  "FAILED_CLOSED",
  "CANCELLED",
]);

/** 全片补齐 Supervisor 仍在协调时视为忙碌（即使镜头队列暂时为空）。 */
function videoSupervisorBusy(ep: Episode): boolean {
  const phase =
    ep.video_supervisor && typeof ep.video_supervisor.phase === "string"
      ? ep.video_supervisor.phase
      : "";
  if (phase && VIDEO_SUPERVISOR_TERMINAL.has(phase)) return false;
  if (
    ep.video_supervisor?.task_running === true ||
    ep.video_supervisor?.running === true
  )
    return true;
  return false;
}

/** 分集是否处于运行态（编剧/分镜/参考图视频）—— 决定是否需要高频轮询。
 *  空闲时彻底停轮询，避免反复拉取 1MB+ 的分集 payload 拖垮页面。 */
const episodeBusy = (ep: Episode | null): boolean => {
  if (!ep) return true; // 首次未拿到数据时，按可能忙碌处理触发首次拉取后的轮询
  if (ep.screenplay_status === "queued" || ep.screenplay_status === "running") return true;
  if (ep.status === "scripting" || ep.status === "drafting") return true;
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
    () => api.get(`/episodes/${episodeId}${view ? `?view=${view}` : ""}`),
    intervalMs,
    [episodeId, view],
  );

type ScreenplayLightStatus = Partial<Episode> & { id: string; active: boolean };

/**
 * 剧本台初始/终态拉详情，运行中只轮询轻量快照。
 * 1646 集项目不再每 2s 重复传输正文和全部台词。
 */
export function useScriptEpisode(episodeId: string) {
  const detail = useEpisode(episodeId, "script", 0);
  const status = usePoll<ScreenplayLightStatus>(
    () => api.get(`/episodes/${episodeId}/screenplay/status`),
    (value) => (value?.active ? 2000 : 0),
    [episodeId],
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
    loading: detail.loading,
    refresh: async () => {
      const [next] = await Promise.all([detail.refresh(), status.refresh()]);
      return next;
    },
  };
}
