import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import { api } from "../api";
import type {
  Call,
  CallsPage,
  CatalogModel,
  Health,
  Job,
  JobsPage,
  JobsSummary,
  ModelCatalog,
  ModelSelection,
  SettingsView,
  SystemOverview,
} from "../api";
import { useNav, usePoll } from "../App";
import { useAuth } from "../auth/AuthContext";
import SearchField from "../components/SearchField";
import TraceDrawer, {
  type TraceTarget,
} from "../components/observability/TraceDrawer";
import "../styles/MonitorPage.css";
import CallDrawer from "./monitor/CallDrawer";
import CallsSection from "./monitor/CallsSection";
import JobDrawer from "./monitor/JobDrawer";
import JobsSection from "./monitor/JobsSection";
import ModelCenter from "./monitor/ModelsSection";
import SettingsPanel from "./monitor/SettingsSection";
import ProjectOverviewSection, { SystemOverviewSection } from "./monitor/OverviewSection";
import {
  CALL_KIND_LABELS,
  CALL_STATUS_LABELS,
  JOB_STATUS_LABELS,
  SECTIONS,
  SYSTEM_SECTION_DESCRIPTIONS,
  VALID_SECTIONS,
  WORKFLOW_LABELS,
  assertProjectScope,
  blockStatus,
  callBusinessLabel,
  callNextStep,
  callPurpose,
  callStatusLabel,
  DataBoundary,
  encodeQuery,
  fmtTime,
  isProviderCreateUnresolved,
  jobBusinessLabel,
  jobNextStep,
  jobStatusLabel,
  jobWorkLabel,
  nowQuery,
  Pagination,
  PROVIDER_RESUBMISSION_WARNING,
  queryTarget,
  querySection,
  stampClass,
  track,
  useBlockTelemetry,
  writeQuery,
} from "./monitor/shared";
import type { BlockStatus, MonitorSection } from "./monitor/shared";
import {
  modelAssignmentSettingKey,
  modelAssignmentValue,
  modelBusinessLabel,
  modelProviderOptions,
} from "./monitor/models/constants";
import {
  categorizeSettingKeys,
  normalizeDraft,
  settingOptionLabel,
} from "./monitor/SettingsSection";

// MonitorPage.test.ts 直接从本文件按名导入这些纯函数/类型（拆分前就是这样，
// 拆分只是把实现挪到 monitor/ 子模块，这里原样转发，保持测试文件不用改）。
// 页面路由用的 mode（project/system 两种壳），只在这个文件里用，不属于
// monitor/shared.tsx 的公共分区常量。
export type MonitorMode = "project" | "system";
export {
  blockStatus,
  callBusinessLabel,
  callNextStep,
  categorizeSettingKeys,
  isProviderCreateUnresolved,
  jobBusinessLabel,
  jobNextStep,
  modelAssignmentSettingKey,
  modelAssignmentValue,
  modelBusinessLabel,
  modelProviderOptions,
  normalizeDraft,
  PROVIDER_RESUBMISSION_WARNING,
  settingOptionLabel,
};
export type { Call, CatalogModel, Job, ModelSelection } from "../api";
export type { SettingSchema } from "../api";

export default function MonitorPage({
  mode,
  projectId,
  projectName,
}: {
  mode: MonitorMode;
  projectId?: string;
  projectName?: string;
}) {
  const { go, toast, registerNavigationGuard, requestNavigation } = useNav();
  const { isSystemAdmin } = useAuth();
  const initial = nowQuery();
  // 「系统设置」子页只对系统管理员开放：App.tsx 已经在路由层拦住非管理员进入
  // mode="system"，这里是同一条边界在组件内部的兜底，两处都只是 UX 层面的隐藏，
  // 真正的授权仍由后端 403/404 兜底。
  const allowedSections = useMemo(() => mode === "project"
    ? SECTIONS.filter((item) => ["jobs", "calls"].includes(item.key))
    : SECTIONS.filter((item) =>
        ["overview", "models", "settings"].includes(item.key)
        && (item.key !== "settings" || isSystemAdmin)), [mode, isSystemAdmin]);
  const defaultSection: MonitorSection = mode === "project" ? "jobs" : "overview";
  const initialSection = querySection();
  const [activeSection, setActiveSection] =
    useState<MonitorSection>(allowedSections.some((item) => item.key === initialSection)
      ? initialSection
      : defaultSection);
  const activeSectionMeta = allowedSections.find((item) => item.key === activeSection)
    || allowedSections[0];
  const pageTitle = mode === "system" ? activeSectionMeta.label : "观测台";
  const pageDescription = mode === "system"
    ? SYSTEM_SECTION_DESCRIPTIONS[activeSection] || activeSectionMeta.description
    : "仅展示当前项目的任务与模型调用数据";
  const [urlNotice, setUrlNotice] = useState("");
  const [jobSearch, setJobSearch] = useState(initial.get("job_search") || "");
  const [jobStatus, setJobStatus] = useState(initial.get("job_status") || "");
  const [jobProject, setJobProject] = useState(projectId || initial.get("job_project") || "");
  const [jobWorkflow, setJobWorkflow] = useState(
    initial.get("job_workflow") || "",
  );
  const [jobFrom, setJobFrom] = useState(initial.get("job_from") || "");
  const [jobTo, setJobTo] = useState(initial.get("job_to") || "");
  const [jobSort, setJobSort] = useState(initial.get("job_sort") || "desc");
  const [jobPage, setJobPage] = useState(
    Math.max(1, Number(initial.get("job_page")) || 1),
  );
  const [jobPageSize, setJobPageSize] = useState(
    Math.max(1, Number(initial.get("job_page_size")) || 20),
  );
  const [selectedJob, setSelectedJob] = useState<Job | null>(null);
  const [selectedJobId, setSelectedJobId] = useState(
    initial.get("job_id") || initial.get("run_id") || "",
  );
  const [callSearch, setCallSearch] = useState(
    initial.get("call_search") || "",
  );
  const [callStatus, setCallStatus] = useState(
    initial.get("call_status") || "",
  );
  const [callModel, setCallModel] = useState(initial.get("call_model") || "");
  const [callFrom, setCallFrom] = useState(initial.get("call_from") || "");
  const [callTo, setCallTo] = useState(initial.get("call_to") || "");
  const [callProject, setCallProject] = useState(projectId || initial.get("call_project") || "");
  const [callFunction, setCallFunction] = useState(
    initial.get("call_function") || "",
  );
  const [callSort, setCallSort] = useState(initial.get("call_sort") || "desc");
  const [callIds, setCallIds] = useState(initial.get("call_ids") || "");
  const [callPage, setCallPage] = useState(
    Math.max(1, Number(initial.get("call_page")) || 1),
  );
  const [callPageSize, setCallPageSize] = useState(
    Math.max(1, Number(initial.get("call_page_size")) || 20),
  );
  const [selectedCall, setSelectedCall] = useState<Call | null>(null);
  const [selectedCallId, setSelectedCallId] = useState(
    Number(initial.get("call_id") || 0),
  );
  const [traceTarget, setTraceTarget] = useState<TraceTarget | null>(null);
  const [objectLoadError, setObjectLoadError] = useState("");
  const [refreshingSection, setRefreshingSection] = useState<MonitorSection | "">("");
  const observabilityBase = projectId
    ? `/projects/${encodeURIComponent(projectId)}/observability`
    : "";
  useLayoutEffect(() => {
    if (mode !== "project" || !/\/observability\/runs$/.test(window.location.pathname))
      return;
    const params = nowQuery();
    const runId = params.get("run_id");
    if (runId && !params.get("job_id")) {
      params.set("job_id", runId);
      params.set("source", "run");
    }
    params.delete("run_id");
    params.delete("focus");
    const pathname = window.location.pathname.replace(/\/runs$/, "/jobs");
    window.history.replaceState(
      {},
      "",
      `${pathname}${params.toString() ? `?${params}` : ""}`,
    );
    window.dispatchEvent(new Event("manju:locationchange"));
  }, [mode]);
  const jobsSummaryPoll = usePoll<JobsSummary>(
    async () => assertProjectScope(
      await api.getJobsSummary(projectId),
      projectId,
    ),
    0,
    [mode === "system" ? null : mode, projectId || mode],
    { refreshOnFocus: false },
  );
  const settingsPoll = usePoll<SettingsView>(
    () => api.getSettings(),
    0,
    [mode === "project" ? null : mode],
  );
  const features = settingsPoll.data?.features || {
    overview_state_v2: true,
    jobs_query_v2: true,
    run_center_v2: true,
    call_detail_v2: true,
    settings_edit_v2: true,
  };
  const healthPoll = usePoll<Health>(() => api.getHealth(), 0, [mode === "project" ? null : mode]);
  const catalogPoll = usePoll<ModelCatalog>(() => api.getModelCatalog(), 0, [mode === "project" ? null : mode]);
  const systemOverviewPoll = usePoll<SystemOverview>(
    () => api.getSystemOverview(),
    activeSection === "overview" ? 10000 : 0,
    [mode === "system" ? mode : null, activeSection],
  );
  const jobQuery = encodeQuery({
    page: jobPage,
    page_size: jobPageSize,
    search: jobSearch,
    status: jobStatus,
    project_id: projectId ? undefined : jobProject,
    workflow: jobWorkflow,
    from_ts: jobFrom ? new Date(jobFrom).getTime() / 1000 : undefined,
    to_ts: jobTo ? new Date(jobTo).getTime() / 1000 : undefined,
    sort: jobSort,
  });
  const jobsPagePoll = usePoll<JobsPage>(
    async () => assertProjectScope(
      await api.getJobsPage(jobQuery, projectId),
      projectId,
    ),
    0,
    [mode === "system" ? null : mode, activeSection, jobQuery, projectId || mode],
    { refreshOnFocus: false },
  );
  const callQuery = encodeQuery({
    page: callPage,
    page_size: callPageSize,
    search: callSearch,
    status: callStatus,
    category: "business",
    model: callModel,
    project_id: projectId ? undefined : callProject,
    function: callFunction,
    from_ts: callFrom ? new Date(callFrom).getTime() / 1000 : undefined,
    to_ts: callTo ? new Date(callTo).getTime() / 1000 : undefined,
    sort: callSort,
    ids: callIds,
  });
  const callsPagePoll = usePoll<CallsPage>(
    async () => assertProjectScope(
      await api.getCallsPage(callQuery, projectId),
      projectId,
    ),
    0,
    [mode === "system" ? null : mode, activeSection, callQuery, projectId || mode],
    { refreshOnFocus: false },
  );
  useEffect(() => {
    if (!jobsPagePoll.data || jobPage <= jobsPagePoll.data.page_count) return;
    setJobPage(jobsPagePoll.data.page_count);
    writeQuery({ job_page: String(jobsPagePoll.data.page_count) }, false);
    toast(`任务数据已变化，已回到最后合法页 ${jobsPagePoll.data.page_count}`);
  }, [jobPage, jobsPagePoll.data, toast]);
  useEffect(() => {
    if (!callsPagePoll.data || callPage <= callsPagePoll.data.page_count)
      return;
    setCallPage(callsPagePoll.data.page_count);
    writeQuery({ call_page: String(callsPagePoll.data.page_count) }, false);
    toast(`调用数据已变化，已回到最后合法页 ${callsPagePoll.data.page_count}`);
  }, [callPage, callsPagePoll.data, toast]);
  useEffect(() => {
    const onPop = () => {
      const p = nowQuery();
      const raw = p.get("section");
      if (raw && raw !== "runs" && !VALID_SECTIONS.has(raw as MonitorSection)) {
        setUrlNotice(`已忽略非法区域参数：${raw}`);
        setActiveSection("overview");
      } else {
        setUrlNotice("");
        const nextSection = querySection();
        setActiveSection(allowedSections.some((item) => item.key === nextSection)
          ? nextSection
          : defaultSection);
      }
      setJobSearch(p.get("job_search") || "");
      setJobStatus(p.get("job_status") || "");
      setJobProject(projectId || p.get("job_project") || "");
      setJobWorkflow(p.get("job_workflow") || "");
      setJobFrom(p.get("job_from") || "");
      setJobTo(p.get("job_to") || "");
      setJobSort(p.get("job_sort") || "desc");
      setJobPage(Math.max(1, Number(p.get("job_page")) || 1));
      setJobPageSize(Math.max(1, Number(p.get("job_page_size")) || 20));
      setSelectedJobId(p.get("job_id") || p.get("run_id") || "");
      setCallSearch(p.get("call_search") || "");
      setCallStatus(p.get("call_status") || "");
      setCallModel(p.get("call_model") || "");
      setCallFrom(p.get("call_from") || "");
      setCallTo(p.get("call_to") || "");
      setCallProject(projectId || p.get("call_project") || "");
      setCallFunction(p.get("call_function") || "");
      setCallSort(p.get("call_sort") || "desc");
      setCallIds(p.get("call_ids") || "");
      setCallPage(Math.max(1, Number(p.get("call_page")) || 1));
      setCallPageSize(Math.max(1, Number(p.get("call_page_size")) || 20));
      setSelectedCallId(Number(p.get("call_id") || 0));
      if (!p.get("job_id")) setSelectedJob(null);
      if (!p.get("call_id")) setSelectedCall(null);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [allowedSections, defaultSection, projectId]);
  const openSection = (
    section: MonitorSection,
    patch: Record<string, string | null> = {},
  ) => {
    const cleanup: Record<string, string | null> = {};
    cleanup.source = null;
    if (section !== "jobs") cleanup.job_id = null;
    if (section !== "calls") cleanup.call_id = null;
    cleanup.focus = null;
    cleanup.run_id = null;
    const queryPatch = {
      section,
      ...cleanup,
      ...patch,
    };
    const target = queryTarget(queryPatch);
    requestNavigation(target, () => {
      const source = nowQuery().get("section") || "overview";
      setObjectLoadError("");
      setTraceTarget(null);
      if (section !== "jobs") {
        setSelectedJob(null);
        setSelectedJobId("");
      }
      if (section !== "calls") {
        setSelectedCall(null);
        setSelectedCallId(0);
      }
      setActiveSection(section);
      writeQuery(queryPatch);
      track("drilldown", {
        source,
        target_type: section,
        filter_count: Object.values(patch).filter(Boolean).length,
      });
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  };
  const openTrace = (target: TraceTarget) => {
    setSelectedJob(null);
    setSelectedJobId("");
    setSelectedCall(null);
    setSelectedCallId(0);
    setObjectLoadError("");
    setTraceTarget(target);
    writeQuery({ job_id: null, call_id: null }, false);
  };
  const jobsStatus = blockStatus(
    jobsSummaryPoll.loading,
    jobsSummaryPoll.error,
    jobsSummaryPoll.data,
    !!jobsSummaryPoll.data && jobsSummaryPoll.data.total === 0,
  );
  const callsStatus = blockStatus(
    callsPagePoll.loading,
    callsPagePoll.error,
    callsPagePoll.data,
    !!callsPagePoll.data && callsPagePoll.data.total === 0,
  );
  const settingsStatus = blockStatus(
    settingsPoll.loading,
    settingsPoll.error,
    settingsPoll.data,
    !settingsPoll.data,
  );
  const healthStatus = blockStatus(
    healthPoll.loading,
    healthPoll.error,
    healthPoll.data,
    !healthPoll.data,
  );
  const catalogStatus = blockStatus(
    catalogPoll.loading,
    catalogPoll.error,
    catalogPoll.data,
    !!catalogPoll.data && catalogPoll.data.items.length === 0,
  );
  useBlockTelemetry("jobs", jobsStatus);
  useBlockTelemetry("calls", callsStatus);
  useBlockTelemetry("settings", settingsStatus);
  useBlockTelemetry("health", healthStatus);
  useBlockTelemetry("model_catalog", catalogStatus);
  useEffect(() => {
    if (!jobsPagePoll.data) return;
    track("query_result", {
      query_type: "jobs",
      total: jobsPagePoll.data.total,
      page_size: jobsPagePoll.data.page_size,
      query_ms: jobsPagePoll.data.query_ms || 0,
    });
  }, [jobsPagePoll.data?.server_time]);
  useEffect(() => {
    if (!callsPagePoll.data) return;
    track("query_result", {
      query_type: "calls",
      total: callsPagePoll.data.total,
      page_size: callsPagePoll.data.page_size,
      query_ms: callsPagePoll.data.query_ms || 0,
    });
  }, [callsPagePoll.data?.server_time]);
  const counts = jobsSummaryPoll.data?.counts || {};
  const jobFilterCount =
    [
      jobSearch,
      jobStatus,
      projectId ? "" : jobProject,
      jobWorkflow,
      jobFrom,
      jobTo,
      jobSort !== "desc" ? jobSort : "",
    ].filter(Boolean).length;
  const callFilterCount =
    [
      callSearch,
      callStatus,
      callModel,
      callFrom,
      callTo,
      projectId ? "" : callProject,
      callFunction,
      callSort !== "desc" ? callSort : "",
      callIds,
    ].filter(Boolean).length;
  const jobTimeInvalid = Boolean(
    jobFrom && jobTo && new Date(jobFrom).getTime() > new Date(jobTo).getTime(),
  );
  const callTimeInvalid = Boolean(
    callFrom &&
      callTo &&
      new Date(callFrom).getTime() > new Date(callTo).getTime(),
  );
  const refreshJobs = async () => {
    if (refreshingSection) return;
    setRefreshingSection("jobs");
    try {
      await Promise.all([jobsPagePoll.refresh(), jobsSummaryPoll.refresh()]);
    } finally {
      setRefreshingSection("");
    }
  };
  const refreshCalls = async () => {
    if (refreshingSection) return;
    setRefreshingSection("calls");
    try {
      await callsPagePoll.refresh();
    } finally {
      setRefreshingSection("");
    }
  };
  useEffect(() => {
    if (activeSection !== "jobs" || !selectedJobId) return;
    setObjectLoadError("");
    void api
      .getJobDetail(selectedJobId, "auto", projectId)
      .then((item) => {
        setSelectedJob(item as unknown as Job);
        track(
          "deep_link",
          { source: "url", target_type: "job" },
          selectedJobId,
        );
      })
      .catch((error) => {
        setSelectedJob(null);
        setObjectLoadError(
          `目标任务无法定位：${(error as Error).message}。不会改选其他任务。`,
        );
        track(
          "deep_link",
          { source: "url", target_type: "job", result: "failed" },
          selectedJobId,
        );
      });
  }, [activeSection, observabilityBase, projectId, selectedJobId]);
  useEffect(() => {
    if (activeSection !== "calls" || !selectedCallId) return;
    if (selectedCall?.id === selectedCallId) return;
    setObjectLoadError("");
    void api
      .getCallDetail(selectedCallId, projectId)
      .then((item) => {
        setSelectedCall(item as Call);
        track(
          "deep_link",
          { source: "url", target_type: "call" },
          String(selectedCallId),
        );
      })
      .catch((error) => {
        setSelectedCall(null);
        setObjectLoadError(
          `目标调用无法定位：${(error as Error).message}。不会改选其他调用。`,
        );
        track(
          "deep_link",
          { source: "url", target_type: "call", result: "failed" },
          String(selectedCallId),
        );
      });
  }, [activeSection, observabilityBase, projectId, selectedCall?.id, selectedCallId]);
  return (
    <div className="monitor-page">
      <header className="desk-head">
        <div className="crumb">
          漫剧案头 / {mode === "system" ? pageTitle : `${projectName || "当前项目"} / 观测台`}
        </div>
        <h1>
          {pageTitle}{" "}
          <span className="sub">{pageDescription}</span>
        </h1>
        <hr className="rule" />
      </header>
      {urlNotice && (
        <div className="monitor-state error" role="alert">
          {urlNotice}
        </div>
      )}
      {objectLoadError && (
        <div className="monitor-state error" role="alert">
          {objectLoadError}
          <button
            onClick={() => {
              setObjectLoadError("");
              setSelectedJobId("");
              setSelectedCallId(0);
              writeQuery({ job_id: null, call_id: null }, false);
            }}
          >
            返回当前列表
          </button>
        </div>
      )}
      {mode === "project" && (
        <div className="monitor-scope-banner" role="status">
          <span aria-hidden="true">锁</span>
          <div><b>{projectName || "当前项目"}</b><small>查询、详情与处理动作均由服务端锁定到本项目</small></div>
        </div>
      )}
      <div className="monitor-block-strip" aria-label="数据块状态">
        {(mode === "system"
          ? [["设置", settingsStatus, settingsPoll.data?.server_time], ["健康", healthStatus, undefined], ["模型库", catalogStatus, undefined]]
          : [["任务", jobsStatus, jobsSummaryPoll.data?.server_time], ["调用", callsStatus, callsPagePoll.data?.server_time]]
        ).map(([label, status, stamp]) => (
          <span className={`monitor-block-chip ${status}`} key={String(label)}>
            {label}：
            {status === "loading"
              ? "加载中"
              : status === "error"
                ? "失败"
                : status === "stale"
                  ? "已过期"
                  : status === "ready-empty"
                    ? "已确认空"
                    : "已同步"}
            {stamp && status !== "loading"
              ? ` · ${fmtTime(Number(stamp))}`
              : ""}
          </span>
        ))}
      </div>
      {mode !== "system" && (
        <nav className="monitor-subnav" aria-label="观测台子菜单">
          {allowedSections.map((section) => {
            const badge =
              section.key === "jobs" && jobsSummaryPoll.data
                ? (counts.running || 0) +
                  (counts.queued || 0) +
                  (counts.waiting_human || 0)
                : undefined;
            return (
              <button
                type="button"
                key={section.key}
                className={activeSection === section.key ? "active" : ""}
                aria-current={activeSection === section.key ? "page" : undefined}
                onClick={() => openSection(section.key)}
              >
                <span>
                  {section.label}
                  {badge != null && badge > 0 && <em>{badge}</em>}
                </span>
                <small>{section.description}</small>
              </button>
            );
          })}
        </nav>
      )}
      {mode !== "system" && activeSection === "overview" && !features.overview_state_v2 && (
        <section
          className="card monitor-section monitor-state stale"
          role="status"
        >
          新版总览已由独立发布开关停用；任务、运行与调用账本仍可从子菜单直接访问。
        </section>
      )}
      {mode === "system" && activeSection === "overview" && (
        <SystemOverviewSection
          systemOverviewPoll={systemOverviewPoll}
          onOpenProject={(projectId) => go("observability", projectId, null)}
        />
      )}
      {mode !== "system" && activeSection === "overview" && features.overview_state_v2 && (
        <ProjectOverviewSection
          jobsStatus={jobsStatus}
          jobsSummaryPoll={jobsSummaryPoll}
          callsStatus={callsStatus}
          callsPagePoll={callsPagePoll}
          settingsStatus={settingsStatus}
          settingsPoll={settingsPoll}
          onJobStatusFilter={(status) => {
            setJobStatus(status);
            setJobSearch("");
            setJobProject("");
            setJobWorkflow("");
            setJobFrom("");
            setJobTo("");
            setSelectedJobId("");
            setJobPage(1);
            openSection("jobs", {
              source: "overview",
              job_status: status,
              job_search: null,
              job_project: null,
              job_workflow: null,
              job_from: null,
              job_to: null,
              job_page: null,
              job_id: null,
            });
          }}
          onViewAllJobs={() => openSection("jobs")}
          onSelectRecentJob={(job) => {
            setJobStatus("");
            setJobSearch("");
            setJobProject("");
            setJobWorkflow("");
            setJobFrom("");
            setJobTo("");
            setJobPage(1);
            openSection("jobs", {
              source: "overview",
              job_id: job.id,
              job_status: null,
              job_search: null,
              job_project: null,
              job_workflow: null,
              job_from: null,
              job_to: null,
              job_page: null,
            });
            setSelectedJob(job);
            setSelectedJobId(job.id);
          }}
          onViewAllCalls={() => {
            setCallSearch("");
            setCallStatus("");
            setCallModel("");
            setCallFrom("");
            setCallTo("");
            setCallProject("");
            setCallFunction("");
            setCallSort("desc");
            setCallIds("");
            setCallPage(1);
            openSection("calls", {
              source: "overview",
              call_category: "business",
              call_search: null,
              call_status: null,
              call_model: null,
              call_from: null,
              call_to: null,
              call_project: null,
              call_function: null,
              call_sort: null,
              call_ids: null,
              call_page: null,
            });
          }}
          onSelectCallGroup={(group) => {
            setCallSearch("");
            setCallStatus("");
            setCallModel("");
            setCallFrom("");
            setCallTo("");
            setCallProject(group.project_id || "");
            setCallFunction("");
            setCallSort("desc");
            setCallIds(group.call_ids.join(","));
            setCallPage(1);
            openSection("calls", {
              source: "overview",
              call_category: "business",
              call_search: null,
              call_status: null,
              call_model: null,
              call_from: null,
              call_to: null,
              call_project: group.project_id || null,
              call_function: null,
              call_sort: null,
              call_ids: group.call_ids.join(","),
              call_page: null,
            });
          }}
          onManageSettings={() => openSection("settings")}
        />
      )}
      {activeSection === "jobs" && (
        <JobsSection
          jobsQueryV2={features.jobs_query_v2}
          callDetailV2={features.call_detail_v2}
          projectId={projectId}
          projectName={projectName}
          jobSearch={jobSearch} setJobSearch={setJobSearch}
          jobStatus={jobStatus} setJobStatus={setJobStatus}
          jobProject={jobProject} setJobProject={setJobProject}
          jobWorkflow={jobWorkflow} setJobWorkflow={setJobWorkflow}
          jobFrom={jobFrom} setJobFrom={setJobFrom}
          jobTo={jobTo} setJobTo={setJobTo}
          jobSort={jobSort} setJobSort={setJobSort}
          jobPage={jobPage} setJobPage={setJobPage}
          jobPageSize={jobPageSize} setJobPageSize={setJobPageSize}
          jobFilterCount={jobFilterCount}
          jobTimeInvalid={jobTimeInvalid}
          jobsPagePoll={jobsPagePoll}
          refreshingSection={refreshingSection}
          onRefresh={() => void refreshJobs()}
          onOpenTrace={openTrace}
          onSelectJob={(job) => {
            setSelectedJob(job);
            setSelectedJobId(job.id);
            setObjectLoadError("");
            writeQuery({ job_id: job.id });
          }}
        />
      )}
      {activeSection === "calls" && (
        <CallsSection
          callDetailV2={features.call_detail_v2}
          projectId={projectId}
          projectName={projectName}
          callSearch={callSearch} setCallSearch={setCallSearch}
          callStatus={callStatus} setCallStatus={setCallStatus}
          callModel={callModel} setCallModel={setCallModel}
          callProject={callProject} setCallProject={setCallProject}
          callFunction={callFunction} setCallFunction={setCallFunction}
          callFrom={callFrom} setCallFrom={setCallFrom}
          callTo={callTo} setCallTo={setCallTo}
          callSort={callSort} setCallSort={setCallSort}
          callIds={callIds} setCallIds={setCallIds}
          callPage={callPage} setCallPage={setCallPage}
          callPageSize={callPageSize} setCallPageSize={setCallPageSize}
          callFilterCount={callFilterCount}
          callTimeInvalid={callTimeInvalid}
          callsStatus={callsStatus}
          callsPagePoll={callsPagePoll}
          refreshingSection={refreshingSection}
          onRefresh={() => void refreshCalls()}
          onSelectCall={(call) => {
            setSelectedCall(call);
            setSelectedCallId(call.id);
            setObjectLoadError("");
            writeQuery({ call_id: String(call.id) });
          }}
        />
      )}
      {activeSection === "models" && (
        <DataBoundary
          status={healthStatus === "ready-data" ? catalogStatus : healthStatus}
          error={healthPoll.error || catalogPoll.error}
          onRetry={() =>
            void Promise.all([healthPoll.refresh(), catalogPoll.refresh()])
          }
          emptyLabel="模型库为空"
        >
          <ModelCenter
            health={healthPoll.data}
            catalog={catalogPoll.data}
            settings={settingsPoll.data}
            refreshHealth={healthPoll.refresh}
            refreshCatalog={catalogPoll.refresh}
            refreshSettings={settingsPoll.refresh}
            toast={toast}
          />
        </DataBoundary>
      )}
      {activeSection === "settings" && (
        <SettingsPanel
          state={settingsPoll.data}
          loading={settingsPoll.loading}
          error={settingsPoll.error}
          refresh={settingsPoll.refresh}
          toast={toast}
          registerGuard={registerNavigationGuard}
          editable={features.settings_edit_v2}
        />
      )}
      {selectedCall && features.call_detail_v2 && (
        <CallDrawer
          call={selectedCall}
          projectId={projectId}
          onClose={() => {
            setSelectedCall(null);
            setSelectedCallId(0);
            writeQuery({ call_id: null }, false);
          }}
        />
      )}
      {selectedJob && (
        <JobDrawer
          job={selectedJob}
          projectId={projectId}
          onClose={() => {
            setSelectedJob(null);
            setSelectedJobId("");
            writeQuery({ job_id: null }, false);
          }}
          onChanged={() =>
            void Promise.all([
              jobsPagePoll.refresh(),
              jobsSummaryPoll.refresh(),
              api
                .getJobDetail(selectedJob.id, "auto", projectId)
                .then((item) => setSelectedJob(item as unknown as Job)),
            ])
          }
          onJumpToRun={(runId) => {
            setSelectedJobId(runId);
            writeQuery({ job_id: runId });
          }}
        />
      )}
      {traceTarget && projectId && (
        <TraceDrawer
          projectId={projectId}
          target={traceTarget}
          onClose={() => setTraceTarget(null)}
        />
      )}
    </div>
  );
}
