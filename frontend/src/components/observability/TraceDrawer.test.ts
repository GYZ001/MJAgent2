import { describe, expect, it } from "vitest";

import {
  traceDisplayNames,
  traceInitialExpandedIds,
  traceNodeSummaries,
  traceNodeRole,
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

  it("兼容旧后端返回时也不在左树暴露技术英文或笼统模型名称", () => {
    const nodes = [
      { ...node("run:main", null, "task"), name: "screenplay" },
      {
        ...node("step:discovery", "run:main", undefined),
        node_role: undefined,
        name: "character_discovery",
        subtitle: "screenplay_character_discovery",
      },
      {
        ...node("call:1", "step:discovery", undefined),
        node_role: undefined,
        name: "文本模型调用",
        subtitle: "模型调用 · d71l5c8nfdb167kligqg",
      },
      {
        ...node("call:2", "run:main", undefined),
        node_role: undefined,
        name: "val422_metric",
        subtitle: "d2a5n9rnvvm49eucvnvg",
      },
    ];

    const names = traceDisplayNames(nodes);
    expect(names.get("run:main")).toBe("生成剧本");
    expect(names.get("step:discovery")).toBe("识别剧本角色");
    expect(names.get("call:1")).toBe("为“识别剧本角色”生成业务内容");
    expect(names.get("call:2")).toBe("记录结构校验指标");
    expect(traceNodeRole(nodes[2])).toBe("model_processing");
    expect(traceNodeRole(nodes[3])).toBe("program_processing");
  });
});
