// 人物谱/场景库「完全手动新增/替换」（2026-08-31 用户拍板：图像描述与图片全部
// 由用户提供，不走模型）。对应后端 app/domain/bible_ops/manual_character.py /
// manual_scene.py。单独成文件而不是塞进 characters.ts/scenes.ts——那两个文件
// 已经顶着前端 300 行默认上限，characters.ts 恰好 299 行零余量。

import { request } from "../client";

export interface ManualUploadResult {
  image_url?: string | null;
  style_warning?: string;
}

export interface ManualCharacterAddResult extends ManualUploadResult {
  added: true;
  name: string;
  portrait_id: string;
}

export interface ManualCharacterReplaceResult extends ManualUploadResult {
  replaced: true;
  portrait_id: string;
  downstream_notice?: string;
  rollback_url?: string;
}

export interface ManualSceneAddResult extends ManualUploadResult {
  added: true;
  name: string;
  scene_reference_id: string;
}

export interface ManualSceneReplaceResult extends ManualUploadResult {
  replaced: true;
  scene_reference_id: string;
  previous_scene_reference_id?: string | null;
  downstream_notice?: string;
  rollback_url?: string | null;
}

export function addManualCharacter(
  projectId: string,
  form: FormData,
): Promise<ManualCharacterAddResult> {
  return request("POST", `/projects/${projectId}/characters/manual`, undefined, { form });
}

export function replaceCharacterPortraitImage(
  projectId: string,
  characterName: string,
  form: FormData,
): Promise<ManualCharacterReplaceResult> {
  return request(
    "POST",
    `/projects/${projectId}/characters/${encodeURIComponent(characterName)}/portrait-image`,
    undefined,
    { form },
  );
}

export function addManualScene(
  projectId: string,
  form: FormData,
): Promise<ManualSceneAddResult> {
  return request("POST", `/projects/${projectId}/scenes/manual`, undefined, { form });
}

export function replaceSceneImage(
  projectId: string,
  sceneName: string,
  form: FormData,
): Promise<ManualSceneReplaceResult> {
  return request(
    "POST",
    `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}/image`,
    undefined,
    { form },
  );
}

export function rollbackManualSceneImage(
  projectId: string,
  sceneName: string,
  sceneReferenceId: string,
  body?: { reason?: string },
) {
  return request(
    "POST",
    `/projects/${projectId}/scenes/${encodeURIComponent(sceneName)}` +
      `/refs/${encodeURIComponent(sceneReferenceId)}/manual-rollback`,
    body,
  );
}
