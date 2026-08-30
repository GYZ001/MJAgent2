import { get } from "../client";

/** 观测台链路视图（TraceDrawer）：project 下按 target 类型/id 查询一条链路，
 *  以及链路内某个节点的输入/输出详情。响应形状（TraceView/TraceNodeDetail）
 *  页面自己的展示逻辑较重，仍留在 components/observability/TraceDrawer.tsx
 *  里定义类型；这里只收敛 URL 知识。 */
export function getTraceView(
  projectId: string,
  targetType: string,
  targetId: string,
  sourceQuery: string,
): Promise<unknown> {
  return get(
    `/projects/${encodeURIComponent(projectId)}/observability/traces/${targetType}/${encodeURIComponent(targetId)}${sourceQuery}`,
  );
}

export function getTraceNodeDetail(
  projectId: string,
  targetType: string,
  targetId: string,
  nodeId: string,
  sourceQuery: string,
): Promise<unknown> {
  return get(
    `/projects/${encodeURIComponent(projectId)}/observability/traces/${targetType}/${encodeURIComponent(targetId)}/nodes/${encodeURIComponent(nodeId)}${sourceQuery}`,
  );
}
