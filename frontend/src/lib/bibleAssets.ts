import { Bible } from '../api'

/**
 * 提取到 lib/（原在 pages/ScriptPage.tsx）：分镜台 2.0.0 展示改造需要在
 * BoardPage.tsx 里按 portrait_id / scene_reference_id 查同一份人物谱/场景库
 * 素材缩略图，跨页复用必须共享同一个真源，不得复制第二份实现——见
 * docs/STORYBOARD_PROMPT_IR_DESIGN.md。ScriptPage.tsx 通过
 * `export { findPortraitImage, findSceneReferenceImage } from '../lib/bibleAssets'`
 * 保持对外接口不变，函数体没有改动。
 */

/** 复用 BiblePage 展示 character_portraits 的口径：在项目人物谱的 portraits[] 里按 id 查图。 */
export function findPortraitImage(bible: Bible | null | undefined, portraitId: string | null | undefined): string | null {
  if (!portraitId) return null
  for (const character of bible?.characters ?? []) {
    const match = (character.portraits ?? []).find(portrait => portrait.id === portraitId)
    if (match?.image_url) return match.image_url
  }
  return null
}

/** 复用 ScenesPage 展示 scene_references 的口径：在项目场景库的 scene_refs[] 里按 id 查图。 */
export function findSceneReferenceImage(bible: Bible | null | undefined, sceneReferenceId: string | null | undefined): string | null {
  if (!sceneReferenceId) return null
  for (const scene of bible?.scenes ?? []) {
    const match = (scene.scene_refs ?? []).find(ref => ref.id === sceneReferenceId)
    if (match?.image_url) return match.image_url
  }
  return null
}

/**
 * 用户拍板（2026-08-30）：映射台/分镜台/生成台展示定妆照时，按身份实时解析出
 * 「当前实际会用的那张」，不再用 asset_manifest 里固化的 portrait_id 快照当作
 * 当前状态渲染（那只做溯源）。current_portrait_id / current_portrait_image_url
 * 由后端 GET /episodes/{id} 投影时按本集集号现算（app/domain/storyboard_ops/
 * current_portraits.py），与生成时 app.portraits.current_portrait_ref 同一份
 * 判据——这里只做纯展示派生（取图、比对两个 id 是否一致），不重新判定"当前是
 * 哪一张"本身，避免长出第二份可能漂移的判据。
 */
export interface CharacterPortraitDisplay {
  /** 当前解析出的定妆照；null 表示这个身份现在没有可用定妆照（如群演），必须
   *  显示"无定妆照"，不得回退显示 portrait_id 快照对应的旧图。 */
  imageUrl: string | null
  /** true 表示当前解析结果与发布时固化的 portrait_id 快照不同——分镜/映射记录
   *  当时依据的是另一张图，界面需要一个不打断的轻标记，不是弹窗或告警。 */
  updated: boolean
}

export function characterPortraitDisplay(character: {
  portrait_id?: string | null
  current_portrait_id?: string | null
  current_portrait_image_url?: string | null
}): CharacterPortraitDisplay {
  return {
    imageUrl: character.current_portrait_image_url ?? null,
    updated: !!character.portrait_id
      && !!character.current_portrait_id
      && character.portrait_id !== character.current_portrait_id,
  }
}
