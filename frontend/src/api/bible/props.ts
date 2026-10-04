import { mutate, request } from "../client";

/** 世界书物件库一条道具：参考图与人物谱定妆照、场景库参考图同一条路径传给视频生成。 */
export interface PropItem {
  name: string;
  appearance: string;
  aliases: string[];
  image_path: string | null;
  image_url: string | null;
  status: string;
}

export interface PropListResponse {
  project_id: string;
  items: PropItem[];
}

export function listProps(projectId: string): Promise<PropListResponse> {
  return request("GET", `/projects/${encodeURIComponent(projectId)}/props`);
}

export function regenerateProp(projectId: string, name: string): Promise<PropItem> {
  return mutate("POST", `/projects/${encodeURIComponent(projectId)}/props/${encodeURIComponent(name)}/regenerate`);
}

/** 道具卡「按现行规则复核」一次判定里被删掉的一条外观子句（原文逐字摘录，不是改写）。 */
export interface PropAuditRemovedClause {
  index: number;
  category: string;
  reason: string;
  text: string;
}

/** 被剪除的一条别名：source 区分是数据推导的歧义剪除还是模型提名的泛称剪除。 */
export interface PropAuditRemovedAlias {
  alias: string;
  source: string;
  reason: string;
}

/** 两次独立判定不一致 / 归属没有自己的卡 / 模型自述拿不准——交给人工确认
 * 删除或保留的一条存疑。``doubt_key`` 就是确认/保留端点请求体要传的那个
 * 值（按原文逐字定位，不是不透明 id）。 */
export interface PropAuditDoubt {
  kind: "clause" | "alias";
  index?: number;
  alias?: string;
  text: string;
  doubt_type: string;
  reason_a?: string;
  reason_b?: string;
  owner?: string;
}

/** 一张卡最近一轮复核记录；status 为 running/ready/failed（与建卡补卡闸门同一口径）。 */
export interface PropAuditRecord {
  prop_name: string;
  rules_version: string;
  status: string;
  old_appearance: string | null;
  new_appearance: string | null;
  removed_clauses: PropAuditRemovedClause[];
  removed_aliases: PropAuditRemovedAlias[];
  reimaged: boolean;
  feature_shortfall: boolean;
  doubts: PropAuditDoubt[];
  error: string | null;
}

export interface PropAuditAcceptedResponse {
  project_id: string;
  accepted: string[];
}

export interface PropAuditListResponse {
  project_id: string;
  items: PropAuditRecord[];
}

/** 道具库手动入口：整项目全部卡后台复核，立即返回受理的道具名列表（不等复核跑完）。 */
export function auditProps(projectId: string): Promise<PropAuditAcceptedResponse> {
  return mutate("POST", `/projects/${encodeURIComponent(projectId)}/props/audit`);
}

export function listPropAudits(projectId: string): Promise<PropAuditListResponse> {
  return request("GET", `/projects/${encodeURIComponent(projectId)}/props/audit`);
}

export interface PropAuditDoubtResolution {
  prop_name: string;
  doubt_key: string;
  status: string;
  reimaged?: boolean;
}

/** 人工确认删除一条存疑：按卡名 + 存疑原文（doubt_key 本身就是逐字原文）
 * 定位，请求体传卡名避免道具名里的特殊字符在 URL 路径段里被误解析。 */
export function confirmPropAuditDoubt(
  projectId: string, propName: string, doubtKey: string,
): Promise<PropAuditDoubtResolution> {
  return mutate("POST", `/projects/${encodeURIComponent(projectId)}/props/audit/confirm`, {
    prop_name: propName, doubt_key: doubtKey,
  });
}

/** 人工确认保留一条存疑：否定模型的怀疑，同一规则版本内不再重复呈现。 */
export function keepPropAuditDoubt(
  projectId: string, propName: string, doubtKey: string,
): Promise<PropAuditDoubtResolution> {
  return mutate("POST", `/projects/${encodeURIComponent(projectId)}/props/audit/keep`, {
    prop_name: propName, doubt_key: doubtKey,
  });
}
