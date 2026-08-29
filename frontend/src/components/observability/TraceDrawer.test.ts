import { describe, expect, it } from "vitest";

import {
  traceDisplayNames,
  traceInitialExpandedIds,
  traceNodeOrder,
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

  it("默认展开总任务和全部业务环节", () => {
    const nodes = [
      node("run:main", null, "task"),
      node("step:one", "run:main", "business_stage"),
      node("call:1", "step:one", "model_processing"),
      node("step:two", "run:main", "business_stage"),
      node("call:2", "step:two", "model_processing"),
    ];

    expect([...traceInitialExpandedIds(nodes, "run:main")].sort()).toEqual([
      "run:main",
      "step:one",
      "step:two",
    ]);
    expect([...traceInitialExpandedIds(nodes, "call:2")].sort()).toEqual([
      "run:main",
      "step:one",
      "step:two",
    ]);
  });

  it("同一层节点按真实开始时间排序，无时间节点放在最后", () => {
    const nodes = [
      { ...node("step:late", "run:main", "business_stage"), started_at: 20 },
      { ...node("step:unknown", "run:main", "business_stage"), started_at: null },
      { ...node("step:early", "run:main", "business_stage"), started_at: 10 },
    ];

    expect(nodes.sort(traceNodeOrder).map((item) => item.id)).toEqual([
      "step:early",
      "step:late",
      "step:unknown",
    ]);
  });

  it("业务流程和逐镜任务优先按后端业务序号排序", () => {
    const nodes = [
      { ...node("stage:quality", "run:main", "business_stage"), sequence: 6, started_at: 10 },
      { ...node("stage:plan", "run:main", "business_stage"), sequence: 2, started_at: 30 },
      { ...node("stage:authorization", "run:main", "business_stage"), sequence: 1, started_at: 40 },
    ];

    expect(nodes.sort(traceNodeOrder).map((item) => item.id)).toEqual([
      "stage:authorization",
      "stage:plan",
      "stage:quality",
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
      {
        ...node("call:3", "step:discovery"),
        name: "internal_projection",
      },
      {
        ...node("call:4", null),
        name: "unregistered_business_step",
      },
      {
        ...node("call:5", "step:discovery", "model_processing"),
        name: "逐场撰写剧本（场次分片 SS003，共 8 片）",
      },
    ];

    const names = traceDisplayNames(nodes);
    expect(names.get("run:main")).toBe("生成映射包");
    expect(names.get("step:discovery")).toBe("识别本集角色");
    expect(names.get("call:1")).toBe("为“识别本集角色”生成业务内容");
    expect(names.get("call:2")).toBe("记录结构校验指标");
    expect(names.get("call:3")).toBe("处理“识别本集角色”相关数据");
    expect(names.get("call:4")).toBe("业务名称待配置（unregistered_business_step）");
    expect(names.get("call:5")).toBe("逐场撰写剧本（场次分片 SS003，共 8 片）");
    expect(traceNodeRole(nodes[2])).toBe("model_processing");
    expect(traceNodeRole(nodes[3])).toBe("program_processing");
  });
});
