import type { Dispatch, SetStateAction } from "react";
import type { Job, JobsPage } from "../../api";
import type { usePoll } from "../../App";
import type { TraceTarget } from "../../components/observability/TraceDrawer";
import SearchField from "../../components/SearchField";
import {
  JOB_STATUS_LABELS,
  WORKFLOW_LABELS,
  DataBoundary,
  Pagination,
  blockStatus,
  fmtTime,
  jobBusinessLabel,
  jobNextStep,
  jobStatusLabel,
  jobWorkLabel,
  nowQuery,
  stampClass,
  writeQuery,
} from "./shared";

/** 任务队列——从 MonitorPage 的 activeSection==="jobs" 分支里拆出来，纯展示 +
 *  转发筛选/分页事件；筛选状态、轮询与 URL 同步仍由 MonitorPage 持有（block-strip
 *  与「任务」导航角标不论当前 activeSection 是什么都要显示，必须在壳层轮询）。 */
export default function JobsSection({
  jobsQueryV2,
  callDetailV2,
  projectId,
  projectName,
  jobSearch, setJobSearch,
  jobStatus, setJobStatus,
  jobProject, setJobProject,
  jobWorkflow, setJobWorkflow,
  jobFrom, setJobFrom,
  jobTo, setJobTo,
  jobSort, setJobSort,
  jobPage, setJobPage,
  jobPageSize, setJobPageSize,
  jobFilterCount,
  jobTimeInvalid,
  jobsPagePoll,
  refreshingSection,
  onRefresh,
  onOpenTrace,
  onSelectJob,
}: {
  jobsQueryV2: boolean;
  callDetailV2: boolean;
  projectId?: string;
  projectName?: string;
  jobSearch: string; setJobSearch: Dispatch<SetStateAction<string>>;
  jobStatus: string; setJobStatus: Dispatch<SetStateAction<string>>;
  jobProject: string; setJobProject: Dispatch<SetStateAction<string>>;
  jobWorkflow: string; setJobWorkflow: Dispatch<SetStateAction<string>>;
  jobFrom: string; setJobFrom: Dispatch<SetStateAction<string>>;
  jobTo: string; setJobTo: Dispatch<SetStateAction<string>>;
  jobSort: string; setJobSort: Dispatch<SetStateAction<string>>;
  jobPage: number; setJobPage: Dispatch<SetStateAction<number>>;
  jobPageSize: number; setJobPageSize: Dispatch<SetStateAction<number>>;
  jobFilterCount: number;
  jobTimeInvalid: boolean;
  jobsPagePoll: ReturnType<typeof usePoll<JobsPage>>;
  refreshingSection: string;
  onRefresh: () => void;
  onOpenTrace: (target: TraceTarget) => void;
  onSelectJob: (job: Job) => void;
}) {
  if (!jobsQueryV2) {
    return (
      <section
        className="card monitor-section monitor-state stale"
        role="status"
      >
        全量任务查询已由独立发布开关停用；页面不会把旧的有限数据伪装成全量结果。
      </section>
    );
  }
  return (
    <section className="card monitor-section">
      <div className="monitor-section-head compact">
        <div>
          <span className="eyebrow">任务队列</span>
          <h2>任务队列</h2>
        </div>
        <div className="monitor-section-actions">
          <p>
            {nowQuery().get("source") === "overview"
              ? "来自总览 · 已清除冲突筛选"
              : "数据按需加载，不会自动刷新"}
          </p>
          <button
            type="button"
            className="monitor-refresh"
            disabled={refreshingSection === "jobs"}
            onClick={onRefresh}
          >
            <span aria-hidden="true">↻</span>
            {refreshingSection === "jobs" ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>
      <div className="monitor-toolbar">
        <label className="monitor-search">
          <span>搜索</span>
          <SearchField
            value={jobSearch}
            placeholder="搜索项目、集数、镜号或错误"
            ariaLabel="搜索任务"
            onChange={(value) => {
              setJobSearch(value);
              setJobPage(1);
              writeQuery(
                { job_search: value || null, job_page: null },
                false,
              );
            }}
          />
        </label>
        <label>
          <span>状态</span>
          <select
            aria-label="按任务状态筛选"
            value={jobStatus}
            onChange={(e) => {
              setJobStatus(e.target.value);
              setJobPage(1);
              writeQuery(
                { job_status: e.target.value || null, job_page: null },
                false,
              );
            }}
          >
            <option value="">全部状态</option>
            <option value="running,queued,recovering">
              正在运行（合并）
            </option>
            <option value="waiting_human,paused_external">
              待我处理（合并）
            </option>
            <option value="failed,partial">系统异常（合并）</option>
            {Object.entries(JOB_STATUS_LABELS).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        {projectId ? (
          <div className="monitor-scope-lock" role="status"><span>数据范围</span><b>{projectName || "当前项目"}</b></div>
        ) : (
          <label>
            <span>指定项目（高级筛选）</span>
            <input
              aria-label="按项目技术标识精确筛选任务"
              value={jobProject}
              placeholder="输入项目技术标识（可选）"
              onChange={(e) => {
                setJobProject(e.target.value);
                setJobPage(1);
                writeQuery({
                  job_project: e.target.value || null,
                  job_page: null,
                });
              }}
            />
          </label>
        )}
        <label>
          <span>工作流</span>
          <select
            aria-label="按工作流筛选任务"
            value={jobWorkflow}
            onChange={(e) => {
              setJobWorkflow(e.target.value);
              setJobPage(1);
              writeQuery({
                job_workflow: e.target.value || null,
                job_page: null,
              });
            }}
          >
            <option value="">全部</option>
            {Object.entries(WORKFLOW_LABELS).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>开始时间</span>
          <input
            type="datetime-local"
            aria-label="任务开始时间下限"
            value={jobFrom}
            max={jobTo || undefined}
            aria-invalid={jobTimeInvalid}
            onChange={(e) => {
              setJobFrom(e.target.value);
              setJobPage(1);
              writeQuery({
                job_from: e.target.value || null,
                job_page: null,
              });
            }}
          />
        </label>
        <label>
          <span>结束时间</span>
          <input
            type="datetime-local"
            aria-label="任务结束时间上限"
            value={jobTo}
            min={jobFrom || undefined}
            aria-invalid={jobTimeInvalid}
            onChange={(e) => {
              setJobTo(e.target.value);
              setJobPage(1);
              writeQuery({
                job_to: e.target.value || null,
                job_page: null,
              });
            }}
          />
        </label>
        <label>
          <span>排序</span>
          <select
            aria-label="任务排序方式"
            value={jobSort}
            onChange={(e) => {
              setJobSort(e.target.value);
              setJobPage(1);
              writeQuery({ job_sort: e.target.value, job_page: null });
            }}
          >
            <option value="desc">最新优先</option>
            <option value="asc">最早优先</option>
          </select>
        </label>
        <button
          type="button"
          className="monitor-clear"
          disabled={jobFilterCount === 0}
          aria-label={
            jobFilterCount
              ? `清除 ${jobFilterCount} 项任务筛选`
              : "当前没有任务筛选可清除"
          }
          onClick={() => {
            setJobSearch("");
            setJobStatus("");
            setJobProject("");
            setJobWorkflow("");
            setJobFrom("");
            setJobTo("");
            setJobSort("desc");
            setJobPage(1);
            writeQuery(
              {
                job_search: null,
                job_status: null,
                job_project: null,
                job_workflow: null,
                job_from: null,
                job_to: null,
                job_sort: null,
                job_page: null,
                source: null,
              },
              false,
            );
          }}
        >
          {jobFilterCount ? `清除筛选（${jobFilterCount}）` : "清除筛选"}
        </button>
      </div>
      {jobTimeInvalid && (
        <p className="monitor-filter-error" role="alert">
          开始时间不能晚于结束时间，请调整时间范围。
        </p>
      )}
      <DataBoundary
        status={blockStatus(
          jobsPagePoll.loading,
          jobsPagePoll.error,
          jobsPagePoll.data,
          !!jobsPagePoll.data && jobsPagePoll.data.total === 0,
        )}
        error={jobsPagePoll.error}
        updatedAt={jobsPagePoll.data?.server_time}
        onRetry={() => void jobsPagePoll.refresh()}
        emptyLabel="当前筛选下没有任务，可清除筛选重试"
      >
        <div className="monitor-table-wrap">
          <table className="ledger monitor-ledger jobs-ledger">
            <thead>
              <tr>
                <th>更新时间</th>
                <th>项目</th>
                <th>工作项</th>
                <th>状态</th>
                <th>影响与下一步</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {jobsPagePoll.data?.items.map((job) => (
                <tr key={`${job.source}-${job.id}`}>
                  <td className="mono">{fmtTime(job.updated_at)}</td>
                  <td>{job.project_name || "上下文未关联"}</td>
                  <td>
                    <button
                      type="button"
                      className="monitor-name-button"
                      aria-haspopup="dialog"
                      onClick={() =>
                        onOpenTrace({
                          type: "jobs",
                          id: job.id,
                          title: jobWorkLabel(job),
                          source: job.source,
                        })
                      }
                    >
                      {jobWorkLabel(job)}
                    </button>
                  </td>
                  <td>
                    <span className={`stamp ${stampClass(job.status)}`}>
                      {jobStatusLabel(job.status)}
                    </span>
                  </td>
                  <td className="monitor-error-cell">
                    <span>{jobNextStep(job)}</span>
                    {job.error && (
                      <details className="monitor-error-details">
                        <summary
                          aria-label={`查看${jobBusinessLabel(job)}的错误详情`}
                        >
                          错误详情
                        </summary>
                        <pre>{job.error}</pre>
                      </details>
                    )}
                  </td>
                  <td>
                    <button
                      className="btn small"
                      disabled={!callDetailV2}
                      onClick={() => onSelectJob(job)}
                      aria-label={callDetailV2
                        ? `查看${jobBusinessLabel(job)}详情`
                        : `查看${jobBusinessLabel(job)}详情，暂不可用：任务详情功能已停用`}
                    >
                      详情 / 处理
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DataBoundary>
      {jobsPagePoll.data && (
        <Pagination
          page={jobPage}
          pageSize={jobPageSize}
          total={jobsPagePoll.data.total}
          pageCount={jobsPagePoll.data.page_count}
          onPage={(value) => {
            setJobPage(value);
            writeQuery({ job_page: String(value) }, false);
          }}
          onPageSize={(value) => {
            setJobPageSize(value);
            setJobPage(1);
            writeQuery({ job_page_size: String(value), job_page: null });
          }}
        />
      )}
    </section>
  );
}
