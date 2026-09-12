import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { afterEach, describe, expect, it, vi } from "vitest";

// 模型中心管理面板（EP-05 §8）：健康度渲染、凭据只显示掩码、"正在用备用模型
// 顶着"横幅在该状态下确实出现。用 react-test-renderer 真挂载，覆盖组件内部
// 的 fetch-on-mount + 数据合并逻辑，不是只测纯函数。

const { mockApi } = vi.hoisted(() => {
  const healthItems = [
    {
      model_id: "model_1", label: "主用文本模型", provider: "custom:model_1",
      kinds: ["text"], enabled: true, state: "circuit_open",
      calls_window: 12, failures_window: 8, failure_rate_window: 0.667,
      p50_latency_ms_window: 900, p95_latency_ms_window: 2100, window_hours: 24,
      opened_at: 1700000000, last_error_code: "server_error", last_error_at: 1700000000,
    },
    {
      model_id: "model_2", label: "备用文本模型", provider: "custom:model_2",
      kinds: ["text"], enabled: true, state: "healthy",
      calls_window: 3, failures_window: 0, failure_rate_window: 0, p50_latency_ms_window: 400,
      p95_latency_ms_window: 500, window_hours: 24, opened_at: null,
      last_error_code: null, last_error_at: null,
    },
  ];
  // credential payload 故意多带一个 TS 类型里不存在的 "api_key" 明文字段，
  // 验证组件即使拿到多余字段也不会把它渲染出来——只读它声明过的 masked_key/
  // key_fingerprint/rotated_at。
  const credentialItems = [
    {
      model_id: "model_1", base_url: "https://gw1.example.test/v1",
      key_fingerprint: "abcd12345678", masked_key: "sk-****9999",
      rotated_at: 1700000000, rotated_by: "test-admin",
      api_key: "sk-THIS-SHOULD-NEVER-RENDER-6666",
    },
  ];
  const purposeItems = [
    {
      purpose: "text:default", missing_priority_zero: false,
      bindings: [
        { id: "b1", purpose: "text:default", model_id: "model_1", label: "主用文本模型", priority: 0, enabled: true, params: {}, updated_at: 1700000000 },
        { id: "b2", purpose: "text:default", model_id: "model_2", label: "备用文本模型", priority: 1, enabled: true, params: {}, updated_at: 1700000000 },
      ],
      fallback_active: true, active_priority: 1, active_model_id: "model_2",
      active_label: "备用文本模型", reason_code: "circuit_open",
      reason_label: "主用模型已熔断：服务端错误（含凭据失效等 401/403/5xx）",
      since: 1700000000,
    },
    {
      purpose: "vlm:default", missing_priority_zero: true, bindings: [],
      fallback_active: false, active_priority: null, active_model_id: null,
      active_label: null, reason_code: "", reason_label: "", since: null,
    },
  ];
  const mockApi = {
    getModelHealth: vi.fn(async () => ({ items: healthItems })),
    getModelCredentialsSummary: vi.fn(async () => ({ items: credentialItems })),
    getPurposeStatus: vi.fn(async () => ({ items: purposeItems })),
    updateModel: vi.fn(async () => ({})),
    upsertModelBinding: vi.fn(async () => ({ ok: true, id: "b1" })),
    reportMonitorEvent: vi.fn(async () => undefined),
  };
  return { mockApi };
});

vi.mock("../../../api", () => ({ api: mockApi }));

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import ModelOpsPanel from "./ModelOpsPanel";

async function renderPanel() {
  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(ModelOpsPanel, {
        catalog: [],
        onConfigureConnection: vi.fn(),
        refreshCatalog: vi.fn(async () => null),
        toast: vi.fn(),
      }),
    );
  });
  // useEffect 里的 fetch 是异步的；再走一轮微任务确保 setState 已落地。
  await act(async () => {
    await Promise.resolve();
  });
  return renderer;
}

function treeText(renderer: TestRenderer.ReactTestRenderer): string {
  return JSON.stringify(renderer.toJSON());
}

describe("ModelOpsPanel", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("渲染每个模型的健康状态、近 24h 调用与失败率、p50/p95", async () => {
    const renderer = await renderPanel();
    const text = treeText(renderer);
    expect(text).toContain("主用文本模型");
    expect(text).toContain("熔断中");
    expect(text).toContain("备用文本模型");
    expect(text).toContain("健康");
    expect(text).toContain("66.7%"); // failure_rate_window 格式化
    expect(text).toContain("900 ms");
    expect(text).toContain("2,100 ms");
  });

  it("凭据只显示掩码 + 指纹，绝不渲染明文", async () => {
    const renderer = await renderPanel();
    const text = treeText(renderer);
    expect(text).toContain("sk-****9999");
    expect(text).toContain("abcd12345678");
    expect(text).not.toContain("sk-THIS-SHOULD-NEVER-RENDER-6666");
  });

  it("主用模型熔断、正在用备用模型顶着时，顶部横幅与对应 purpose 行都要出现醒目标记", async () => {
    const renderer = await renderPanel();
    const text = treeText(renderer);
    // 顶部横幅：写清哪个 purpose、第几优先级、原因、从什么时候开始。
    expect(text).toContain("正在用备用模型顶着");
    expect(text).toContain("text:default");
    expect(text).toContain("备用文本模型");
    expect(text).toContain("主用模型已熔断");
    // 对应 purpose 行内也要有标记（横幅可能被滚动划走）。
    expect(text).toContain("正在用第 ");
    expect(text).toContain("优先级顶着");
    expect(text).toContain("当前生效");
    // 另一个 purpose 缺主用绑定，同样要显著告警。
    expect(text).toContain("缺主用绑定");
  });

  it("没有任何异常状态时不渲染横幅（空横幅本身是噪音）", async () => {
    mockApi.getPurposeStatus.mockResolvedValueOnce({
      items: [{
        purpose: "text:default", missing_priority_zero: false, bindings: [],
        fallback_active: false, active_priority: 0, active_model_id: "model_1",
        active_label: "主用文本模型", reason_code: "", reason_label: "", since: null,
      }],
    });
    const renderer = await renderPanel();
    const text = treeText(renderer);
    expect(text).not.toContain("正在用备用模型顶着");
    expect(text).not.toContain("缺主用绑定");
  });
});
