import { describe, expect, it } from "vitest";
import {
  blockStatus,
  callBusinessLabel,
  callNextStep,
  categorizeSettingKeys,
  jobBusinessLabel,
  jobNextStep,
  isProviderCreateUnresolved,
  modelAssignmentSettingKey,
  modelAssignmentValue,
  modelBusinessLabel,
  modelProviderOptions,
  normalizeDraft,
  PROVIDER_RESUBMISSION_WARNING,
  settingOptionLabel,
  type Call,
  type Job,
  type CatalogModel,
  type ModelSelection,
  type SettingSchema,
} from "./MonitorPage";

const integer: SettingSchema = {
  label: "视频提交并发",
  type: "integer",
  default: "4",
  min: 1,
  max: 64,
  step: 1,
  unit: "任务",
  immediate: true,
  experimental: false,
};

describe("监制房数据块状态", () => {
  it("首次加载与无快照失败不会伪装成空数据", () => {
    expect(blockStatus(true, null, null, true)).toBe("loading");
    expect(blockStatus(false, "断网", null, true)).toBe("error");
  });

  it("有成功快照后刷新失败进入 stale", () => {
    expect(blockStatus(false, "超时", { total: 3 }, false)).toBe("stale");
  });

  it("只有成功查询才可进入 ready-empty", () => {
    expect(blockStatus(false, null, { total: 0 }, true)).toBe("ready-empty");
  });
});

describe("任务队列业务名称与恢复建议", () => {
  const job = {
    id: "run_internal_123",
    source: "run",
    project_name: "斗破苍穹",
    episode_no: 1,
    shot_no: 4,
    workflow_type: "video_generation",
    status: "paused_external",
    updated_at: 0,
  } satisfies Job;

  it("主要操作名称使用项目、集镜和工作流，不暴露内部任务标识", () => {
    expect(jobBusinessLabel(job))
      .toBe("斗破苍穹 · 第1集 · 镜4 · 视频生成 · 外部中断");
    expect(jobBusinessLabel(job)).not.toContain(job.id);
  });

  it("按状态给出可执行的下一步，不把原始错误铺在列表中", () => {
    expect(jobNextStep(job)).toBe("任务被外部中断，可查看原因后恢复");
    expect(jobNextStep({ ...job, status: "failed", error: "provider_stack" }))
      .toBe("任务未完成，可查看详情后重试");
    expect(jobNextStep({ ...job, status: "succeeded" }))
      .toBe("任务已完成，无需处理");
  });

  it("create 结果不确定时先恢复旧句柄，不引导直接重试", () => {
    const unresolved = {
      ...job,
      status: "waiting_human",
      reason_code: "VIDEO_PROVIDER_CREATE_UNRESOLVED",
    };
    expect(isProviderCreateUnresolved(unresolved)).toBe(true);
    expect(isProviderCreateUnresolved({
      ...job,
      reason_code: undefined,
      error: "[VIDEO_PROVIDER_CREATE_UNRESOLVED] create 结果未知",
    })).toBe(true);
    expect(jobNextStep(unresolved)).toContain("先恢复原任务句柄");
    expect(jobNextStep(unresolved)).not.toContain("重试");
  });

  it("重新提交明确使用新 operation 和独立预算 claim", () => {
    expect(PROVIDER_RESUBMISSION_WARNING).toContain("新的 operation ID");
    expect(PROVIDER_RESUBMISSION_WARNING).toContain("独立预算 claim");
    expect(PROVIDER_RESUBMISSION_WARNING).not.toContain("复用原幂等标识");
  });

  it("恢复排队与已被接管的历史记录不能共用同一句“正在等待执行”文案", () => {
    // 修复前 queued/recovering 共用一句话；已被接管(recovered_by_run_id
    // 非空)的历史记录从未真正排队，也不会再被 worker 领取，必须与两者都区分。
    const queuedText = jobNextStep({ ...job, status: "queued" });
    const recoveringText = jobNextStep({ ...job, status: "recovering" });
    const supersededText = jobNextStep({
      ...job,
      status: "superseded",
      recovered_by_run_id: "run_child",
    });
    expect(queuedText).toBe("正在等待执行，可查看排队详情或取消任务");
    expect(recoveringText).not.toBe(queuedText);
    expect(recoveringText).toContain("服务重启");
    expect(supersededText).not.toBe(queuedText);
    expect(supersededText).not.toBe(recoveringText);
    expect(supersededText).not.toContain("等待执行");
    expect(supersededText).toContain("接管");
  });

  it("已自动续跑完成的历史记录仍提示无需处理", () => {
    expect(jobNextStep({ ...job, status: "recovered" }))
      .toBe("任务已自动续跑完成，无需处理");
  });
});

describe("调用日志业务名称与恢复建议", () => {
  const call = {
    id: 22112,
    ts: 0,
    kind: "video_create",
    status: "FAILED",
    effective_status: "FAILED",
    category: "business",
    latency_ms: 1200,
    error: "provider_stack",
    context: {
      project_name: "斗破苍穹",
      episode_no: 1,
      shot_no: 4,
    },
  } satisfies Call;

  it("详情入口只使用调用目的、模型与状态，不展示业务上下文", () => {
    expect(callBusinessLabel(call))
      .toBe("创建视频任务 · 未记录模型 · 失败");
    expect(callBusinessLabel(call)).not.toContain("斗破苍穹");
    expect(callBusinessLabel(call)).not.toContain("第1集");
    expect(callBusinessLabel(call)).not.toContain(String(call.id));
  });

  it("主列表统一引导查看本次模型输入输出", () => {
    expect(callNextStep(call)).toBe("调用未完成，可查看本次模型输入输出");
    expect(callNextStep({ ...call, error: undefined, effective_status: "OK" }))
      .toBe("查看本次模型输入输出");
    expect(callNextStep({ ...call, run_id: "run_1" }))
      .toBe("调用未完成，可查看本次模型输入输出");
  });
});

describe("设置规范化与脏状态基础规则", () => {
  it("拒绝文本、非有限数、越界和非整数", () => {
    expect(normalizeDraft(integer, "abc")).toBeNull();
    expect(normalizeDraft(integer, "Infinity")).toBeNull();
    expect(normalizeDraft(integer, "0")).toBeNull();
    expect(normalizeDraft(integer, "2.5")).toBeNull();
  });

  it("数字字符串规范化后可与基线深比较", () => {
    expect(normalizeDraft(integer, "020")).toBe("20");
    expect(normalizeDraft(integer, "20")).toBe("20");
  });

  it("布尔与枚举只接受 schema 声明值", () => {
    const boolean: SettingSchema = {
      label: "开关",
      type: "boolean",
      default: "true",
      immediate: true,
      experimental: false,
    };
    const choice: SettingSchema = {
      label: "策略",
      type: "enum",
      default: "safe",
      options: ["safe", "fast"],
      immediate: true,
      experimental: false,
    };
    expect(normalizeDraft(boolean, "yes")).toBeNull();
    expect(normalizeDraft(boolean, "false")).toBe("false");
    expect(normalizeDraft(choice, "unknown")).toBeNull();
  });

  it("可选字符串允许空值，普通字符串仍拒绝空值", () => {
    const optionalString: SettingSchema = {
      label: "视频参考媒体公开基址",
      type: "string",
      default: "",
      allow_empty: true,
      format: "public_http_url",
      immediate: true,
      experimental: false,
    };
    expect(normalizeDraft(optionalString, "   ")).toBe("");
    expect(normalizeDraft({ ...optionalString, allow_empty: false }, "   "))
      .toBeNull();
  });
});

describe("系统设置功能分类", () => {
  it("按业务影响归类且每个设置只出现一次", () => {
    const keys = [
      "text_generation_concurrency",
      "video_submit_concurrency",
      "vlm_request_concurrency",
      "episode_cost_limit_cny",
      "storyboard_workspace_safe_readonly",
      "future_setting",
    ];
    const groups = categorizeSettingKeys(keys);
    expect(groups.find((group) => group.id === "text-generation")?.keys)
      .toEqual(["text_generation_concurrency"]);
    expect(groups.find((group) => group.id === "video-flow")?.keys).toEqual([
      "video_submit_concurrency",
    ]);
    expect(
      groups.find((group) => group.id === "quality-repair")?.affects,
    ).toContain("视频质检");
    expect(groups.find((group) => group.id === "other")?.keys).toEqual([
      "future_setting",
    ]);
    expect(groups.flatMap((group) => group.keys).sort()).toEqual(
      [...keys].sort(),
    );
  });

  it("设置枚举在业务层显示中文，未知值保留以便兼容", () => {
    expect(settingOptionLabel("media_scheduler_policy", "legacy"))
      .toBe("兼容调度");
    expect(settingOptionLabel("media_scheduler_policy", "stage_aware"))
      .toBe("分阶段调度");
    expect(settingOptionLabel("future_setting", "future_value"))
      .toBe("future_value");
  });
});

describe("模型业务名称", () => {
  it("替换内置占位英文且保留真实模型品牌名", () => {
    expect(modelBusinessLabel("Text 模型")).toBe("文本模型");
    expect(modelBusinessLabel("Claude Opus 4.8")).toBe("Claude Opus 4.8");
  });

  const selection: ModelSelection = {
    key: "text",
    label: "文本模型",
    provider: "hiagent",
    model: "",
    options: [
      { provider: "hiagent", model: "hiagent-text", available: false },
      { provider: "openrouter", model: "router-default", available: false },
      { provider: "openrouter", model: "duplicate", available: false },
    ],
  };
  const catalog: CatalogModel[] = [
    {
      id: "hiagent-text",
      provider: "hiagent",
      model: "hiagent-text",
      label: "火山文本",
      kinds: ["text"],
      builtin: true,
      key_configured: false,
    },
    {
      id: "router-default",
      provider: "openrouter",
      model: "router-default",
      label: "路由默认模型",
      kinds: ["text"],
      builtin: true,
      key_configured: false,
    },
    {
      id: "router-ready",
      provider: "openrouter",
      model: "router-ready",
      label: "路由已配置模型",
      kinds: ["text"],
      builtin: true,
      key_configured: true,
    },
  ];

  it("未配置连接时仍保留服务分类，不再渲染空下拉框", () => {
    expect(modelProviderOptions(selection, catalog, "text")).toEqual([
      { provider: "hiagent", model: "hiagent-text", available: false },
      { provider: "openrouter", model: "router-default", available: true },
    ]);
  });

  it("切换服务时优先选择该服务下已配置的模型", () => {
    expect(
      modelAssignmentValue(selection, catalog, "text", "openrouter"),
    ).toBe("router-ready");
    expect(
      modelAssignmentValue(selection, catalog, "text", "hiagent"),
    ).toBe("hiagent-text");
  });

  it("自定义服务只保存职责分配，不生成未声明的模型设置键", () => {
    expect(modelAssignmentSettingKey("openrouter", "text"))
      .toBe("openrouter_model_text");
    expect(modelAssignmentSettingKey("custom:model_123", "text")).toBeNull();
  });
});
