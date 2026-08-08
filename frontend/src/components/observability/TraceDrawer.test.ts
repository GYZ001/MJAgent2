import { describe, expect, it } from "vitest";

import { traceRoots, type TraceNode } from "./TraceDrawer";

function node(id: string, parentId: string | null): TraceNode {
  return {
    id,
    parent_id: parentId,
    kind: id.split(":")[0] as TraceNode["kind"],
    name: id,
    subtitle: "",
    status: "SUCCEEDED",
    latency_ms: 1,
  };
}

describe("调用树根节点识别", () => {
  it("保留主运行并把父节点缺失的兼容记录作为独立根节点", () => {
    const nodes = [
      node("run:main", null),
      node("step:generate", "run:main"),
      node("call:1", "step:generate"),
      node("job:legacy", "run:missing"),
    ];

    expect(traceRoots(nodes).map((item) => item.id)).toEqual([
      "run:main",
      "job:legacy",
    ]);
  });
});
