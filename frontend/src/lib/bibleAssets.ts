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

const BIBLE_IDENTITY_PREFIX = 'bible:'

/** 已绑定人物谱的具名角色，identity_id 恒为 `bible:${name}`（与后端
 *  app/domain/storyboard_ops/current_portraits.py::_character_name_from_identity、
 *  app/portraits/portrait_io.py 同一构造）；群演/一次性人物用 entity: 前缀或裸
 *  称谓，据此可以只用字符串前缀判断"这是不是可能出定妆照的具名角色"，不需要
 *  再遍历 bible.characters 做名字匹配。前缀不匹配（含空/undefined）返回 null。 */
export function characterNameFromIdentity(identityId: string | null | undefined): string | null {
  if (!identityId || !identityId.startsWith(BIBLE_IDENTITY_PREFIX)) return null
  const name = identityId.slice(BIBLE_IDENTITY_PREFIX.length)
  return name || null
}

export interface RefsTaskLike {
  refs_status?: string
  refs_target?: string | null
}

function refsTargetMatches(target: string | null | undefined, name: string): boolean {
  if (!target) return true
  if (target === name) return true
  try {
    const targets = JSON.parse(target)
    return Array.isArray(targets) && targets.includes(name)
  } catch {
    return false
  }
}

export type RefsTaskPhase = 'running' | 'failed' | 'idle'

/** 这个角色名此刻相对给定 refs 任务处于哪个阶段。running/failed 都要求
 *  refs_target 命中该名字（未设 target 视为覆盖全部角色）；status 不是
 *  running/failed，或 target 没覆盖到它，一律判 idle——即使任务本身在跑/
 *  失败，"这一轮没管到它"也不该展示成"生成中"/"生成失败"。 */
export function refsPhaseForName(project: RefsTaskLike, name: string): RefsTaskPhase {
  if (project.refs_status !== 'running' && project.refs_status !== 'failed') return 'idle'
  if (!refsTargetMatches(project.refs_target, name)) return 'idle'
  return project.refs_status
}

/**
 * 从 BiblePage.tsx 挪来（2026-08-31，定妆照占位四态改造）：ScriptPage/BoardPage/
 * WallPage 判断"这个角色是否被本轮 refs 生成任务覆盖"要用同一份判据，不得复制
 * 第二份实现——同 findPortraitImage 当年从 ScriptPage 挪出来的先例。只在
 * running 时为真，BiblePage.tsx 改为重新导出，对外接口与测试导入路径不变。
 */
export function characterIsFitting(project: RefsTaskLike, character: { name: string }): boolean {
  return refsPhaseForName(project, character.name) === 'running'
}

export type PortraitPlaceholderKind = 'extra' | 'generating' | 'failed' | 'pending'

const PORTRAIT_PLACEHOLDER_TEXT: Record<PortraitPlaceholderKind, string> = {
  extra: '无定妆照',
  generating: '定妆照生成中',
  failed: '定妆照生成失败',
  pending: '定妆照待生成',
}

/**
 * 映射台/分镜台/生成台共用（用户拍板，2026-08-31）：给定一个角色资产的
 * identity_id + 项目当前 refs 任务状态，算出定妆照占位该显示哪一态。
 * `extra` 覆盖群演/一次性人物/未收录称谓（identity_id 没有 bible: 前缀）——
 * 这是设计使然的"无定妆照"，不套用下面三态；具名角色当前无图时，按是否命中
 * 本轮 refs 任务区分"生成中/生成失败/待生成"，不许一律显示"无"。
 */
export function resolvePortraitPlaceholderKind(
  identityId: string | null | undefined,
  project: RefsTaskLike | null | undefined,
): PortraitPlaceholderKind {
  const name = characterNameFromIdentity(identityId)
  if (!name) return 'extra'
  const phase = refsPhaseForName(project ?? {}, name)
  if (phase === 'running') return 'generating'
  if (phase === 'failed') return 'failed'
  return 'pending'
}

export function portraitPlaceholderText(kind: PortraitPlaceholderKind): string {
  return PORTRAIT_PLACEHOLDER_TEXT[kind]
}

/**
 * 映射台/分镜台/生成台共用轮询忙碌判据（用户拍板，2026-08-31：三个页面都要接，
 * 不止映射台/分镜台）：只看 refs_status/scene_refs_status——App.tsx::projectBusy
 * 还会因 bible_status/plan_status/剧本生成等无关信号触发轮询，范围比这里宽，会让
 * 这三页在无关活动时也反复拉取整份项目 payload，不能直接复用。返回值直接是
 * useProject 期望的 interval 毫秒数。
 */
export function refsBusyPollInterval(project: { refs_status?: string; scene_refs_status?: string } | null): number {
  if (project && (project.refs_status === 'running' || project.scene_refs_status === 'running')) return 4000
  return 0
}

export interface SceneRefsTaskLike {
  scene_refs_status?: string
  scene_refs_target?: string | null
}

const SCENE_ID_PREFIX = 'scene:'

/** resources.scenes[]/asset_manifest.scenes[] 里的 scene_id 恒为 `scene:{name}`
 *  （app/production/prep_pack/resolve_assets.py 装配处），跟角色侧 bible: 前缀
 *  同一构造——但场景没有「群演」等价物：未解析到素材库场景的提及在装配阶段直接
 *  报错剔除，不会静默混入 resources，因此这里恒能拿到前缀、不需要 extra 第四态。
 *  防御性兜底：前缀意外缺失时原样返回 sceneId，仍可用于 refs_target 名字匹配。 */
function sceneNameFromSceneId(sceneId: string | null | undefined, fallbackLabel: string): string {
  if (sceneId && sceneId.startsWith(SCENE_ID_PREFIX)) return sceneId.slice(SCENE_ID_PREFIX.length)
  return sceneId || fallbackLabel
}

/** 场景名此刻相对给定场景图任务处于哪个阶段，判据与 refsPhaseForName 对称
 *  （running/failed 都要求 scene_refs_target 命中该名字，其余一律 idle）。 */
export function sceneRefsPhaseForName(project: SceneRefsTaskLike, name: string): RefsTaskPhase {
  if (project.scene_refs_status !== 'running' && project.scene_refs_status !== 'failed') return 'idle'
  if (!refsTargetMatches(project.scene_refs_target, name)) return 'idle'
  return project.scene_refs_status
}

export type SceneRefPlaceholderKind = 'generating' | 'failed' | 'pending'

const SCENE_REF_PLACEHOLDER_TEXT: Record<SceneRefPlaceholderKind, string> = {
  generating: '场景图生成中',
  failed: '场景图生成失败',
  pending: '场景图待生成',
}

/**
 * 场景图占位三态（用户拍板，2026-08-31，跟 resolvePortraitPlaceholderKind 同一次
 * 改造，场景侧没有 extra 第四态——见 sceneNameFromSceneId 注释）：具卡场景当前无图
 * 时，按是否命中本轮 scene_refs 任务区分"生成中/生成失败/待生成"，不许一律显示
 * "无场景图/无图"。
 */
export function resolveSceneRefPlaceholderKind(
  sceneId: string | null | undefined,
  label: string,
  project: SceneRefsTaskLike | null | undefined,
): SceneRefPlaceholderKind {
  const name = sceneNameFromSceneId(sceneId, label)
  const phase = sceneRefsPhaseForName(project ?? {}, name)
  if (phase === 'running') return 'generating'
  if (phase === 'failed') return 'failed'
  return 'pending'
}

export function sceneRefPlaceholderText(kind: SceneRefPlaceholderKind): string {
  return SCENE_REF_PLACEHOLDER_TEXT[kind]
}

/** BoardPage/WallPage 段落级组件树里 project 要一路往下传给人物和场景两套占位
 *  组件，中间每一跳的 prop 类型都得同时满足 RefsTaskLike 与 SceneRefsTaskLike，
 *  否则窄类型会在半路把 scene_refs_status/scene_refs_target 截断掉。 */
export type ImageGenTaskLike = RefsTaskLike & SceneRefsTaskLike
