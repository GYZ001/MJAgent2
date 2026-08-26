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
