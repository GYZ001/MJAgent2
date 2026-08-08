import { describe, expect, it } from "vitest";

import {
  traceInitialExpandedIds,
  traceNodeSummaries,
  traceRoots,
  type TraceNode,
} from "./TraceDrawer";

function node(
  id: string,
  parentId: string | null,
  role: TraceNode["node_role"] = "program_processing",
): TraceNode {
  return {
    id,
    parent_id: parentId,
    kind: id.split(":")[0] as TraceNode["kind"],
    node_role: role,
    name: id,
    subtitle: "",
    status: "SUCCEEDED",
    latency_ms: 1,
  };
}

describe("调用树根节点识别", () => {
  it("保留主运行并把父节点缺失的兼容记录作为独立根节点", () => {
    const nodes = [
      node("run:main", null, "task"),
      node("step:generate", "run:main", "business_stage"),
      node("call:1", "step:generate", "model_processing"),
      node("job:legacy", "run:missing"),
    ];

    expect(traceRoots(nodes).map((item) => item.id)).toEqual([
      "run:main",
      "job:legacy",
    ]);
  });

  it("默认展开总任务和当前节点路径，其余业务环节保持合并", () => {
    const nodes = [
      node("run:main", null, "task"),
      node("step:one", "run:main", "business_stage"),
      node("call:1", "step:one", "model_processing"),
      node("step:two", "run:main", "business_stage"),
      node("call:2", "step:two", "model_processing"),
    ];

    expect([...traceInitialExpandedIds(nodes, "run:main")]).toEqual(["run:main"]);
    expect([...traceInitialExpandedIds(nodes, "call:2")].sort()).toEqual([
      "run:main",
      "step:two",
    ]);
  });

  it("业务环节摘要合并统计全部后代中的模型与程序处理", () => {
    const nodes = [
      node("run:main", null, "task"),
      node("step:generate", "run:main", "business_stage"),
      node("step:iteration", "step:generate"),
      node("call:1", "step:iteration", "model_processing"),
      node("call:2", "step:generate", "program_processing"),
    ];

    expect(traceNodeSummaries(nodes).get("step:generate")).toEqual({
      total: 3,
      stages: 0,
      models: 1,
      programs: 2,
    });
  });
});
