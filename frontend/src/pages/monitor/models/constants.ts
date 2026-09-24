import type { CatalogModel, ModelKind, ModelSelection, ProtocolHint } from "../../../api";

export type ProviderKey = string;

/** 模型能力总清单——新增能力（比如这次的声音生成）只改这一处；能力勾选框、
 *  协议候选集（两处）原来各写死一份重复清单，现在都改成引用这里。 */
export const MODEL_KINDS: ModelKind[] = ["text", "vlm", "video", "image", "voice"];
export const MODEL_ROWS: Array<{ key: ModelKind; label: string; note: string }> = [
  { key: "text", label: "文本模型", note: "分集、映射包、分镜与文本修复" },
  { key: "vlm", label: "视觉理解模型", note: "定妆照、场景图与关键帧质检" },
  { key: "video", label: "视频模型", note: "首尾帧、参考图与视频输入生成" },
  { key: "image", label: "图像模型", note: "Seedream 参考图 / 定妆照" },
  { key: "voice", label: "声音生成模型", note: "角色音色设计（人物卡配音）" },
];
export const MODEL_KIND_LABELS: Record<ModelKind, string> = {
  text: "文本生成",
  vlm: "视觉理解",
  video: "视频生成",
  image: "图像生成",
  voice: "声音生成",
};
export const PROVIDER_LABELS: Record<string, string> = {
  hiagent: "火山",
  minimax_h3: "MiniMax H3",
  openrouter: "OpenRouter",
  bailian: "百炼",
  deepseek: "DeepSeek",
  zhipu: "智谱",
};

export function modelBusinessLabel(value: string) {
  return value.trim().toLowerCase() === "text 模型" ? "文本模型" : value;
}

/** 协议下拉的显示名：命中 protocol_hints 就显示中文名，没有 hint 的协议
 *  （现状多数协议如此）原样显示协议标识——不写死清单、也不编造中文。 */
export function protocolLabel(protocol: string, hints?: Record<string, ProtocolHint>) {
  return hints?.[protocol]?.label || protocol;
}

/** 协议下方的中文填写提示，按「服务地址示例 / 模型标识示例 / 说明」拼接，
 *  缺项跳过、不留空分隔符；没有 hint 时返回空串，调用方据此不渲染提示行。 */
export function protocolHintText(hint?: ProtocolHint) {
  if (!hint) return "";
  return [
    hint.base_url_example ? `服务地址示例：${hint.base_url_example}` : "",
    hint.model_example ? `模型标识示例：${hint.model_example}` : "",
    hint.note || "",
  ]
    .filter(Boolean)
    .join("；");
}

export function formatTokenCapacity(value?: number) {
  if (!value) return "待检测";
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024}K`;
  return value.toLocaleString("zh-CN");
}

export function tokenLimitSourceLabel(source?: string) {
  if (source === "provider_metadata") return "供应商元数据";
  if (source === "configured") return "已配置";
  return "128K/32K 兼容默认";
}

/**
 * “available” 表示连接是否就绪，不表示服务商是否支持该职责。
 * 分配下拉必须保留待配置服务商，否则一个全新环境会把所有选项过滤成空白。
 */
export function modelProviderOptions(
  selection: ModelSelection,
  catalogItems: CatalogModel[],
  kind: ModelKind,
) {
  const providersWithModels = new Set(
    catalogItems
      .filter((item) => item.kinds.includes(kind))
      .map((item) => item.provider),
  );
  const seen = new Set<string>();
  return selection.options
    .filter(
      (option) =>
        providersWithModels.has(option.provider) ||
        option.provider === selection.provider,
    )
    .filter((option) => {
      if (seen.has(option.provider)) return false;
      seen.add(option.provider);
      return true;
    })
    .map((option) => ({
      ...option,
      available:
        option.available ||
        catalogItems.some(
          (item) =>
            item.provider === option.provider &&
            item.kinds.includes(kind) &&
            item.key_configured,
        ),
    }));
}

export function modelAssignmentValue(
  selection: ModelSelection,
  catalogItems: CatalogModel[],
  kind: ModelKind,
  provider: ProviderKey,
  draftModel?: string,
) {
  const models = catalogItems.filter(
    (item) => item.provider === provider && item.kinds.includes(kind),
  );
  const inCatalog = (model: string | undefined) =>
    Boolean(model && models.some((item) => item.model === model));
  if (draftModel !== undefined && inCatalog(draftModel)) return draftModel;
  if (provider === selection.provider && inCatalog(selection.model))
    return selection.model;

  const providerDefault = selection.options.find(
    (option) => option.provider === provider,
  )?.model;
  const configuredDefault = models.find((item) => item.key_configured)?.model;
  if (configuredDefault) return configuredDefault;
  if (inCatalog(providerDefault)) return providerDefault || "";
  return models[0]?.model || draftModel || providerDefault || "";
}

export function modelAssignmentSettingKey(
  provider: ProviderKey,
  kind: ModelKind,
) {
  return provider.startsWith("custom:") ? null : `${provider}_model_${kind}`;
}

/** 健康度状态 -> 中文标签，模型中心健康表/横幅/用途绑定链共用。 */
export const HEALTH_STATE_LABELS: Record<string, string> = {
  healthy: "健康",
  degraded: "降级",
  circuit_open: "熔断中",
  half_open: "探测恢复中",
};

export function healthStateLabel(state: string) {
  return HEALTH_STATE_LABELS[state] || state || "未知";
}

export function formatPercent(ratio: number) {
  return `${(ratio * 100).toFixed(1)}%`;
}

export function formatLatencyMs(value: number | null | undefined) {
  return value == null ? "—" : `${value.toLocaleString("zh-CN")} ms`;
}

export function formatTimestamp(value: number | null | undefined) {
  if (!value) return "—";
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}
