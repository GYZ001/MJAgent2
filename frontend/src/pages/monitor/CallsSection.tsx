import type { Dispatch, SetStateAction } from "react";
import type { Call, CallsPage } from "../../api";
import type { usePoll } from "../../App";
import SearchField from "../../components/SearchField";
import {
  CALL_STATUS_LABELS,
  DataBoundary,
  Pagination,
  blockStatus,
  callBusinessLabel,
  callNextStep,
  callPurpose,
  callStatusLabel,
  fmtTime,
  nowQuery,
  stampClass,
  writeQuery,
} from "./shared";

/** 调用日志——从 MonitorPage 的 activeSection==="calls" 分支里拆出来，纯展示 +
 *  转发筛选/分页事件；筛选状态、轮询与 URL 同步仍由 MonitorPage 持有（block-strip
 *  不论当前 activeSection 是什么都要显示「调用」状态，必须在壳层轮询）。 */
export default function CallsSection({
  callDetailV2,
  projectId,
  projectName,
  callSearch, setCallSearch,
  callStatus, setCallStatus,
  callModel, setCallModel,
  callProject, setCallProject,
  callFunction, setCallFunction,
  callFrom, setCallFrom,
  callTo, setCallTo,
  callSort, setCallSort,
  callIds, setCallIds,
  callPage, setCallPage,
  callPageSize, setCallPageSize,
  callFilterCount,
  callTimeInvalid,
  callsStatus,
  callsPagePoll,
  refreshingSection,
  onRefresh,
  onSelectCall,
}: {
  callDetailV2: boolean;
  projectId?: string;
  projectName?: string;
  callSearch: string; setCallSearch: Dispatch<SetStateAction<string>>;
  callStatus: string; setCallStatus: Dispatch<SetStateAction<string>>;
  callModel: string; setCallModel: Dispatch<SetStateAction<string>>;
  callProject: string; setCallProject: Dispatch<SetStateAction<string>>;
  callFunction: string; setCallFunction: Dispatch<SetStateAction<string>>;
  callFrom: string; setCallFrom: Dispatch<SetStateAction<string>>;
  callTo: string; setCallTo: Dispatch<SetStateAction<string>>;
  callSort: string; setCallSort: Dispatch<SetStateAction<string>>;
  callIds: string; setCallIds: Dispatch<SetStateAction<string>>;
  callPage: number; setCallPage: Dispatch<SetStateAction<number>>;
  callPageSize: number; setCallPageSize: Dispatch<SetStateAction<number>>;
  callFilterCount: number;
  callTimeInvalid: boolean;
  callsStatus: ReturnType<typeof blockStatus>;
  callsPagePoll: ReturnType<typeof usePoll<CallsPage>>;
  refreshingSection: string;
  onRefresh: () => void;
  onSelectCall: (call: Call) => void;
}) {
  return (
    <section className="card monitor-section">
      <div className="monitor-section-head compact">
        <div>
          <span className="eyebrow">模型调用</span>
          <h2>调用日志</h2>
        </div>
        <div className="monitor-section-actions">
          <p>
            {nowQuery().get("source") === "overview"
              ? "来自总览 · 与异常聚合共享口径"
              : "数据按需加载，不会自动刷新"}
          </p>
          <button
            type="button"
            className="monitor-refresh"
            disabled={refreshingSection === "calls"}
            onClick={onRefresh}
          >
            <span aria-hidden="true">↻</span>
            {refreshingSection === "calls" ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>
      <div className="monitor-toolbar">
        <label className="monitor-search">
          <span>搜索</span>
          <SearchField
            value={callSearch}
            placeholder="搜索功能、模型、接口状态或错误"
            ariaLabel="搜索调用日志"
            onChange={(value) => {
              setCallSearch(value);
              setCallIds("");
              setCallPage(1);
              writeQuery(
                {
                  call_search: value || null,
                  call_ids: null,
                  call_page: null,
                },
                false,
              );
            }}
          />
        </label>
        <label>
          <span>状态</span>
          <select
            aria-label="按调用状态筛选"
            value={callStatus}
            onChange={(e) => {
              setCallStatus(e.target.value);
              setCallIds("");
              setCallPage(1);
              writeQuery({
                call_status: e.target.value || null,
                call_ids: null,
                call_page: null,
              });
            }}
          >
            <option value="">全部状态</option>
            {Object.entries(CALL_STATUS_LABELS).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>指定模型（高级筛选）</span>
          <input
            aria-label="按模型技术标识精确筛选调用"
            value={callModel}
            placeholder="输入模型技术标识（可选）"
            onChange={(e) => {
              setCallModel(e.target.value);
              setCallIds("");
              setCallPage(1);
              writeQuery({
                call_model: e.target.value || null,
                call_ids: null,
                call_page: null,
              });
            }}
          />
        </label>
        {projectId ? (
          <div className="monitor-scope-lock" role="status"><span>数据范围</span><b>{projectName || "当前项目"}</b></div>
        ) : (
          <label>
            <span>指定项目（高级筛选）</span>
            <input
              aria-label="按项目技术标识精确筛选调用"
              value={callProject}
              placeholder="输入项目技术标识（可选）"
              onChange={(e) => {
                setCallProject(e.target.value);
                setCallIds("");
                setCallPage(1);
                writeQuery({
                  call_project: e.target.value || null,
                  call_ids: null,
                  call_page: null,
                });
              }}
            />
          </label>
        )}
        <label>
          <span>指定功能（高级筛选）</span>
          <input
            aria-label="按功能技术标识精确筛选调用"
            value={callFunction}
            placeholder="输入功能技术标识（可选）"
            onChange={(e) => {
              setCallFunction(e.target.value);
              setCallIds("");
              setCallPage(1);
              writeQuery({
                call_function: e.target.value || null,
                call_ids: null,
                call_page: null,
              });
            }}
          />
        </label>
        <label>
          <span>开始时间</span>
          <input
            type="datetime-local"
            aria-label="调用开始时间下限"
            value={callFrom}
            max={callTo || undefined}
            aria-invalid={callTimeInvalid}
            onChange={(e) => {
              setCallFrom(e.target.value);
              setCallIds("");
              setCallPage(1);
              writeQuery({
                call_from: e.target.value || null,
                call_ids: null,
                call_page: null,
              });
            }}
          />
        </label>
        <label>
          <span>结束时间</span>
          <input
            type="datetime-local"
            aria-label="调用结束时间上限"
            value={callTo}
            min={callFrom || undefined}
            aria-invalid={callTimeInvalid}
            onChange={(e) => {
              setCallTo(e.target.value);
              setCallIds("");
              setCallPage(1);
              writeQuery({
                call_to: e.target.value || null,
                call_ids: null,
                call_page: null,
              });
            }}
          />
        </label>
        <label>
          <span>排序</span>
          <select
            aria-label="调用排序方式"
            value={callSort}
            onChange={(e) => {
              setCallSort(e.target.value);
              setCallPage(1);
              writeQuery({ call_sort: e.target.value, call_page: null });
            }}
          >
            <option value="desc">最新优先</option>
            <option value="asc">最早优先</option>
          </select>
        </label>
        <button
          type="button"
          className="monitor-clear"
          disabled={callFilterCount === 0}
          aria-label={
            callFilterCount
              ? `清除 ${callFilterCount} 项调用筛选`
              : "当前没有调用筛选可清除"
          }
          onClick={() => {
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
            writeQuery(
              {
                call_search: null,
                call_status: null,
                call_category: null,
                call_model: null,
                call_from: null,
                call_to: null,
                call_project: null,
                call_function: null,
                call_sort: null,
                call_ids: null,
                call_page: null,
                source: null,
              },
              false,
            );
          }}
        >
          {callFilterCount ? `清除筛选（${callFilterCount}）` : "清除筛选"}
        </button>
      </div>
      {callTimeInvalid && (
        <p className="monitor-filter-error" role="alert">
          开始时间不能晚于结束时间，请调整时间范围。
        </p>
      )}
      <DataBoundary
        status={callsStatus}
        error={callsPagePoll.error}
        updatedAt={callsPagePoll.data?.server_time}
        onRetry={() => void callsPagePoll.refresh()}
        emptyLabel="当前筛选下没有调用记录"
      >
        <div className="monitor-table-wrap">
          <table className="ledger monitor-ledger calls-ledger">
            <thead>
              <tr>
                <th>时间</th>
                <th>调用目的</th>
                <th>模型</th>
                <th>状态</th>
                <th>接口状态码</th>
                <th>延迟</th>
                <th>查看内容</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {callsPagePoll.data?.items.map((call) => (
                <tr key={call.id}>
                  <td className="mono">{fmtTime(call.ts)}</td>
                  <td>
                    <button
                      type="button"
                      className="monitor-name-button"
                      aria-haspopup="dialog"
                      onClick={() => onSelectCall(call)}
                    >
                      {callPurpose(call)}
                    </button>
                  </td>
                  <td>
                    {call.model_label || call.model || "未记录模型"}
                    <details>
                      <summary
                        aria-label={`查看${call.model_label || "当前模型"}的技术标识`}
                      >
                        技术标识
                      </summary>
                      <code>{call.model}</code>
                    </details>
                  </td>
                  <td>
                    <span
                      className={`stamp ${stampClass(call.effective_status)}`}
                    >
                      {callStatusLabel(call.effective_status)}
                    </span>
                  </td>
                  <td>
                    {call.http_status
                      ? `状态码 ${call.http_status}`
                      : "未返回"}
                  </td>
                  <td>{(call.latency_ms / 1000).toFixed(1)} 秒</td>
                  <td className="monitor-error-cell">
                    <span>{callNextStep(call)}</span>
                  </td>
                  <td>
                    <button
                      className="btn small"
                      disabled={!callDetailV2}
                      onClick={() => onSelectCall(call)}
                      aria-label={callDetailV2
                        ? `查看${callBusinessLabel(call)}的${projectId ? "完整原始" : "脱敏"}详情`
                        : `查看${callBusinessLabel(call)}详情，暂不可用：调用详情功能已停用`}
                    >
                      {callDetailV2 ? "查看详情" : "详情已停用"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DataBoundary>
      {callsPagePoll.data && (
        <Pagination
          page={callPage}
          pageSize={callPageSize}
          total={callsPagePoll.data.total}
          pageCount={callsPagePoll.data.page_count}
          onPage={(value) => {
            setCallPage(value);
            writeQuery({ call_page: String(value) }, false);
          }}
          onPageSize={(value) => {
            setCallPageSize(value);
            setCallPage(1);
            writeQuery({ call_page_size: String(value), call_page: null });
          }}
        />
      )}
    </section>
  );
}
