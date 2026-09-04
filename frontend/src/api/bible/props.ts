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
