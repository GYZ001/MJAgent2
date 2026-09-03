import { ReferenceImage } from '../api'

/*
 * 曾经这里有 findPortraitImage / findSceneReferenceImage：拿产物里固化的
 * portrait_id / scene_reference_id 快照，去项目人物谱/场景库的 portraits[] /
 * scene_refs[] 里查缩略图。2026-09-01 随同一天的展示修复一起退场——出图解耦到
 * 后台之后，映射/分镜落库那一刻两个快照恒为 null，这条查图路径必然落空，图后来
 * 出好了界面也永远停在"待生成"（真实事故：proj_f8cf2eeb2e66 EP1，人物谱与场景库
 * 六张图全在，映射台/分镜台/生成台一张不显示）。取代它的是后端在
 * GET /episodes/{id} 现算的 current_portrait_image_url /
 * current_scene_image_url（app/domain/storyboard_ops/current_portraits.py、
 * current_scene_refs.py），与生成侧挑参考图同一份判据。快照字段本身保留，只做
 * 溯源，不再参与"现在该显示哪张图"。
 */

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
 * 第二份实现——同 characterPortraitDisplay 当年从 ScriptPage 挪出来的先例。只在
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

/**
 * 从 pages/WallPage.tsx 挪来（2026-08-31，「传入素材」展示重做）：给一张
 * GET /shots/{id}/review 返回的 image_inputs.reference_images 元素生成展示标签。
 * 这是「这一次生成实际发给供应商的参考图」的标签，与 characterPortraitDisplay
 * 展示的「本段脚本声明涉及哪个实体、当前解析到哪张图」是两件不同的事——
 * 前者只在生成台按具体一次生成尝试展示，后者跨分镜台/生成台展示当前解析结果。
 * components/GenerationReferenceGallery.tsx 与 WallPage.tsx 共用同一份实现，
 * WallPage.tsx 通过 `export { referenceImageLabel } from '../lib/bibleAssets'`
 * 保持对外接口（含既有测试的导入路径）不变。
 */
/**
 * 人物谱页「未出图」角标的归因说明（WS13，用户拍板 2026-09-03）：
 * 一句话真名（画面在场证据不足）入谱后不自动出定妆照，此前界面只显示一个笼统
 * 的「未出图」角标，用户会误以为是出图失败去反复重试，看不到
 * ``app.portraits.card_verdict.portrait_generation_decision`` 判定时写下的真实
 * 原因（如"戏份不足……人物卡已登记但未自动出图……也可在人物谱页手动生成"）。
 *
 * 后端 ``GET /projects/{id}?view=bible`` 现在给每个角色投出
 * ``portrait_status``（app.domain.bible_ops.portrait_status 模块，只读投影，
 * 不新造状态机）与 ``portrait_reason``（逐字取 decision_reason/失败原因）。
 * 这里只做纯展示拼接：category 前缀由 status 这个"数据驱动的枚举"决定，具体
 * 原因文案一律取自后端 reason 字段逐字拼接，不在前端重新判断"为什么没出图"
 * ——那道判断只应该在后端一处发生。``portrait_status`` 缺失、或落在
 * ready/missing（没有可归因的队列数据，比如从未走过 discovery 队列的初始
 * 批次角色）时返回 null，调用方沿用既有的笼统「未出图」角标，不编造原因。
 *
 * ``deferred`` 的前缀刻意只写「未出图」，不写死「戏份不足」——用 B 生产库
 * proj_ecabd38b7261（三国白话）/proj_ce9fcf749b23（跑不快的孩子）实测核对时
 * 发现，``auto_applied_asset_pending`` 的 decision_reason 既可能是
 * ``portrait_generation_decision`` 给出的具体"戏份不足（原文仅一句话提及……）"
 * 长句，也可能是身份消歧确认真名后结构性延后出图的通用兜底句"人物卡已加入；
 * 定妆包等待独立资产环节确认"（``app/identity_adjudication.py``/``app/
 * production/prep_pack/persistent_appellation.py`` 都以 ``generate_portrait=
 * False`` 调用建卡，与"戏份不够"无关）——把"戏份不足"焊进前缀会在后一种情形
 * 下断言一个数据不支持的具体原因，界面承诺就跟实际不一致了。真正的原因始终
 * 由 reason 逐字兜底表达，前缀只负责说"这是未出图状态"。
 */
const PORTRAIT_STATUS_DETAIL_PREFIX: Record<'deferred' | 'failed' | 'generating', string> = {
  deferred: '未出图',
  failed: '未出图 · 生成失败',
  generating: '定妆照生成中',
}

export function characterPortraitStatusDetail(character: {
  portrait_status?: string | null
  portrait_reason?: string | null
}): string | null {
  const status = character.portrait_status
  if (status !== 'deferred' && status !== 'failed' && status !== 'generating') return null
  const prefix = PORTRAIT_STATUS_DETAIL_PREFIX[status]
  const reason = (character.portrait_reason || '').trim()
  return reason ? `${prefix}：${reason}` : prefix
}

export function referenceImageLabel(ref: ReferenceImage): string {
  if (ref.type === 'character' || ref.entity_type === 'character') return `人物 · ${ref.entity_name || '未命名'}`
  if (ref.type === 'scene' || ref.entity_type === 'scene') return `场景 · ${ref.entity_name || '未命名'}`
  return ref.entity_name || ref.source || '参考图'
}
