import { useEffect, useState } from "react";
import { ApiError, getUsageTop, type UsageResource, type UsageTopItem } from "../../api";
import { RESOURCE_LABELS, formatResourceValue } from "./resourceLabels";

const RESOURCE_OPTIONS: UsageResource[] = ["token", "video_seconds", "image", "storage_bytes"];
const DIMENSION_OPTIONS: { value: "team" | "user" | "project" | "model"; label: string }[] = [
  { value: "team", label: "团队" },
  { value: "user", label: "用户" },
  { value: "project", label: "项目" },
  { value: "model", label: "模型" },
];

function keyOf(item: UsageTopItem): string {
  return item.team_id || item.user_id || item.project_id || item.model || "-";
}

function totalOf(item: UsageTopItem, resource: UsageResource): number {
  return resource === "storage_bytes" ? (item.bytes_total ?? 0) : item.total;
}

/** 用量排行榜：按团队/用户/项目/模型看谁用得最多（EP-04 §7）。storage_bytes
 *  目前只支持 dimension=project（见 app/quota_policy/usage_query.py 文档），
 *  切到该资源时自动把维度收窄，避免用户选出一个后端会 422 的组合。 */
export default function TopRankingSection() {
  const [resource, setResource] = useState<UsageResource>("token");
  const [dimension, setDimension] = useState<"team" | "user" | "project" | "model">("project");
  const [items, setItems] = useState<UsageTopItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const effectiveDimension = resource === "storage_bytes" ? "project" : dimension;

  useEffect(() => {
    setItems(null);
    getUsageTop(effectiveDimension, resource, 15)
      .then((data) => setItems(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : "加载失败"));
  }, [resource, effectiveDimension]);

  const maxTotal = Math.max(1, ...(items || []).map((item) => totalOf(item, resource)));

  return (
    <section className="card">
      <div className="card-heading-row"><h3>用量排行</h3></div>
      <div className="resource-admin-toolbar">
        <label>
          资源
          <select value={resource} onChange={(e) => setResource(e.target.value as UsageResource)}>
            {RESOURCE_OPTIONS.map((r) => <option key={r} value={r}>{RESOURCE_LABELS[r]}</option>)}
          </select>
        </label>
        <label>
          维度
          <select
            value={effectiveDimension} disabled={resource === "storage_bytes"}
            onChange={(e) => setDimension(e.target.value as typeof dimension)}
          >
            {DIMENSION_OPTIONS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
          </select>
        </label>
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
      {items === null && !error && <p className="hint">正在加载…</p>}
      {items?.length === 0 && <p className="hint">这个组合暂时没有用量数据。</p>}
      <div className="resource-top-list">
        {items?.map((item) => {
          const total = totalOf(item, resource);
          return (
            <div className="resource-top-row" key={keyOf(item)}>
              <span className="resource-top-key" title={keyOf(item)}>{keyOf(item)}</span>
              <div className="resource-top-bar">
                <div className="resource-top-bar-fill" style={{ width: `${(total / maxTotal) * 100}%` }} />
              </div>
              <span className="resource-top-total">{formatResourceValue(resource, total)}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
