import type { CallAggregate, CallsPage, Job, JobsSummary, SettingsView, SystemOverview } from "../../api";
import type { usePoll } from "../../App";
import {
  CALL_KIND_LABELS,
  DataBoundary,
  blockStatus,
  fmtTime,
  jobStatusLabel,
  jobWorkLabel,
  stampClass,
} from "./shared";

/** 系统模式（mode==="system"）的总览——系统级项目/任务/调用汇总。 */
export function SystemOverviewSection({
  systemOverviewPoll,
  onOpenProject,
}: {
  systemOverviewPoll: ReturnType<typeof usePoll<SystemOverview>>;
  onOpenProject: (projectId: string) => void;
}) {
  return (
    <section className="card monitor-section system-overview">
      <div className="monitor-section-head">
        <div><span className="eyebrow">系统级汇总</span><h2>总览</h2></div>
        <p>只呈现聚合数字；项目运行原始数据请进入对应项目观测台。</p>
      </div>
      <DataBoundary
        status={blockStatus(systemOverviewPoll.loading, systemOverviewPoll.error, systemOverviewPoll.data, !systemOverviewPoll.data)}
        error={systemOverviewPoll.error}
        updatedAt={systemOverviewPoll.data?.server_time}
        onRetry={() => void systemOverviewPoll.refresh()}
        emptyLabel="系统暂时没有项目"
      >
        {systemOverviewPoll.data && (
          <>
            <div className="stat-row monitor-stats">
              <div className="stat-cell"><div className="s-label">项目空间</div><div className="cost-ink">{systemOverviewPoll.data.totals.projects}</div></div>
              <div className="stat-cell"><div className="s-label">任务总数</div><div className="cost-ink">{systemOverviewPoll.data.totals.jobs}</div></div>
              <div className="stat-cell"><div className="s-label">调用总数</div><div className="cost-ink">{systemOverviewPoll.data.totals.calls}</div></div>
              <div className="stat-cell"><div className="s-label">待治理未归属数据</div><div className="cost-ink">{systemOverviewPoll.data.totals.unattributed_jobs + systemOverviewPoll.data.totals.unattributed_calls}</div></div>
            </div>
            <div className="system-project-summary">
              {systemOverviewPoll.data.projects.map((project) => (
                <button type="button" key={project.id} onClick={() => onOpenProject(project.id)}>
                  <div><b>{project.name}</b><small>项目级聚合</small></div>
                  <span>活跃任务 {(project.job_counts.running || 0) + (project.job_counts.queued || 0)}</span>
                  <span>异常任务 {(project.job_counts.failed || 0) + (project.job_counts.partial || 0)}</span>
                  <span>调用 {project.call_count}</span>
                </button>
              ))}
            </div>
          </>
        )}
      </DataBoundary>
    </section>
  );
}

/** 项目/系统共用模式（mode!=="system"）的运行总览——从任务摘要派生的四个统计格
 *  + 异常待办/最近任务/系统状态三张卡片。所有点击都只是「预置筛选后跳转到对应
 *  分区」，实际筛选状态与轮询仍由 MonitorPage 持有，这里只转发回调。 */
export default function ProjectOverviewSection({
  jobsStatus,
  jobsSummaryPoll,
  callsStatus,
  callsPagePoll,
  settingsStatus,
  settingsPoll,
  onJobStatusFilter,
  onViewAllJobs,
  onSelectRecentJob,
  onViewAllCalls,
  onSelectCallGroup,
  onManageSettings,
}: {
  jobsStatus: ReturnType<typeof blockStatus>;
  jobsSummaryPoll: ReturnType<typeof usePoll<JobsSummary>>;
  callsStatus: ReturnType<typeof blockStatus>;
  callsPagePoll: ReturnType<typeof usePoll<CallsPage>>;
  settingsStatus: ReturnType<typeof blockStatus>;
  settingsPoll: ReturnType<typeof usePoll<SettingsView>>;
  onJobStatusFilter: (status: string) => void;
  onViewAllJobs: () => void;
  onSelectRecentJob: (job: Job) => void;
  onViewAllCalls: () => void;
  onSelectCallGroup: (group: CallAggregate) => void;
  onManageSettings: () => void;
}) {
  const counts = jobsSummaryPoll.data?.counts || {};
  return (
    <div className="monitor-section">
      <div className="monitor-section-head">
        <div>
          <span className="eyebrow">运行总览</span>
          <h2>制作运行总览</h2>
        </div>
        <p>正在运行、待我处理、系统异常与近期完成。</p>
      </div>
      <DataBoundary
        status={jobsStatus}
        error={jobsSummaryPoll.error}
        updatedAt={jobsSummaryPoll.data?.server_time}
        onRetry={() => void jobsSummaryPoll.refresh()}
        emptyLabel="当前确实没有制作任务"
      >
        <div className="stat-row monitor-stats">
          {[
            {
              label: "正在运行",
              count:
                (counts.running || 0) +
                (counts.queued || 0) +
                (counts.recovering || 0),
              status: "running,queued,recovering",
            },
            {
              label: "待我处理",
              count:
                (counts.waiting_human || 0) +
                (counts.paused_budget || 0) +
                (counts.paused_external || 0),
              status: "waiting_human,paused_budget,paused_external",
            },
            {
              label: "系统异常",
              count: (counts.failed || 0) + (counts.partial || 0),
              status: "failed,partial",
            },
            {
              label: "近期完成",
              count: counts.succeeded || 0,
              status: "succeeded",
            },
          ].map((item) => (
            <button
              className="stat-cell"
              key={item.label}
              onClick={() => onJobStatusFilter(item.status)}
            >
              <div className="s-label">{item.label}</div>
              <div className="cost-ink">{item.count}</div>
              <span>查看对应任务 →</span>
            </button>
          ))}
        </div>
      </DataBoundary>
      <div className="monitor-overview-grid">
        <section className="card monitor-overview-card">
          <div className="monitor-card-head">
            <div>
              <span className="eyebrow">异常待办</span>
              <h3>需要关注</h3>
            </div>
            <button onClick={onViewAllCalls}>
              查看全部
            </button>
          </div>
          <DataBoundary
            status={callsStatus}
            error={callsPagePoll.error}
            updatedAt={callsPagePoll.data?.server_time}
            onRetry={() => void callsPagePoll.refresh()}
            emptyLabel="当前确实没有业务调用记录"
          >
            {callsPagePoll.data?.aggregates.length ? (
              <div className="monitor-brief-list">
                {callsPagePoll.data.aggregates.slice(0, 5).map((group) => (
                  <button
                    key={group.key}
                    onClick={() => onSelectCallGroup(group)}
                  >
                    <span>
                      <b>
                        {CALL_KIND_LABELS[group.kind] || "其他业务异常"} ·{" "}
                        {group.project_name === "上下文未关联"
                          ? "未关联项目"
                          : group.project_name}
                      </b>
                      <small>
                        {group.episode_no
                          ? `第${group.episode_no}集`
                          : "未关联具体分集"}{" "}
                        · 首次 {fmtTime(group.first_ts)} · 最近{" "}
                        {fmtTime(group.last_ts)}
                      </small>
                    </span>
                    <span className="stamp red">
                      异常 {group.count} 次
                    </span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="monitor-ok">
                全量查询成功：当前没有需要关注的业务异常
              </div>
            )}
          </DataBoundary>
        </section>
        <section className="card monitor-overview-card">
          <div className="monitor-card-head">
            <div>
              <span className="eyebrow">最近任务</span>
              <h3>最近任务</h3>
            </div>
            <button onClick={onViewAllJobs}>查看全部</button>
          </div>
          {jobsSummaryPoll.data?.recent.slice(0, 5).map((job) => (
            <button
              className="monitor-recent-job"
              key={job.id}
              onClick={() => onSelectRecentJob(job)}
            >
              <span>
                <b>{job.project_name || "上下文未关联"}</b>
                <small>{jobWorkLabel(job)}</small>
              </span>
              <span className={`stamp ${stampClass(job.status)}`}>
                {jobStatusLabel(job.status)}
              </span>
            </button>
          ))}
        </section>
        <section className="card monitor-overview-card monitor-system-card">
          <div className="monitor-card-head">
            <div>
              <span className="eyebrow">系统状态</span>
              <h3>配置概况</h3>
            </div>
            <button onClick={onManageSettings}>
              管理设置
            </button>
          </div>
          <DataBoundary
            status={settingsStatus}
            error={settingsPoll.error}
            updatedAt={settingsPoll.data?.server_time}
            onRetry={() => void settingsPoll.refresh()}
            emptyLabel="未获得配置"
          >
            <dl>
              <div>
                <dt>配置版本</dt>
                <dd>v{settingsPoll.data?.version}</dd>
              </div>
              <div>
                <dt>上游在途上限</dt>
                <dd>{settingsPoll.data?.effective.video_inflight_limit}</dd>
              </div>
              <div>
                <dt>视频提交并发</dt>
                <dd>
                  {settingsPoll.data?.effective.video_submit_concurrency}
                </dd>
              </div>
            </dl>
          </DataBoundary>
        </section>
      </div>
    </div>
  );
}
