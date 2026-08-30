import { get, mutate, request } from "../client";

export type ModelKind = "text" | "vlm" | "video" | "image";
type ProviderKey = string;

export interface ModelOption {
  provider: ProviderKey;
  model: string;
  available: boolean;
}

export interface ModelSelection {
  key: ModelKind;
  label: string;
  provider: ProviderKey;
  model: string;
  options: ModelOption[];
}

export interface Health {
  ok: boolean;
  models?: Record<ModelKind, ModelSelection>;
}

export interface CatalogModel {
  id: string;
  provider: ProviderKey;
  model: string;
  label: string;
  kinds: ModelKind[];
  builtin: boolean;
  provider_label?: string;
  base_url?: string;
  key_configured?: boolean;
  context_window_tokens?: number;
  max_output_tokens?: number;
  token_limits_source?: string;
  protocol?: string;
}

export interface ModelCatalog {
  items: CatalogModel[];
  // 视频/图像没有统一协议，自建实例必须声明走哪一套；清单由后端注册表给出。
  media_protocols?: {
    video?: string[];
    image?: string[];
    text?: string[];
    vlm?: string[];
  };
}

export interface ModelTestResult {
  latency_ms: number;
  context_window_tokens?: number;
  max_output_tokens?: number;
  token_limits_source?: string;
}

export function getHealth(): Promise<Health> {
  return get("/system/health");
}

export function getModelCatalog(): Promise<ModelCatalog> {
  return get("/models");
}

/** 模型库「测试连接」；已入库模型（含新表单尚未保存的编辑草稿）走
 *  `/models/{id}/test`，body 可选传草稿覆盖当前已保存的连接配置。 */
export function testModel(
  modelId: string,
  body?: object,
): Promise<ModelTestResult> {
  return mutate("POST", `/models/${encodeURIComponent(modelId)}/test`, body);
}

/** 新建自定义模型时、尚未有 id 可用的连接测试。 */
export function testNewModel(body: object): Promise<ModelTestResult> {
  return request("POST", "/models/test", { ...body, provider: "custom" });
}

export function saveModelCredentials(
  modelId: string,
  body: { base_url: string; api_key: string; confirm: true },
) {
  return mutate("PUT", `/models/${encodeURIComponent(modelId)}/credentials`, body);
}

export function createModel(body: object) {
  return request("POST", "/models", { ...body, provider: "custom" });
}

export function updateModel(modelId: string, body: object) {
  return mutate("PUT", `/models/${encodeURIComponent(modelId)}`, body);
}

export function deleteModel(modelId: string) {
  return mutate("DELETE", `/models/${encodeURIComponent(modelId)}`);
}
