import { get } from "./client";

// 跨域共享的叶子类型：多个域（bible/screenplay/storyboard/video/projects）都
// 引用这些形状，放在这里避免每个域文件互相 import 对方造成循环阅读负担。
// 这些类型本身没有循环依赖（都是纯数据形状），只是被多处引用。

export interface EvidenceIssue {
  code: string;
  severity: "info" | "warning" | "blocker";
  subject: string;
  message: string;
  repair_hint?: string | null;
  repairable: boolean;
}

export interface EvaluationSummary {
  id: string;
  evaluator_type: string;
  evaluator_name: string;
  evaluator_version: string;
  status: string;
  hard_gate_passed: number | boolean;
  score?: number | null;
  issues?: EvidenceIssue[];
  evidence?: Record<string, unknown>;
  recovered: number | boolean;
}

export interface ArtifactEvidence {
  id: string;
  type: string;
  version: number;
  status: string;
  trust_level: string;
  content_hash: string;
  contract_version?: string | null;
  prompt_version?: string | null;
  parent_artifact_ids?: string[];
  stale_reason?: string | null;
  evaluations: EvaluationSummary[];
}

/** 服务端 run 的起止时间（秒）；缺失表示该任务从未跑过。 */
export interface TaskTiming {
  started_at?: number | null;
  finished_at?: number | null;
}

/** 单个镜头的生成耗时。已完成迭代累计在 elapsed_ms，仍在跑的那轮给起点。 */
export interface ShotTiming {
  elapsed_ms: number;
  running_since?: number | null;
  iterations: number;
}

export interface NumberConstraint {
  type: "number";
  unit: string;
  default: number;
  min: number;
  max: number;
  step: number;
  finite: boolean;
}

/** 世界书/映射台/分镜台分环节文本模型下拉的单个可选项；只包含已配凭据的模型，
 *  不会出现选了就必然失败的条目（见 app/model_registry.py::text_model_choices）。*/
export interface TextModelChoice {
  provider: string;
  label: string;
  model: string;
}

/** 证据抽屉的上下游关联查询：给定一个 artifact，返回它的祖先/后代证据。 */
export function getArtifactLineage(
  artifactId: string,
): Promise<{ ancestors: ArtifactEvidence[]; descendants: ArtifactEvidence[] }> {
  return get(`/artifacts/${artifactId}/lineage`);
}

export const numToCn = (n: number): string => {
  const cn = "零一二三四五六七八九";
  if (n <= 10) return n === 10 ? "十" : cn[n];
  if (n < 20) return "十" + cn[n % 10];
  if (n < 100)
    return cn[Math.floor(n / 10)] + "十" + (n % 10 ? cn[n % 10] : "");
  return String(n);
};
