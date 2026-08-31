import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  api, ApiError, Bible, BibleImpactPreview, Character, Portrait, PortraitView, RefsCostPrecheck,
} from '../api'
import { useNav, usePoll, useProject } from '../App'
import SearchField from '../components/SearchField'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import type { ImpactSummary } from '../components/harness/ImpactDialog'
import GenerationParamsDialog from '../components/GenerationParamsDialog'
import QueryState from '../components/QueryState'
import PrepSubnav from '../components/PrepSubnav'
import { SINGLE_ROW_ASSET_PAGE, useFillPageSize } from '../hooks/useFillPageSize'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { usePrepListState } from '../hooks/usePrepListState'
import { formatBookTitle } from '../lib/bookTitle'
import { sceneStepStatus } from '../lib/prepSteps'
import { applyStyleRegen } from '../lib/styleRegen'
import type { PrepStepStatus } from '../lib/statusLabels'
import CharacterFilters, {
  characterFilterActiveCount,
  EMPTY_CHARACTER_FILTERS,
  matchCharacterFilters,
  type CharacterFilterState,
} from '../components/CharacterFilters'
import CharacterQaPanel from '../components/CharacterQaPanel'
import ImageCompareModal from '../components/ImageCompareModal'
import OperationError from '../components/OperationError'
import StageTextModelPicker from '../components/StageTextModelPicker'
import VisualStyleDialog from '../components/VisualStyleDialog'
import WorldbuildingStatus from '../components/WorldbuildingStatus'
import NominateCharacterEntry from '../components/NominateCharacterDialog'
import { useVisualStyleDialog } from '../hooks/useVisualStyleDialog'
import "../styles/BiblePage.css";

const REQUIRED_CHARACTER_VIEWS = ['front_full', 'three_quarter', 'profile'] as const

function trackBible(name: string, projectId: string, dimensions: Record<string, string | number | boolean> = {}) {
  void api.reportMonitorEvent(name, dimensions, projectId).catch(() => undefined)
}

type RefsProgress = Awaited<ReturnType<typeof api.refsProgress>>
type PaymentQuote = RefsCostPrecheck & {
  estimated_duration_min?: number[]
  estimate_note?: string
  character_names?: string[]
}

export function currentPortrait(character: Character): Portrait | null {
  const portraits = [...(character.portraits ?? [])]
    .filter(portrait => !!portrait.image_url || (portrait.views ?? []).some(view => !!view.image_url))
    .sort((a, b) => b.ep_start - a.ep_start)
  return portraits.find(p => p.ep_end == null) || portraits[0] || null
}

type PortraitAvailability =
  | 'generating'
  | 'passed'
  | 'warning'
  | 'failed'
  | 'unverified'
  | 'missing'
  | 'deferred'
  | 'blocked'

export function characterIsPortraitEligible(character: Character): boolean {
  if (!character.name?.trim()) return false
  if (character.portrait_eligible === false) return false
  if (character.appearance_status && character.appearance_status !== 'grounded') return false
  return true
}

export function portraitAvailability(character: Character, fitting: boolean): PortraitAvailability {
  if (fitting) return 'generating'
  const portrait = currentPortrait(character)
  if (!portrait || (!portrait.image_url && !(portrait.views ?? []).some(v => v.image_url))) {
    if (!characterIsPortraitEligible(character)) {
      if (character.presence_status === 'mentioned_only' || character.appearance_status === 'deferred') {
        return 'deferred'
      }
      return 'blocked'
    }
    return character.ref_image_url ? 'unverified' : 'missing'
  }
  const status = portrait.pack_status
  if (status === 'generating' || status === 'qa_pending') return 'generating'
  if (status === 'failed') return 'failed'
  const readyViewRoles = new Set(
    (portrait.views ?? [])
      .filter(view => view.status === 'ready' && !!view.image_url)
      .map(view => view.view_role),
  )
  if (REQUIRED_CHARACTER_VIEWS.some(role => !readyViewRoles.has(role))) return 'failed'
  // VLM 图片质检已下线：三视角文件齐全（技术产物存在）即视为通过，不再依赖质检分数。
  return status === 'ready' ? 'passed' : 'unverified'
}

function availabilityStamp(state: PortraitAvailability): { label: string; color: string } {
  switch (state) {
    case 'generating': return { label: '生成或质检中', color: 'gold' }
    case 'passed': return { label: '已采用且通过', color: 'green' }
    case 'warning': return { label: '已采用 · 质量需复核', color: 'gold' }
    case 'failed': return { label: '暂不可用', color: 'red' }
    case 'missing': return { label: '未出图', color: 'grey' }
    case 'deferred': return { label: '暂缓定妆', color: 'grey' }
    case 'blocked': return { label: '外观未通过', color: 'gold' }
    default: return { label: '待质检', color: 'grey' }
  }
}

function cloneBible(bible: Bible): Bible {
  return JSON.parse(JSON.stringify(bible)) as Bible
}

function isBibleDraft(value: unknown): value is Bible {
  return !!value
    && typeof value === 'object'
    && Array.isArray((value as Bible).characters)
    && !!(value as Bible).world
}

function bibleDraftKey(projectId: string, version?: number | null): string {
  return `bible-draft:${projectId}:${version ?? 0}`
}

function countBibleChanges(next: Bible | null, base: Bible | null | undefined): number {
  if (!next || !base) return 0
  let count = 0
  if (next.world.visual_style_canonical !== base.world.visual_style_canonical) count += 1
  const baseByName = new Map((base.characters ?? []).map(character => [character.name, character]))
  for (const character of next.characters ?? []) {
    const previous = baseByName.get(character.name)
    if (!previous) {
      count += 1
      continue
    }
    if (character.role !== previous.role) count += 1
    if (character.appearance_canonical !== previous.appearance_canonical) count += 1
    if (character.personality !== previous.personality) count += 1
    if (character.speech_style !== previous.speech_style) count += 1
    if (JSON.stringify(character.relationships ?? []) !== JSON.stringify(previous.relationships ?? [])) count += 1
  }
  const nextNames = new Set((next.characters ?? []).map(character => character.name))
  for (const character of base.characters ?? []) {
    if (!nextNames.has(character.name)) count += 1
  }
  return count
}

function characterChanged(next: Character | null | undefined, base: Character | null | undefined): boolean {
  if (!next || !base) return !!next !== !!base
  return next.role !== base.role
    || next.appearance_canonical !== base.appearance_canonical
    || next.personality !== base.personality
    || next.speech_style !== base.speech_style
    || JSON.stringify(next.relationships ?? []) !== JSON.stringify(base.relationships ?? [])
}

function characterHasPortrait(character: Character): boolean {
  return (character.portraits ?? []).some(portrait =>
    !!portrait.image_url || (portrait.views ?? []).some(view => !!view.image_url),
  ) || !!character.ref_image_url
}

function characterFirstAppearance(character: Character): number | null {
  const starts = (character.portraits ?? []).map(item => item.ep_start).filter(value => value > 0)
  return starts.length ? Math.min(...starts) : null
}

function characterFilterMeta(
  project: { refs_status?: string; refs_target?: string | null } | null | undefined,
  character: Character,
) {
  const portraits = character.portraits ?? []
  return {
    availability: characterAvailabilityForFilter(project, character),
    hasPortrait: characterHasPortrait(character),
    hasCandidate: portraits.length > 1 || portraits.some(item => !!item.pack_status && item.pack_status !== 'ready'),
    hasHistory: portraits.some(item => item.ep_end != null || item.ep_start <= 0),
    firstAppearance: characterFirstAppearance(character),
  }
}

function compareCharacters(left: Character, right: Character, sort: string, project?: {
  refs_status?: string
  refs_target?: string | null
} | null): number {
  if (sort === 'role') return (left.role || '').localeCompare(right.role || '', 'zh-CN')
  if (sort === 'first') return (characterFirstAppearance(left) ?? Number.MAX_SAFE_INTEGER)
    - (characterFirstAppearance(right) ?? Number.MAX_SAFE_INTEGER)
  if (sort === 'qa') return characterAvailabilityForFilter(project, left)
    .localeCompare(characterAvailabilityForFilter(project, right))
  return left.name.localeCompare(right.name, 'zh-CN')
}

export function characterIsFitting(project: { refs_status?: string; refs_target?: string | null }, character: Character): boolean {
  if (project.refs_status !== 'running') return false
  if (!project.refs_target) return true
  if (project.refs_target === character.name) return true
  try {
    const targets = JSON.parse(project.refs_target)
    return Array.isArray(targets) && targets.includes(character.name)
  } catch {
    return false
  }
}

function characterAvailabilityForFilter(
  project: { refs_status?: string; refs_target?: string | null } | null | undefined,
  character: Character,
): PortraitAvailability {
  return portraitAvailability(character, !!project && characterIsFitting(project, character))
}

export function bibleStepStatus(project: {
  bible?: Bible | null
  bible_status?: string
  refs_status?: string
}): PrepStepStatus {
  const states = (project.bible?.characters ?? []).map(character => portraitAvailability(character, false))
  const hasTaskProblem = ['failed', 'warning'].includes(project.bible_status || '')
    || ['failed', 'warning'].includes(project.refs_status || '')
  const isRunning = project.bible_status === 'running' || project.refs_status === 'running'
  if (hasTaskProblem) return 'problem'
  if (isRunning) return 'running'
  if (states.some(state => state === 'failed' || state === 'missing' || state === 'blocked')) return 'problem'
  if (states.length > 0 && states.every(state => state === 'passed' || state === 'warning' || state === 'deferred')) return 'done'
  if (project.bible_status === 'ready' && project.refs_status === 'ready') return 'done'
  return 'idle'
}

function episodeStepStatus(project: { episodes?: unknown[]; episodes_total?: number; episode_count?: number }): PrepStepStatus {
  if (Array.isArray(project.episodes)) return project.episodes.length > 0 ? 'done' : 'idle'
  if (typeof project.episodes_total === 'number') return project.episodes_total > 0 ? 'done' : 'idle'
  if (typeof project.episode_count === 'number') return project.episode_count > 0 ? 'done' : 'idle'
  return 'idle'
}

export function characterCompareImages(character: Character): { src: string; label: string }[] {
  const images: { src: string; label: string; ep: number }[] = []
  const seen = new Set<string>()
  const addImage = (src: string, label: string, ep: number) => {
    if (seen.has(src)) return
    seen.add(src)
    images.push({ src, label, ep })
  }
  for (const portrait of character.portraits ?? []) {
    for (const view of portrait.views ?? []) {
      if (view.image_url) {
        addImage(
          view.image_url,
          `${portraitVersionLabel(portrait)} · ${VIEW_ROLE_LABELS[view.view_role || ''] || view.view_role || '视角'}`,
          portrait.ep_start,
        )
      }
    }
    if (portrait.image_url) {
      addImage(portrait.image_url, portraitVersionLabel(portrait), portrait.ep_start)
    }
  }
  if (!images.length && character.ref_image_url) {
    addImage(character.ref_image_url, '历史定妆照', 0)
  }
  return images.sort((a, b) => b.ep - a.ep).map(({ src, label }) => ({ src, label }))
}

export function summarizeProgress(progress: RefsProgress | null): string {
  if (!progress) return ''
  const parts = [
    `定妆进度：已完成 ${progress.ready} / ${progress.total}`,
    `失败 ${progress.failed}`,
    `缺失 ${progress.missing}`,
  ]
  if (progress.deferred) parts.push(`暂缓 ${progress.deferred}`)
  if (progress.blocked) parts.push(`外观未通过 ${progress.blocked}`)
  return parts.join('，')
}

function progressProblemNames(progress: RefsProgress | null): string[] {
  return (progress?.items ?? [])
    .filter(item => item.status === 'missing' || item.status === 'failed')
    .map(item => item.character)
    .filter(Boolean)
}

export type BibleMergeConflict = {
  path: string
  base: unknown
  local: unknown
  server: unknown
}

export function bibleConflictFieldLabel(path: string): string {
  if (!path) return '人物谱'
  const parts = path.split('.')
  const field = parts.at(-1) || path
  const labels: Record<string, string> = {
    role: '角色定位',
    appearance_canonical: '固定外观',
    personality: '性格',
    speech_style: '说话风格',
    relationships: '人物关系',
    visual_style_canonical: '统一画面风格',
  }
  if (parts[0] === 'characters' && parts[1]) {
    return `${parts[1]} · ${labels[field] || '角色信息'}`
  }
  return labels[field] || '人物谱字段'
}

const sameValue = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right)

export function mergeBibleThreeWay(
  base: Bible,
  local: Bible,
  server: Bible,
  choices: Record<string, 'local' | 'server'> = {},
): { bible: Bible; conflicts: BibleMergeConflict[] } {
  const conflicts: BibleMergeConflict[] = []

  const mergeValue = (baseValue: unknown, localValue: unknown, serverValue: unknown, path: string): unknown => {
    if (sameValue(localValue, serverValue)) return localValue
    if (sameValue(localValue, baseValue)) return serverValue
    if (sameValue(serverValue, baseValue)) return localValue
    if (path === 'characters' && Array.isArray(localValue) && Array.isArray(serverValue)) {
      const baseItems = Array.isArray(baseValue) ? baseValue as Character[] : []
      const localItems = localValue as Character[]
      const serverItems = serverValue as Character[]
      const baseByName = new Map(baseItems.map(item => [item.name, item]))
      const localByName = new Map(localItems.map(item => [item.name, item]))
      const serverByName = new Map(serverItems.map(item => [item.name, item]))
      const names = [...new Set([...serverItems.map(item => item.name), ...localItems.map(item => item.name)])]
      return names.flatMap(name => {
        const merged = mergeValue(baseByName.get(name), localByName.get(name), serverByName.get(name), `characters.${name}`)
        return merged == null ? [] : [merged]
      })
    }
    if (baseValue && localValue && serverValue
      && typeof baseValue === 'object' && typeof localValue === 'object' && typeof serverValue === 'object'
      && !Array.isArray(baseValue) && !Array.isArray(localValue) && !Array.isArray(serverValue)) {
      const baseRecord = baseValue as Record<string, unknown>
      const localRecord = localValue as Record<string, unknown>
      const serverRecord = serverValue as Record<string, unknown>
      const keys = new Set([...Object.keys(baseRecord), ...Object.keys(localRecord), ...Object.keys(serverRecord)])
      return Object.fromEntries([...keys].map(key => [
        key,
        mergeValue(baseRecord[key], localRecord[key], serverRecord[key], path ? `${path}.${key}` : key),
      ]))
    }
    conflicts.push({ path, base: baseValue, local: localValue, server: serverValue })
    return choices[path] === 'local' ? localValue : serverValue
  }

  return { bible: mergeValue(base, local, server, '') as Bible, conflicts }
}

function cloneCharacter(character: Character): Character {
  return JSON.parse(JSON.stringify(character)) as Character
}

function replaceCharacter(bible: Bible, name: string, character: Character): Bible {
  return {
    ...bible,
    characters: bible.characters.map(item => item.name === name ? cloneCharacter(character) : item),
  }
}

function cnEpisodeToNumber(value: string): number | null {
  if (/^\d+$/.test(value)) return Number(value)
  const digits: Record<string, number> = { 零: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 }
  if (value === '十') return 10
  if (value.includes('十')) {
    const [tensRaw, onesRaw] = value.split('十')
    const tens = tensRaw ? digits[tensRaw] : 1
    const ones = onesRaw ? digits[onesRaw] : 0
    if (typeof tens === 'number' && typeof ones === 'number') return tens * 10 + ones
  }
  return digits[value] ?? null
}

function promptSegments(prompt: string | undefined): { label: string; text: string }[] {
  const parts = (prompt || '').split('。').map(part => part.trim()).filter(Boolean)
  const fallback = (prompt || '').trim()
  return [
    { label: '统一画风', text: parts[0] || fallback || '未生成' },
    { label: '角色定妆', text: parts[1] || parts[0] || fallback || '未生成' },
    { label: '构图要求', text: parts.slice(2).join('。') || parts[1] || fallback || '未生成' },
  ]
}

export default function BiblePage() {
  const { projectId, toast, go, registerNavigationGuard } = useNav()
  const { data: p, refresh, error, status, loading } = useProject(projectId!, undefined, 'bible')
  const [editing, setEditing] = useState<Bible | null>(null)
  const [editBaseVersion, setEditBaseVersion] = useState<number | null>(null)
  const [editBaseBible, setEditBaseBible] = useState<Bible | null>(null)
  const [undoStack, setUndoStack] = useState<Bible[]>([])
  const [draftState, setDraftState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [busy, setBusy] = useState(false)
  const [pageSize, characterGridRef] = useFillPageSize(SINGLE_ROW_ASSET_PAGE)
  const [listState, setListState] = usePrepListState(projectId!, 'bible-characters', pageSize)
  const charSearch = listState.search
  const charFilters: CharacterFilterState = { ...EMPTY_CHARACTER_FILTERS, ...(listState.filters as Partial<CharacterFilterState>) }
  const charPage = listState.page
  const setCharSearch = (value: string) => setListState(current => ({ ...current, search: value, page: 0 }))
  const setCharFilters = (value: CharacterFilterState) => setListState(current => ({ ...current, filters: value, page: 0 }))
  const setCharPage = (value: number) => setListState(current => ({ ...current, page: value, scrollY: window.scrollY }))
  const [paramsCharacterName, setParamsCharacterName] = useState<string | null>(null)
  const [qaDetail, setQaDetail] = useState<{ characterName: string; portrait: Portrait | null } | null>(null)
  const [compareDetail, setCompareDetail] = useState<{ title: string; images: { src: string; label: string }[] } | null>(null)
  const [timelineCharacter, setTimelineCharacter] = useState('')
  const [skipConfirm, setSkipConfirm] = useState<{ count: number; names: string[] } | null>(null)
  const [conflict, setConflict] = useState<{
    message: string
    current_version?: number
    character_names?: string[]
    server_bible?: Bible | null
  } | null>(null)
  const styleDialog = useVisualStyleDialog(projectId!)
  const styleActionRef = useRef<'full_regen' | 'style_only'>('full_regen')
  const editingRef = useRef<Bible | null>(null)
  // 生成前的费用确认弹窗已删除（2026-08-29 用户拍板：模型与视频生成走公司自
  // 有服务，不计费，这层确认对他是纯摩擦）。busyRef 是同步锁，防止连点两次
  // 直接把「预检 -> 立即确认」这条自动化路径跑两遍——React 的 busy 状态更新
  // 是异步的，两次物理点击之间不保证已经重渲染出 disabled，ref 在同一个事件
  // 循环内立即生效，堵住这个窗口。
  const busyRef = useRef(false)

  const biblePreview = editing ?? p?.bible
  const charQuery = charSearch.trim()
  const activeCharFilterCount = characterFilterActiveCount(charFilters)
  const hasCharacterCriteria = Boolean(charQuery) || activeCharFilterCount > 0
  const indexedCharsPreview = (biblePreview?.characters ?? []).map((c, i) => ({ c, i }))
  const filteredCharsPreview = indexedCharsPreview.filter(({ c }) =>
    matchCharacterFilters(c, charQuery, charFilters, characterFilterMeta(p, c)),
  ).sort((left, right) => compareCharacters(left.c, right.c, charFilters.sort, p))
  const charPageCount = pageSize > 0
    ? Math.max(1, Math.ceil(filteredCharsPreview.length / pageSize))
    : 1
  const dirtyCount = countBibleChanges(editing, p?.bible)
  const dirty = dirtyCount > 0
  const currentEditVersion = editBaseVersion ?? p?.bible_version ?? 0
  const {
    data: draftSavedAt,
    error: draftSaveError,
  } = usePoll<number>(
    async () => {
      const latest = editingRef.current
      if (!latest) return Date.now()
      setDraftState('saving')
      await api.saveBibleDraft(projectId!, { bible: latest, expected_version: currentEditVersion })
      return Date.now()
    },
    8000,
    [projectId && dirty ? projectId : null, currentEditVersion],
    { refreshOnFocus: false },
  )
  const {
    data: polledRefsProgress,
    refresh: refreshRefsProgress,
  } = usePoll<RefsProgress>(
    () => api.refsProgress(projectId!),
    progress => (
      p?.bible_status === 'running'
      || p?.refs_status === 'running'
      || progress?.refs_status === 'running'
        ? 3500
        : 0
    ),
    [p?.id ?? null],
  )
  const refsProgress = p?.bible ? polledRefsProgress : null

  const resetCharacterList = () => {
    setCharSearch('')
    setCharFilters({ ...EMPTY_CHARACTER_FILTERS })
    setCharPage(0)
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLInputElement>('input[aria-label="搜索角色"]')?.focus()
    })
  }

  useLayoutEffect(() => {
    if (!dirty) {
      registerNavigationGuard(null, false)
      return
    }
    registerNavigationGuard(
      {
        title: '保留人物谱草稿并离开？',
        summary: `当前有 ${dirtyCount} 项未定稿修订`,
        message: '修订已自动保存在当前项目草稿中，离开不会发布新版本，也不会影响已生成资产。',
        details: ['下次返回人物谱时可继续编辑', '只有完成定稿后，下游才会采用这些修订'],
        confirmLabel: '保留草稿并离开',
        cancelLabel: '继续修订',
        onConfirm: () => trackBible('bible_navigation_guard', projectId || '', { result: 'leave' }),
        onCancel: () => trackBible('bible_navigation_guard', projectId || '', { result: 'stay' }),
      },
      true,
    )
    return () => registerNavigationGuard(null, false)
  }, [dirty, dirtyCount, projectId, registerNavigationGuard])

  useEffect(() => {
    if (charPage > charPageCount - 1) setCharPage(Math.max(0, charPageCount - 1))
  }, [charPage, charPageCount])

  useEffect(() => {
    if (!projectId) return
    try {
      const raw = window.sessionStorage.getItem(`prep-bible-focus:${projectId}`)
      if (!raw) return
      const focus = JSON.parse(raw) as { missing?: string }
      if (focus.missing === 'yes') {
        setListState(current => ({
          ...current,
          filters: { ...EMPTY_CHARACTER_FILTERS, ...(current.filters as Partial<CharacterFilterState>), missing: 'yes' },
          page: 0,
        }))
      }
      window.sessionStorage.removeItem(`prep-bible-focus:${projectId}`)
    } catch { /* ignore invalid focus payload */ }
  }, [projectId, setListState])

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (listState.scrollY > 0) window.scrollTo({ top: listState.scrollY, behavior: 'auto' })
    })
    return () => window.cancelAnimationFrame(frame)
    // Only restore once for this page instance; subsequent scroll is user-driven.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    let ticking = false
    const saveScroll = () => {
      if (ticking) return
      ticking = true
      window.requestAnimationFrame(() => {
        ticking = false
        setListState(current => current.scrollY === window.scrollY
          ? current
          : { ...current, scrollY: window.scrollY })
      })
    }
    window.addEventListener('scroll', saveScroll, { passive: true })
    return () => {
      window.removeEventListener('scroll', saveScroll)
      setListState(current => ({ ...current, scrollY: window.scrollY }))
    }
  }, [setListState])

  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  useEffect(() => {
    editingRef.current = editing
  }, [editing])

  useEffect(() => {
    if (!dirty) return
    if (draftSaveError) {
      setDraftState('error')
    } else if (draftSavedAt !== null) {
      setDraftState('saved')
    }
  }, [dirty, draftSaveError, draftSavedAt])

  useEffect(() => {
    if (p?.bible_status === 'running' || p?.refs_status === 'running') {
      void refreshRefsProgress()
    }
  }, [p?.bible_status, p?.refs_status, refreshRefsProgress])

  useEffect(() => {
    if (!projectId || !editing || !dirty) return
    const key = bibleDraftKey(projectId, currentEditVersion)
    try {
      window.localStorage.setItem(key, JSON.stringify({
        bible: editing,
        bible_version: currentEditVersion,
        updated_at: Date.now(),
      }))
    } catch { /* local backup is best-effort */ }
  }, [projectId, editing, dirty, currentEditVersion])

  if (error && !p) return <QueryState loading={false} error={error} status={status} hasData={false} objectName="人物谱" onRetry={refresh}>{null}</QueryState>
  if (!p) return <QueryState loading={loading !== false} error={null} hasData={false} objectName="人物谱" onRetry={refresh}>{null}</QueryState>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    try { await fn(); if (doneMsg) toast(doneMsg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { busyRef.current = false; setBusy(false) }
  }

  const bible = editing ?? p.bible
  const indexedChars = (bible?.characters ?? []).map((c, i) => ({ c, i }))
  const filteredChars = indexedChars.filter(({ c }) =>
    matchCharacterFilters(c, charQuery, charFilters, characterFilterMeta(p, c)),
  ).sort((left, right) => compareCharacters(left.c, right.c, charFilters.sort, p))
  const curCharPage = Math.min(charPage, charPageCount - 1)
  const pagedChars = pageSize > 0
    ? filteredChars.slice(curCharPage * pageSize, curCharPage * pageSize + pageSize)
    : []
  const refsRunning = p.refs_status === 'running' || refsProgress?.refs_status === 'running'
  const generating = p.bible_status === 'running' || refsRunning
  const visualStyleDisplayName = bible?.world.visual_style_canonical?.trim() || '未设置统一画风'
  const paramsCharacter = paramsCharacterName
    ? bible?.characters.find(character => character.name === paramsCharacterName) ?? null
    : null
  const prepStatuses: Partial<Record<'bible' | 'scenes' | 'episodes', PrepStepStatus>> = {
    bible: bibleStepStatus(p),
    scenes: sceneStepStatus(p),
    episodes: episodeStepStatus(p),
  }
  const characterRoles = Array.from(new Set((bible?.characters ?? []).map(character => character.role).filter(Boolean)))
  const timelineQuery = timelineCharacter.trim()
  const timelineNames = (bible?.characters ?? []).map(character => character.name).filter(Boolean)
  const filteredTimeline = (p.key_timeline ?? []).filter(item => {
    if (!timelineQuery) return true
    return item.includes(timelineQuery)
      || timelineNames.some(name => name.includes(timelineQuery) && item.includes(name))
  })

  /**
   * 预检后立即用返回的 quote_id 自动确认，不再弹窗等用户手动点「确认并开始」
   * （2026-08-29 用户拍板：删除生成前的费用确认弹窗；后端 confirm+quote_id 契约
   * 不变，_issue_payment_quote/_validate_payment_quote 仍在，quote 的幂等重放
   * 能力原样保留，只是前端不再让用户手动点一次确认）。
   */
  const openPayment = async (
    precheckBody: { character?: string; characters?: string[]; resume?: boolean; view_role?: string },
    action: (quote: RefsCostPrecheck) => Promise<void>,
    precheckLoader?: () => Promise<PaymentQuote>,
  ) => {
    await act(async () => {
      const quote = precheckLoader
        ? await precheckLoader()
        : await api.refsPrecheck(p.id, precheckBody)
      trackBible('bible_payment_precheck', p.id, { action: quote.action, result: 'auto_confirmed' })
      await action(quote)
    })
  }

  const undoEdit = () => {
    const snapshot = undoStack[0]
    if (!snapshot) return
    setEditing(cloneBible(snapshot))
    setUndoStack(stack => stack.slice(1))
  }

  const beginRevision = async () => {
    if (!p.bible) return
    setEditBaseVersion(p.bible_version ?? 0)
    setEditBaseBible(cloneBible(p.bible))
    setUndoStack([])
    setDraftState('idle')
    setEditing(cloneBible(p.bible))
    try {
      const draft = await api.getBibleDraft(p.id)
      if (draft.bible_version === (p.bible_version ?? 0) && isBibleDraft(draft.draft)) {
        setEditing(cloneBible(draft.draft))
        toast('已载入上次未定稿草稿')
        return
      }
    } catch {
      // Fall through to local backup.
    }
    try {
      const raw = window.localStorage.getItem(bibleDraftKey(p.id, p.bible_version ?? 0))
      if (!raw) return
      const parsed = JSON.parse(raw) as { bible?: unknown; bible_version?: number }
      if (parsed.bible_version === (p.bible_version ?? 0) && isBibleDraft(parsed.bible)) {
        setEditing(cloneBible(parsed.bible))
        toast('已载入本地草稿备份')
      }
    } catch { /* ignore invalid local backup */ }
  }

  /**
   * 人物谱页与场景库页共用的「首次/全量重生成」路径：POST /projects/{id}/bible
   * （重路径，含 LLM）。人物谱生成成功后，后端 _bible_task 会无条件依次触发
   * 定妆照、场景清单（pending_scene_regen 票据）、场景图——本页确认一次即
   * 拿到全部四类产物，不需要用户再去场景库页点第二次。
   */
  const startBibleAfterStyle = async (styleName: string) => {
    await openPayment(
      {},
      async (quote) => {
        await api.generateBible(p.id, {
          confirm: true,
          quote_id: quote.quote_id,
          idempotency_key: quote.quote_id,
          style_name: styleName,
        })
        toast('人物谱生成已开始；完成后会自动接续生成定妆照、场景清单与场景图')
      },
      () => api.bibleGeneratePrecheck(p.id, { style_name: styleName }),
    )
  }

  const startBible = async () => {
    styleActionRef.current = 'full_regen'
    await styleDialog.openStyleDialog(p.bible_style_name)
  }

  const startStyleOnly = async () => {
    styleActionRef.current = 'style_only'
    await styleDialog.openStyleDialog(p.bible_style_name)
  }

  /**
   * 「更换统一画风（不改人物设定）」的轻量路径：只切换项目风格字段
   * （POST /bible/style，不重生成人物谱内容、不调模型）。预检拿到合并报价后
   * 立即用 quote_id 自动确认，不再弹窗等用户手动点「确认并开始」——后端在
   * 同一次请求里发起人物定妆照与场景图两条生成线，不是本页自己排队调用两个
   * 端点，那样任一步失败或页面被关掉，另一条线就发不出去了。
   */
  const submitStyleOnly = async (styleName: string) => {
    await act(async () => {
      const outcome = await applyStyleRegen(p.id, styleName, p.bible_version ?? 0)
      if (outcome.kind === 'unchanged') {
        toast(`统一画风仍为「${styleName}」，无需变更`)
        return
      }
      if (outcome.kind === 'idempotent_replay') {
        toast('该次风格切换已经处理过，未重复触发生成')
        return
      }
      const parts: string[] = []
      parts.push(outcome.refsStarted ? '定妆照已开始按新画风重新生成' : `定妆照未能启动：${outcome.refsError || '请重试'}`)
      if (outcome.sceneBibleReady) {
        parts.push(outcome.sceneRefsStarted ? '场景图已开始按新画风重新生成' : `场景图未能启动：${outcome.sceneRefsError || '请到场景库重试'}`)
      } else {
        parts.push('场景清单尚未生成，请先在「场景库」准备场景清单，完成后可单独按新画风生成场景图')
      }
      toast(parts.join('；'), !outcome.refsStarted)
    })
  }

  const retryRefs = async () => {
    await openPayment(
      { resume: true },
      async (quote) => {
        await api.generateRefs(p.id, {
          resume: true,
          confirm: true,
          quote_id: quote.quote_id,
          idempotency_key: quote.quote_id,
        })
        toast(`已开始补齐缺失的定妆照（共 ${quote.character_count} 个角色），已有成品会保留`)
      },
      async () => {
        const gaps = await api.refsGaps(p.id)
        return gaps.precheck
      },
    )
  }

  const restartRefsWithLatestSettings = async () => {
    if (dirty) {
      toast('请先定稿当前人物谱修订，再按最新画风批量重新生成', true)
      return
    }
    await openPayment(
      { resume: false },
      async (quote) => {
        await api.generateRefs(p.id, {
          resume: false,
          confirm: true,
          quote_id: quote.quote_id,
          idempotency_key: quote.quote_id,
        })
        toast(`已按最新人物设定与画风开始批量重新生成全部 ${quote.character_count} 个角色的定妆照；新包结构完整前保留旧成品`)
      },
    )
  }

  const skipToEpisodes = async () => {
    try {
      let names: string[] = []
      try {
        const progress = await refreshRefsProgress()
        if (progress) {
          names = progressProblemNames(progress)
        } else {
          const gaps = await api.refsGaps(p.id)
          names = (gaps.items ?? [])
            .map(item => String(item.character || ''))
            .filter(Boolean)
        }
      } catch {
        const gaps = await api.refsGaps(p.id)
        names = (gaps.items ?? [])
          .map(item => String(item.character || ''))
          .filter(Boolean)
      }
      if (names.length > 0) {
        setSkipConfirm({ count: names.length, names })
        return
      }
      go('episodes', p.id)
    } catch (e: unknown) {
      toast((e as Error).message, true)
    }
  }

  // 决策③：三道内容质量人工确认门取消。定稿人物谱不再停下来等一次额外的“批准影响”点击——
  // 影响预检仍然照算（版本冲突等正确性问题必须能拦住），只是算完立即自动放行写入，
  // 不再弹窗等待人工二次确认。清库/删除类破坏性操作的确认（如项目/剧本删除）不在此列，均保留。
  const finalizeBible = async () => {
    if (!editing) return
    setBusy(true)
    try {
      const preview = await api.bibleImpactPreview(p.id, {
        bible: editing,
        expected_version: currentEditVersion,
      })
      await saveBible(preview.fingerprint)
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 409) {
        const detail = e.detail as {
          code?: string
          message?: string
          character_names?: string[]
          current_version?: number
          server_bible?: Bible | null
        } | undefined
        if (detail?.code === 'BIBLE_VERSION_CONFLICT') {
          trackBible('bible_conflict', p.id, { conflict: true, source: 'impact_preview' })
          setConflict({
            message: detail.message || e.message,
            current_version: detail.current_version,
            character_names: detail.character_names,
            server_bible: detail.server_bible,
          })
          return
        }
      }
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const saveBible = async (fingerprint: string) => {
    if (!editing) return
    setBusy(true)
    try {
      const r = await api.updateBible(p.id, {
        bible: editing,
        expected_version: currentEditVersion,
        confirm: true,
        impact_preview_fingerprint: fingerprint,
      }) as {
        style_changed?: boolean
        purged?: { versions: number } | null
        impact?: ImpactSummary
      }
      setEditing(null)
      setEditBaseVersion(null)
      setEditBaseBible(null)
      setUndoStack([])
      try {
        window.localStorage.removeItem(bibleDraftKey(p.id, currentEditVersion))
      } catch { /* ignore */ }
      toast(r.style_changed
        ? `画风已变更：旧画风定妆照与已生成视频（${r.purged?.versions ?? 0} 个版本）已全部作废，请重新生成定妆照后再生成视频`
        : `人物谱已定稿；${r.impact?.stale_descendant_ids?.length ?? 0} 个下游证据已标记失效`)
      refresh()
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 409) {
        const detail = e.detail as {
          code?: string
          message?: string
          character_names?: string[]
          current_version?: number
          preview?: BibleImpactPreview
          server_bible?: Bible | null
        } | undefined
        if (detail?.code === 'BIBLE_VERSION_CONFLICT') {
          trackBible('bible_conflict', p.id, { conflict: true, source: 'bible_save' })
          setConflict({
            message: detail.message || e.message,
            current_version: detail.current_version,
            character_names: detail.character_names,
            server_bible: detail.server_bible,
          })
          return
        }
        if (detail?.code === 'IMPACT_PREVIEW_STALE' && detail.preview) {
          // 指纹在两次请求之间过期是正确性校验（防止用旧快照覆盖新写入），不是需要人工
          // 点头的内容质量门；直接用服务端刷新出的最新指纹自动重试一次。
          await saveBible(detail.preview.fingerprint)
          return
        }
      }
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const saveCharacterDraft = async (character: Character, fingerprint?: string) => {
    if (!editing) return
    setBusy(true)
    try {
      const result = await api.saveCharacter(p.id, character.name, {
        character,
        expected_version: currentEditVersion,
        impact_preview_fingerprint: fingerprint,
        confirm: !!fingerprint,
      })
      const nextCharacter = result.character ?? character
      setEditing(current => current ? replaceCharacter(current, character.name, nextCharacter) : current)
      setEditBaseBible(current => current ? replaceCharacter(current, character.name, nextCharacter) : current)
      if (typeof result.bible_version === 'number') setEditBaseVersion(result.bible_version)
      toast(`「${character.name}」已保存为角色级修订`)
      refresh()
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 409) {
        const detail = e.detail as {
          code?: string
          message?: string
          preview?: BibleImpactPreview
          impact_preview?: BibleImpactPreview
        } | undefined
        if (detail?.code === 'IMPACT_CONFIRM_REQUIRED') {
          let preview = detail.preview || detail.impact_preview || null
          if (!preview) {
            try {
              preview = await api.bibleImpactPreview(p.id, {
                bible: p.bible ? replaceCharacter(cloneBible(p.bible), character.name, character) : editing,
                expected_version: currentEditVersion,
              })
            } catch (previewError) {
              toast((previewError as Error).message, true)
              return
            }
          }
          // 决策③：不再弹窗等待人工点头，算完影响立即自动放行、原样重试一次写入。
          await saveCharacterDraft(character, preview.fingerprint)
          return
        }
        if (detail?.code === 'BIBLE_VERSION_CONFLICT') {
          trackBible('bible_conflict', p.id, { conflict: true, source: 'character_save' })
          const conflictDetail = e.detail as {
            message?: string
            current_version?: number
            character_names?: string[]
            server_bible?: Bible | null
          }
          setConflict({
            message: conflictDetail.message || e.message,
            current_version: conflictDetail.current_version,
            character_names: conflictDetail.character_names,
            server_bible: conflictDetail.server_bible,
          })
          return
        }
      }
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const abandonEditing = () => {
    const keptAsDraft = dirty
    setEditing(null)
    setEditBaseVersion(null)
    setEditBaseBible(null)
    setUndoStack([])
    if (keptAsDraft) toast('未定稿修订已保留，可稍后恢复继续编辑')
  }

  const hasRefGaps = !!refsProgress && (refsProgress.failed > 0 || refsProgress.missing > 0)
  const conflictServerNames = new Set((conflict?.server_bible?.characters ?? []).map(character => character.name))
  const conflictLocalNames = new Set((editing?.characters ?? []).map(character => character.name))
  const conflictOnlyServer = [...conflictServerNames].filter(name => !conflictLocalNames.has(name))
  const conflictOnlyLocal = [...conflictLocalNames].filter(name => !conflictServerNames.has(name))
  const conflictBoth = [...conflictLocalNames].filter(name => conflictServerNames.has(name))
  const conflictMerge = conflict?.server_bible && editing && editBaseBible
    ? mergeBibleThreeWay(editBaseBible, editing, conflict.server_bible)
    : null

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / {formatBookTitle(p.name)}</div>
        <PrepSubnav
          current="bible"
          statuses={prepStatuses}
          onProblemClick={(key) => {
            if (key === 'bible') {
              setCharFilters({ ...EMPTY_CHARACTER_FILTERS, missing: 'yes' })
              setCharPage(0)
            }
          }}
        />
        <h1>人物谱 <span className="sub">角色资产与定妆版本中心 · 保持跨镜头、跨分集一致</span></h1>
        <div className="stage-model-picker-row">
          <StageTextModelPicker
            projectId={projectId!}
            field="bible_text_provider"
            label="文本模型（项目）"
            title="世界书生成使用的文本模型。作用域：整个项目；不选则使用系统默认文本模型，只影响之后新发起的谱写，不影响已生成内容"
            value={p.bible_text_provider}
            choices={p.text_model_choices ?? []}
            disabled={busy}
            toast={toast}
            onSaved={() => void refresh({ force: true })}
          />
        </div>
        <hr className="rule" />
      </header>

      <section className="card">
        <h3>原著 <span className="hint">{(p.novel_chars / 10000).toFixed(1)} 万字 · {p.chapter_count ?? p.chapters?.length ?? 0} 章</span></h3>
        <div className="library-action-row">
          {!p.bible && !generating && (
            <button className="btn primary" disabled={busy}
              title="确认画风后会依次自动生成人物谱、角色定妆照、场景清单与场景图；无需再到场景库页操作。"
              aria-label={busy
                ? '选择画风并生成人物谱与场景库，暂不可用：正在处理上一项操作'
                : p.bible_status === 'failed' ? '重新选择画风并生成人物谱与场景库' : '选择画风并生成人物谱与场景库'}
              onClick={() => void startBible()}>
              {p.bible_status === 'failed' ? '重新选择画风并生成人物谱与场景库' : '选择画风并生成人物谱与场景库'}
            </button>
          )}
          {p.bible && !generating && (
            <>
              <button
                className="btn primary"
                disabled={busy || dirty}
                title={dirty ? '请先定稿当前人物谱修订' : '按当前人物设定与统一画风批量重新生成全部角色定妆照'}
                aria-label={busy || dirty
                  ? `按最新设定批量重新生成定妆照，暂不可用：${busy ? '正在处理上一项操作' : '请先定稿当前人物谱修订'}`
                  : '按最新设定批量重新生成定妆照；点击后立即提交'}
                onClick={() => void restartRefsWithLatestSettings()}
              >
                按最新设定批量重新生成定妆照
              </button>
              <button
                className="btn"
                disabled={busy || dirty}
                title={dirty ? '请先定稿当前人物谱修订' : '重新选择统一画风，将重新生成人物谱与定妆照（会重新生成人物设定本身）'}
                onClick={() => void startBible()}
              >
                重新生成人物谱并更换画风
              </button>
              <button
                className="btn ghost"
                disabled={busy || dirty}
                title={dirty ? '请先定稿当前人物谱修订' : '项目级设置：只切换统一画风，不改动人物设定；确认后将直接重新生成定妆照与场景图'}
                aria-label={busy || dirty
                  ? `更换统一画风，暂不可用：${busy ? '正在处理上一项操作' : '请先定稿当前人物谱修订'}`
                  : '更换统一画风（保留人物设定，重新生成定妆照与场景图）'}
                onClick={() => void startStyleOnly()}
              >
                更换统一画风（不改人物设定）
              </button>
            </>
          )}
          {p.bible && (p.refs_status === 'failed' || hasRefGaps) && !generating && (
            <>
              <button className="btn ghost" disabled={busy}
                aria-label={busy ? '补齐缺失的定妆照，暂不可用：正在处理上一项操作' : '补齐缺失的定妆照'}
                onClick={() => void retryRefs()}>
                补齐缺失的定妆照
              </button>
              <button className="btn ghost" disabled={busy}
                aria-label={busy ? '暂时跳过并继续分集，暂不可用：正在处理上一项操作' : '暂时跳过并继续分集'}
                onClick={() => void skipToEpisodes()}>
                暂时跳过，继续分集
              </button>
            </>
          )}
          <WorldbuildingStatus
            project={p}
            running={generating}
            busy={busy}
            setBusy={setBusy}
            toast={toast}
            refresh={refresh}
            refreshRefsProgress={refreshRefsProgress}
          />
          {p.bible && <span className="stamp green">第 {p.bible_version ?? 1} 稿</span>}
          {dirty && <span className="stamp gold">未保存修订 · {dirtyCount} 项</span>}
          {editing && draftState === 'saving' && <span className="stamp grey">草稿保存中</span>}
          {editing && draftState === 'saved' && <span className="stamp green">草稿已自动保存</span>}
          {editing && draftState === 'error' && <span className="stamp red">草稿保存失败，已保留本地备份</span>}
          {p.bible_evidence && <EvidenceDrawer evidence={p.bible_evidence} label="查看人物谱质检依据" />}
        </div>
        {refsProgress && (
          <div className="refs-progress-strip" role="status" aria-label="定妆进度">
            <span>
              {summarizeProgress(refsProgress)}
              {refsProgress.refs_target ? ` · 当前角色：${refsProgress.refs_target}` : ''}
              {refsProgress.updated_at ? ` · 更新：${new Date(refsProgress.updated_at * 1000).toLocaleTimeString()}` : ''}
            </span>
            <div className="refs-progress-bar">
              <i style={{ width: `${refsProgress.total ? Math.round((refsProgress.ready / refsProgress.total) * 100) : 0}%` }} />
            </div>
            {!generating && !!refsProgress.items?.length && (
              <div className="refs-stop-checklist" aria-label="停止后的定妆清单">
                {(['ready', 'failed', 'missing', 'deferred', 'blocked'] as const).map(status => {
                  const items = refsProgress.items.filter(item => item.status === status)
                  if (!items.length) return null
                  const label = status === 'ready' ? '已完成'
                    : status === 'failed' ? '失败'
                    : status === 'missing' ? '缺失'
                    : status === 'deferred' ? '暂缓'
                    : '外观未通过'
                  return (
                    <div key={status}>
                      <b>{label} {items.length}</b>
                      <p>{items.slice(0, 12).map(item => item.character).join('、')}{items.length > 12 ? ` 等 ${items.length} 个` : ''}</p>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}
        {!generating && p.bible && (
          <div className="hint library-note">
            更新人物谱角色设定并定稿后，可选择角色重新生成定妆照；如需更换统一画风，请重新生成人物谱并选择风格。新图通过质检前不会替换已采用成品。
          </div>
        )}
        {p.bible_status === 'failed' && (
          <OperationError
            title="人物谱生成未完成"
            message={p.bible_error}
            guidance="失败结果没有发布；原著、旧人物谱和已生成资产保持不变。可重新生成，若持续失败可展开详情排查。"
          />
        )}
        {p.bible_status === 'warning' && (
          <OperationError
            title="人物谱有待处理问题"
            message={p.bible_error}
            guidance="下游制作已暂停，现有版本未被覆盖。请按提示修订后重新定稿。"
            variant="warning"
            detailLabel="查看问题详情"
          />
        )}
      </section>

      {bible && (
        <section className="card bible-library">
          <div className="card-heading-row">
            <h3>世界观 <span className="hint">时代：{bible.world.era} · 类型：{bible.world.genre}</span></h3>
            <div className="card-heading-actions">
              {!editing
                ? <button className="btn small" onClick={() => void beginRevision()}>修订人物谱</button>
                : <>
                  <button className="btn small primary" disabled={busy}
                    aria-label={busy ? '定稿人物谱，暂不可用：正在处理上一项操作' : '定稿人物谱'}
                    onClick={() => void finalizeBible()}>定稿人物谱</button>
                  <button className="btn small" disabled={!undoStack.length}
                    aria-label={!undoStack.length ? '撤销上次修改，暂不可用：还没有可撤销的修改' : '撤销上次修改'}
                    onClick={undoEdit}>撤销上次修改</button>
                  <button className="btn small ghost" onClick={abandonEditing}>暂存并退出编辑</button>
                </>}
            </div>
          </div>
          <label className="f">统一画面风格（会用于每个镜头）</label>
          <div style={{ fontSize: 14, background: 'rgba(181,68,52,0.05)', borderLeft: '3px solid var(--cinnabar)', padding: '8px 12px', borderRadius: '0 6px 6px 0', lineHeight: 1.9 }}>
            {visualStyleDisplayName}
          </div>
          {editing && <p className="hint">画风提示词由后端统一管理；如需更换画风，请重新生成人物谱并选择新的风格。</p>}
          <div style={{ height: 16 }} />
          <div className="library-note-row">
            {refsRunning && <span className="stamp gold">定妆中</span>}
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              启动后会先为全部角色生成初始定妆照；随后在分镜阶段按集判断角色外观是否相比当前定妆照大变，大变才图生图重绘并切分适用集，新登场重要人物会自动补人物卡并生成定妆照
            </span>
          </div>
          {p.refs_status === 'failed' && (
            <OperationError
              title="定妆照生成未完成"
              message={p.refs_error}
              guidance="已经完成的定妆照会保留。可重试全部缺口，或按角色单独补齐失败项。"
            />
          )}
          <div className="library-toolbar">
            <SearchField value={charSearch} onChange={value => { setCharSearch(value); setCharPage(0) }}
              placeholder="搜索角色名…" ariaLabel="搜索角色" className="library-search" />
            <CharacterFilters
              value={charFilters}
              onChange={value => { setCharFilters(value); setCharPage(0) }}
              roles={characterRoles}
            />
            <span className="library-result-count" role="status">共 {bible.characters.length} 个角色{hasCharacterCriteria ? ` · 当前显示 ${filteredChars.length}` : ''}</span>
            <NominateCharacterEntry projectId={p.id} onFocusCharacter={name => setCharSearch(name)} />
          </div>
          <div ref={characterGridRef} className="figure-grid">
            {pagedChars.map(({ c, i }: { c: Character; i: number }) => {
              const portraits = c.portraits ?? []
              const hasPortraitImage = portraits.some(portrait =>
                (!!portrait.image_url || (portrait.views ?? []).some(view => !!view.image_url)),
              )
              const fitting = refsRunning && (
                p.refs_target === c.name
                || (!p.refs_target && portraits.some(portrait => portrait.pack_status === 'generating'))
              )
              const availability = portraitAvailability(c, fitting)
              const stamp = availabilityStamp(availability)
              const active = currentPortrait(c)
              const baseCharacter = p.bible?.characters.find(character => character.name === c.name)
              const characterDirty = editing ? characterChanged(editing.characters[i], baseCharacter) : false
              return (
              <article key={c.name} className="figure character-card">
                <div className="f-name">{c.name} <span className="f-role">{c.role}</span>
                  <span className={`stamp ${stamp.color}`}>{stamp.label}</span>
                </div>
                {(c.ref_image_url || hasPortraitImage) && (
                  <CharacterPortraitGallery
                    projectId={p.id}
                    character={c}
                    fitting={fitting}
                    disabled={busy || generating}
                    onChanged={refresh}
                    onPayRequest={openPayment}
                  />
                )}
                <div className="asset-card-actions">
                  <button className="btn small" type="button"
                    onClick={() => setQaDetail({ characterName: c.name, portrait: active })}>
                    定妆候选
                  </button>
                  <button className="btn small" type="button" disabled={!characterCompareImages(c).length}
                    aria-label={!characterCompareImages(c).length ? `放大对比${c.name}定妆照，暂不可用：当前没有可对比图片` : `放大对比${c.name}定妆照`}
                    onClick={() => setCompareDetail({ title: `${c.name} · 定妆图对比`, images: characterCompareImages(c) })}>
                    放大对比
                  </button>
                </div>
                {editing && (
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', margin: '8px 0' }}>
                    <button
                      type="button"
                      className="btn small primary"
                      disabled={busy || !characterDirty}
                      aria-label={busy || !characterDirty
                        ? `保存${c.name}的角色修订，暂不可用：${busy ? '正在处理上一项操作' : '该角色尚无修改'}`
                        : `保存${c.name}的角色修订`}
                      onClick={() => { void saveCharacterDraft(editing.characters[i]) }}
                    >
                      保存此角色
                    </button>
                    {characterDirty && <span className="hint">仅提交当前角色，其他本地修订保留在草稿中。</span>}
                  </div>
                )}
                <div className="asset-params-action">
                  <button className="asset-params-trigger" type="button" onClick={() => setParamsCharacterName(c.name)}>
                    角色设定与生成参数 <span aria-hidden="true">→</span>
                  </button>
                </div>
              </article>
            )})}
          </div>
          {pageSize > 0 && !pagedChars.length && (
            <div className="library-filter-empty" role="status">
              <b>{hasCharacterCriteria ? '没有符合当前条件的角色' : '人物谱暂无角色'}</b>
              <p>{hasCharacterCriteria
                ? `${charQuery ? `搜索“${charQuery}”` : '当前筛选'}未命中；清除条件后可恢复全部 ${bible.characters.length} 个角色。`
                : '可重新生成人物谱，或进入修订补充角色。'}</p>
              {hasCharacterCriteria && <button type="button" className="btn small" onClick={resetCharacterList}>清除搜索与筛选</button>}
            </div>
          )}
          {charPageCount > 1 && (
            <div className="library-pagination" aria-label="角色分页">
              <button className="btn small" disabled={curCharPage <= 0}
                aria-label={curCharPage <= 0 ? '上一页，暂不可用：当前已是第一页' : '上一页'}
                onClick={() => setCharPage(curCharPage - 1)}>← 上一页</button>
              <span>第 {curCharPage + 1} / {charPageCount} 页</span>
              <button className="btn small" disabled={curCharPage >= charPageCount - 1}
                aria-label={curCharPage >= charPageCount - 1 ? '下一页，暂不可用：当前已是最后一页' : '下一页'}
                onClick={() => setCharPage(curCharPage + 1)}>下一页 →</button>
            </div>
          )}
        </section>
      )}

      {!!p.key_timeline?.length && (
        <section className="card world-card">
          <h3>全书关键事件线 <span className="hint">防长篇伏笔丢失</span></h3>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10 }}>
            <SearchField value={timelineCharacter} onChange={setTimelineCharacter}
              placeholder="按角色过滤事件…" ariaLabel="按角色过滤关键事件" className="library-search" />
            <span className="hint">输入角色名后只显示提到该角色的事件</span>
          </div>
          <ol style={{ paddingLeft: 22, fontSize: 13.5, color: 'var(--ink-soft)' }}>
            {filteredTimeline.map((k, i) => {
              const names = timelineNames.filter(name => k.includes(name))
              const parts: Array<string | { target: number; kind: 'episode' | 'chapter'; text: string }> = []
              let lastIndex = 0
              for (const match of k.matchAll(/第([零一二两三四五六七八九十\d]+)(集|章)/g)) {
                const target = cnEpisodeToNumber(match[1])
                if (!target) continue
                if (match.index !== undefined && match.index > lastIndex) parts.push(k.slice(lastIndex, match.index))
                parts.push({ target, kind: match[2] === '章' ? 'chapter' : 'episode', text: match[0] })
                lastIndex = (match.index ?? 0) + match[0].length
              }
              if (lastIndex < k.length) parts.push(k.slice(lastIndex))
              if (!parts.length) parts.push(k)
              return (
                <li key={`${i}:${k}`}>
                  {parts.map((part, index) => typeof part === 'string'
                    ? <span key={index}>{part}</span>
                    : (
                      <button
                        key={`${index}:${part.kind}:${part.target}`}
                        type="button"
                        className="timeline-episode-link"
                        onClick={() => {
                          if (part.kind === 'chapter') {
                            go('reader', p.id, null, part.target)
                          } else {
                            window.sessionStorage.setItem(`prep-episodes-focus:${p.id}`, JSON.stringify({ episode_no: part.target }))
                            go('episodes', p.id)
                          }
                        }}
                      >
                        {part.text}
                      </button>
                    ))}
                  {!!names.length && (
                    <span style={{ marginLeft: 8 }}>
                      {names.slice(0, 4).map(name => (
                        <button
                          key={name}
                          type="button"
                          className="stamp grey stamp-button"
                          onClick={() => {
                            setTimelineCharacter(name)
                            setCharSearch(name)
                            setCharPage(0)
                            window.requestAnimationFrame(() => {
                              document.querySelector('.figure-grid')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                            })
                          }}
                        >
                          {name}
                        </button>
                      ))}
                    </span>
                  )}
                </li>
              )
            })}
          </ol>
          {!filteredTimeline.length && (
            <div className="empty">{timelineQuery ? `没有包含「${timelineQuery}」的关键事件` : '暂无关键事件'}</div>
          )}
        </section>
      )}
      <VisualStyleDialog
        open={styleDialog.styleOpen}
        loading={styleDialog.styleLoading}
        error={styleDialog.styleError}
        options={styleDialog.styleOptions}
        selected={styleDialog.selectedStyle}
        scopeNote={styleActionRef.current === 'style_only'
          ? '确认后将直接重新生成「定妆照 + 场景图」；人物设定本身不会重新生成。'
          : '确认后将在本页重新生成人物谱与定妆照；风格确定后场景库的场景图也会一并重新生成。'}
        onSelect={styleDialog.setSelectedStyle}
        onClose={styleDialog.closeStyleDialog}
        onConfirm={() => {
          if (!styleDialog.selectedStyle) {
            styleDialog.setStyleError('请先选择统一画面风格')
            return
          }
          const chosen = styleDialog.selectedStyle
          styleDialog.closeStyleDialog()
          if (styleActionRef.current === 'style_only') {
            void submitStyleOnly(chosen)
          } else {
            void startBibleAfterStyle(chosen)
          }
        }}
      />
      {skipConfirm && (
        <SkipConfirmDialog
          data={skipConfirm}
          onClose={() => setSkipConfirm(null)}
          onConfirm={() => {
            setSkipConfirm(null)
            go('episodes', p.id)
          }}
        />
      )}
      {conflict && (
        <ConflictDialog
          conflict={conflict}
          onlyServer={conflictOnlyServer}
          onlyLocal={conflictOnlyLocal}
          both={conflictBoth}
          fieldConflicts={conflictMerge?.conflicts ?? []}
          canMerge={!!conflict.server_bible && !!editing && !!editBaseBible}
          onClose={() => setConflict(null)}
          onMerge={(choices) => {
            if (!editing || !conflict.server_bible || !editBaseBible) return
            const merged = mergeBibleThreeWay(editBaseBible, editing, conflict.server_bible, choices)
            setEditing(merged.bible)
            setEditBaseBible(cloneBible(conflict.server_bible))
            setEditBaseVersion(conflict.current_version ?? editBaseVersion ?? p.bible_version ?? 0)
            setConflict(null)
            toast('已完成三方字段合并，请复核后重新定稿')
          }}
          onRefresh={() => {
            setConflict(null)
            setEditing(null)
            setEditBaseVersion(null)
            setEditBaseBible(null)
            setUndoStack([])
            refresh()
          }}
        />
      )}
      {qaDetail && (
        <CharacterQaPanel
          projectId={p.id}
          characterName={qaDetail.characterName}
          portrait={qaDetail.portrait}
          onChanged={refresh}
          onClose={() => setQaDetail(null)}
        />
      )}
      {compareDetail && (
        <ImageCompareModal
          title={compareDetail.title}
          images={compareDetail.images}
          onClose={() => setCompareDetail(null)}
        />
      )}
      {paramsCharacter && (
        <GenerationParamsDialog
          title={`${paramsCharacter.name} · 角色设定与生成参数`}
          subtitle="查看角色设定、调整当前定妆提示词，或重新生成当前角色定妆照。"
          onClose={() => setParamsCharacterName(null)}
        >
          <div className="character-param-summary">
            <div><b>性格</b><p>{paramsCharacter.personality || '未设置'}</p></div>
            <div><b>语风</b><p>{paramsCharacter.speech_style || '未设置'}</p></div>
            <div className="wide"><b>关系</b><p>{paramsCharacter.relationships.length
              ? paramsCharacter.relationships.map(r => `${r.relation}→${r.to}`).join('；')
              : '未设置'}</p></div>
          </div>
          <PortraitBlock projectId={p.id} character={paramsCharacter}
            disabled={busy || refsRunning} onChanged={refresh}
            regenerate={() => openPayment(
              { character: paramsCharacter.name },
              async (quote) => {
                await api.generateRefs(p.id, {
                  character: paramsCharacter.name,
                  confirm: true,
                  quote_id: quote.quote_id,
                  idempotency_key: quote.quote_id,
                })
                toast(`正在为「${paramsCharacter.name}」重新定妆`)
              },
            )} />
        </GenerationParamsDialog>
      )}
    </>
  )
}

function PortraitBlock({ projectId, character: c, disabled, onChanged, regenerate }: {
  projectId: string; character: Character; disabled: boolean
  onChanged: () => void; regenerate: () => void
}) {
  const { toast } = useNav()
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [restoreConfirm, setRestoreConfirm] = useState(false)
  const [discardConfirm, setDiscardConfirm] = useState(false)
  const isOverridden = !!(c.portrait_prompt_override || '').trim()
  const savedPrompt = c.portrait_prompt_override || c.portrait_prompt_effective || ''
  const draftChanged = draft !== null && draft !== savedPrompt
  const draftLen = (draft ?? '').trim().length
  const draftValid = draft !== null && (draftLen === 0 || (draftLen >= 10 && draftLen <= 400))
  const baseDisabledReason = saving
    ? '正在保存上一项修改'
    : disabled
      ? '当前有其他人物资产任务运行，请等待完成'
      : ''
  const saveAndRegenerateDisabledReason = baseDisabledReason
    || (!draftChanged ? '尚未修改定妆提示词' : '')
    || (!draftValid ? '提示词需为 10 至 400 字' : '')
    || (draftLen === 0 ? '提示词为空时只能保存为系统默认值' : '')
  const saveOnlyDisabledReason = saving
    ? '正在保存上一项修改'
    : !draftChanged
      ? '尚未修改定妆提示词'
      : !draftValid
        ? '提示词需为 10 至 400 字，或留空恢复系统默认值'
        : ''

  async function save(thenRegen: boolean, value?: string) {
    setSaving(true)
    try {
      const text = value ?? draft ?? ''
      const r = await api.setCharacterPortraitPrompt(projectId, c.name, { portrait_prompt: text })
      toast(r.reset_to_default ? `「${c.name}」定妆提示词已恢复系统默认值` : `「${c.name}」最新定妆提示词已保存`)
      setRestoreConfirm(false); setDiscardConfirm(false); setDraft(null); onChanged()
      if (thenRegen) regenerate()
    } catch (e: unknown) { toast((e as Error).message, true) }
    finally { setSaving(false) }
  }

  return (
    <div style={{ marginTop: 10 }}>
      <label className="f">当前定妆提示词{isOverridden ? ' · 用户已修改' : ' · 系统默认'}</label>
      {draft === null ? (
        <>
          <div className="prompt-source-chips" aria-label="定妆提示词组成">
            {promptSegments(c.portrait_prompt_effective).map(segment => (
              <span key={segment.label} title={segment.text}>
                <b>{segment.label}</b>{segment.text}
              </span>
            ))}
          </div>
          <div className="f-misc" style={{ background: 'rgba(91,114,83,0.06)', borderLeft: '3px solid var(--moss)', padding: '6px 10px', borderRadius: '0 6px 6px 0', fontSize: 12.5 }}>
            {c.portrait_prompt_effective}
          </div>
          <p className="hint">后续正面、3/4 面和侧面定妆均以这里保存的最新提示词为准。</p>
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button className="btn small" disabled={disabled || saving}
              aria-label={baseDisabledReason ? `修改定妆提示词，暂不可用：${baseDisabledReason}` : '修改定妆提示词'}
              onClick={() => { setDiscardConfirm(false); setDraft(savedPrompt) }}>修改定妆提示词</button>
            <button className="btn small" disabled={disabled || saving}
              aria-label={baseDisabledReason
                ? `${c.ref_image_url ? '重新生成当前定妆照' : '单独生成定妆照'}，暂不可用：${baseDisabledReason}`
                : c.ref_image_url ? '重新生成当前定妆照' : '单独生成定妆照'}
              onClick={regenerate}>
              {c.ref_image_url ? '重新生成当前定妆照' : '单独生成定妆照'}
            </button>
          </div>
        </>
      ) : (
        <>
          <textarea aria-label={`${c.name}定妆提示词`} rows={4} style={{ fontSize: 12.5 }} value={draft} onChange={e => setDraft(e.target.value)}
            placeholder="修改角色定妆提示词：角色外观、画风、姿态、背景……（10~400 字）" />
          <div style={{ fontSize: 12, color: draftValid ? 'var(--ink-faint)' : 'var(--cinnabar)', marginTop: 4 }}>
            {draftLen === 0 ? '留空并保存将恢复系统根据内部角色设定生成的默认值' : `${draftLen} / 10~400 字`}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button className="btn small primary" disabled={Boolean(saveAndRegenerateDisabledReason)}
              aria-label={saveAndRegenerateDisabledReason ? `保存并重新定妆，暂不可用：${saveAndRegenerateDisabledReason}` : '保存并重新定妆'}
              onClick={() => save(true)}>保存并重新定妆</button>
            <button className="btn small" disabled={Boolean(saveOnlyDisabledReason)}
              aria-label={saveOnlyDisabledReason ? `仅保存定妆提示词，暂不可用：${saveOnlyDisabledReason}` : '仅保存定妆提示词'}
              onClick={() => save(false)}>仅保存</button>
            {isOverridden && <button className="btn small" disabled={saving}
              aria-label={saving ? '恢复系统默认提示词，暂不可用：正在保存上一项修改' : '恢复系统默认提示词'}
              onClick={() => setRestoreConfirm(true)}>恢复系统默认值</button>}
            <button className="btn small ghost" disabled={saving}
              aria-label={saving ? '退出提示词编辑，暂不可用：正在保存上一项修改' : draftChanged ? '放弃提示词修改' : '退出提示词编辑'}
              onClick={() => {
                setRestoreConfirm(false)
                if (draftChanged) setDiscardConfirm(true)
                else setDraft(null)
              }}>{draftChanged ? '放弃修改' : '退出编辑'}</button>
          </div>
          {restoreConfirm && (
            <div className="inline-reset-confirm" role="status">
              <span><b>恢复系统默认提示词？</b>系统会根据内部角色设定与统一画风重新生成默认值，不生成图片、不扣费。</span>
              <div>
                <button className="btn small ghost" type="button" disabled={saving}
                  onClick={() => setRestoreConfirm(false)}>取消</button>
                <button className="btn small primary" type="button" disabled={saving}
                  onClick={() => { setRestoreConfirm(false); void save(false, '') }}>确认恢复</button>
              </div>
            </div>
          )}
          {discardConfirm && (
            <div className="inline-reset-confirm" role="status">
              <span><b>放弃尚未保存的提示词？</b>当前输入会恢复为已保存版本，定妆照和下游不会变化。</span>
              <div>
                <button className="btn small ghost" type="button" onClick={() => setDiscardConfirm(false)}>继续编辑</button>
                <button className="btn small danger" type="button"
                  onClick={() => { setDiscardConfirm(false); setDraft(null) }}>放弃修改</button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function portraitVersionLabel(portrait: Portrait): string {
  if (portrait.ep_start <= 0) return '历史初始定妆'
  if (!portrait.base_portrait_id) {
    return portrait.ep_start > 1 ? `第${portrait.ep_start}集首次定妆` : '初始定妆'
  }
  return `第${portrait.ep_start}集更新`
}

const VIEW_ROLE_LABELS: Record<string, string> = {
  front_full: '正面全身',
  three_quarter: '3/4 面',
  profile: '侧面',
  back_full: '背面全身',
  face_closeup: '面部特写',
}

const PRIMARY_PORTRAIT_VIEW_ROLES = ['front_full', 'three_quarter', 'profile']

export function currentPortraitViews(character: Character): PortraitView[] {
  const portrait = currentPortrait(character)
  const byRole = new Map<string, PortraitView>()
  for (const view of portrait?.views ?? []) {
    const role = view.view_role ?? ''
    if (!view.image_url || !PRIMARY_PORTRAIT_VIEW_ROLES.includes(role) || byRole.has(role)) continue
    byRole.set(role, view)
  }
  return PRIMARY_PORTRAIT_VIEW_ROLES
    .map(role => byRole.get(role))
    .filter((view): view is PortraitView => !!view)
}

type PortraitSlide = {
  key: string
  imageUrl: string
  portrait: Portrait | null
  versionIndex: number
  view: PortraitView | null
}

function CharacterPortraitGallery({ projectId, character, fitting, disabled, onChanged, onPayRequest }: {
  projectId: string
  character: Character
  fitting: boolean
  disabled?: boolean
  onChanged?: () => void
  onPayRequest: (
    precheckBody: { character?: string; characters?: string[]; resume?: boolean; view_role?: string },
    action: (quote: RefsCostPrecheck) => Promise<void>,
  ) => Promise<void>
}) {
  const { toast } = useNav()
  const trackRef = useRef<HTMLDivElement>(null)
  const [redoing, setRedoing] = useState<string | null>(null)
  const [canScrollBack, setCanScrollBack] = useState(false)
  const [canScrollForward, setCanScrollForward] = useState(false)
  const portrait = currentPortrait(character)
  const slides: PortraitSlide[] = []
  if (portrait) {
    const views = currentPortraitViews(character)
    if (views.length) {
      slides.push(...views.map(view => ({
        key: `${portrait.id}:${view.id}`,
        imageUrl: view.image_url!,
        portrait,
        versionIndex: 0,
        view,
      })))
    } else if (portrait.image_url) {
      slides.push({
        key: portrait.id || `${portrait.ep_start}:${portrait.image_url}`,
        imageUrl: portrait.image_url,
        portrait,
        versionIndex: 0,
        view: null,
      })
    }
  }
  if (!slides.length && character.ref_image_url) {
    slides.push({
      key: `${character.name}:legacy`,
      imageUrl: character.ref_image_url,
      portrait: null,
      versionIndex: 0,
      view: null,
    })
  }
  const count = slides.length

  useEffect(() => {
    const track = trackRef.current
    if (!track) return
    const sync = () => {
      const max = Math.max(0, track.scrollWidth - track.clientWidth)
      setCanScrollBack(track.scrollLeft > 2)
      setCanScrollForward(track.scrollLeft < max - 2)
    }
    const frame = window.requestAnimationFrame(sync)
    track.addEventListener('scroll', sync, { passive: true })
    window.addEventListener('resize', sync)
    return () => {
      window.cancelAnimationFrame(frame)
      track.removeEventListener('scroll', sync)
      window.removeEventListener('resize', sync)
    }
  }, [character.name, count])

  const scroll = (direction: -1 | 1) => {
    const track = trackRef.current
    if (!track) return
    track.scrollBy({ left: direction * track.clientWidth, behavior: 'smooth' })
  }

  const redoView = async (portraitId: string, viewRole: string) => {
    const label = VIEW_ROLE_LABELS[viewRole] || viewRole
    await onPayRequest(
      { character: character.name, view_role: viewRole },
      async (quote) => {
        setRedoing(`${portraitId}:${viewRole}`)
        try {
          const result = await api.regenerateCharacterView(
            projectId, character.name, portraitId, viewRole,
            { confirm: true, quote_id: quote.quote_id, idempotency_key: quote.quote_id },
          ) as { status?: string; run_id?: string; message?: string }
          toast(result?.status === 'accepted'
            ? `${label}视角重做已受理，可刷新查看进度`
            : `${label}视角已重做`)
          onChanged?.()
        } finally {
          setRedoing(null)
        }
      },
    )
  }

  return (
    <div className="character-portrait">
      <div ref={trackRef} className="character-portrait-track" aria-label={`${character.name}定妆照版本`}>
        {slides.map(({ key, imageUrl, portrait, versionIndex, view }, index) => (
          <figure key={key} className="character-portrait-slide">
            <img src={imageUrl}
              alt={`${character.name} · ${portrait ? portraitVersionLabel(portrait) : '定妆照'} · ${VIEW_ROLE_LABELS[view?.view_role || ''] || view?.view_role || '正面'}`}
              loading="lazy" decoding="async"
              style={{ opacity: fitting ? 0.45 : 1, transition: 'opacity 0.3s' }} />
            {portrait && <figcaption className="portrait-version-label">
              <span>
                {index + 1}/{count} · {portraitVersionLabel(portrait)} · {VIEW_ROLE_LABELS[view?.view_role || ''] || view?.view_role || '正面'}
                {portrait.ep_start <= 0
                  ? ' · 曾适用第1集起'
                  : portrait.ep_end != null
                  ? ` · 第${portrait.ep_start}-${portrait.ep_end}集`
                  : ` · 第${portrait.ep_start}集起`}
              </span>
              {versionIndex === 0 && portrait.ep_end == null ? <em>当前</em> : null}
            </figcaption>}
            {portrait && versionIndex === 0 && portrait.ep_end == null && view?.view_role && (
              <button
                type="button"
                className="portrait-view-redo"
                disabled={disabled || !!redoing}
                aria-label={disabled || !!redoing
                  ? `重做${character.name}的${VIEW_ROLE_LABELS[view.view_role] || view.view_role}视角，暂不可用：${disabled ? '当前有其他人物资产任务运行，请等待完成' : '正在提交上一项视角重做任务'}`
                  : `重做${character.name}的${VIEW_ROLE_LABELS[view.view_role] || view.view_role}视角；点击后立即提交生成`}
                title={
                  disabled
                    ? '当前有其他人物资产任务运行，请等待完成'
                    : redoing
                      ? '正在提交视角重做任务'
                      : '点击后立即提交重做，无需再次确认'
                }
                onClick={() => void redoView(portrait.id!, view.view_role!)}
              >
                {redoing === `${portrait.id}:${view.view_role}`
                  ? '受理中…'
                  : `重做${VIEW_ROLE_LABELS[view.view_role] || view.view_role}`}
              </button>
            )}
            {portrait?.change?.reason && (
              <div className="f-misc" style={{ fontSize: 11, padding: '4px 6px' }}>
                变化：{(portrait.change.change_dimensions || []).join('/') || '外观'} · {portrait.change.reason}
              </div>
            )}
          </figure>
        ))}
      </div>
      {count > 1 && (
        <div className="portrait-scroll-controls" aria-label="切换定妆照">
          <span>{count} 张视角图 · 可横向切换</span>
          <button
            type="button"
            disabled={!canScrollBack}
            onClick={() => scroll(-1)}
            aria-label={canScrollBack
              ? `上一张${character.name}定妆照`
              : `上一张${character.name}定妆照，暂不可用：当前已是第一张`}
          >
            ‹
          </button>
          <button
            type="button"
            disabled={!canScrollForward}
            onClick={() => scroll(1)}
            aria-label={canScrollForward
              ? `下一张${character.name}定妆照`
              : `下一张${character.name}定妆照，暂不可用：当前已是最后一张`}
          >
            ›
          </button>
        </div>
      )}
    </div>
  )
}

function SkipConfirmDialog({
  data,
  onClose,
  onConfirm,
}: {
  data: { count: number; names: string[] }
  onClose: () => void
  onConfirm: () => void
}) {
  const trapRef = useFocusTrap(true, onClose)
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label="跳过定妆缺口确认">
        <h3>仍有定妆缺口，确认继续分集？</h3>
        <p>当前仍有 {data.count} 个角色缺少可用定妆照或质检未通过。继续后，分镜阶段可能需要自动补图或暂停等待。</p>
        <ul>
          {data.names.slice(0, 20).map(name => <li key={name}>{name}</li>)}
          {data.names.length > 20 && <li>另有 {data.names.length - 20} 个角色…</li>}
        </ul>
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>返回补齐</button>
          <button type="button" className="btn primary" onClick={onConfirm}>确认跳过，继续分集</button>
        </div>
      </section>
    </div>
  )
}

function ConflictDialog({
  conflict,
  onlyServer,
  onlyLocal,
  both,
  fieldConflicts,
  canMerge,
  onClose,
  onMerge,
  onRefresh,
}: {
  conflict: {
    message: string
    current_version?: number
    character_names?: string[]
    server_bible?: Bible | null
  }
  onlyServer: string[]
  onlyLocal: string[]
  both: string[]
  fieldConflicts: BibleMergeConflict[]
  canMerge: boolean
  onClose: () => void
  onMerge: (choices: Record<string, 'local' | 'server'>) => void
  onRefresh: () => void
}) {
  const trapRef = useFocusTrap(true, onClose)
  const [choices, setChoices] = useState<Record<string, 'local' | 'server'>>(() =>
    Object.fromEntries(fieldConflicts.map(item => [item.path, 'server'])),
  )
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label="版本冲突">
        <h3>人物谱版本冲突</h3>
        <p>{conflict.message}</p>
        {typeof conflict.current_version === 'number' && (
          <p>线上最新版本：第 {conflict.current_version} 稿</p>
        )}
        {conflict.server_bible ? (
          <div className="conflict-merge-grid">
            <div>
              <b>仅线上版本新增</b>
              <p>{onlyServer.length ? onlyServer.join('、') : '无'}</p>
            </div>
            <div>
              <b>仅本地修订</b>
              <p>{onlyLocal.length ? onlyLocal.join('、') : '无'}</p>
            </div>
            <div>
              <b>两边都存在</b>
              <p>{both.length ? both.join('、') : '无'}</p>
            </div>
          </div>
        ) : !!conflict.character_names?.length && (
          <p>线上版本角色：{conflict.character_names.join('、')}</p>
        )}
        {!!fieldConflicts.length && (
          <div className="conflict-field-list">
            <h4>双方同时修改的字段</h4>
            {fieldConflicts.map(item => (
              <fieldset key={item.path}>
                <legend>{bibleConflictFieldLabel(item.path)}</legend>
                <label>
                  <input type="radio" name={`conflict:${item.path}`} checked={choices[item.path] === 'local'}
                    onChange={() => setChoices(current => ({ ...current, [item.path]: 'local' }))} />
                  保留我的修改：{JSON.stringify(item.local)}
                </label>
                <label>
                  <input type="radio" name={`conflict:${item.path}`} checked={choices[item.path] !== 'local'}
                    onChange={() => setChoices(current => ({ ...current, [item.path]: 'server' }))} />
                  采用线上版本：{JSON.stringify(item.server)}
                </label>
              </fieldset>
            ))}
          </div>
        )}
        <p>请处理冲突后继续；禁止用旧页面静默覆盖。</p>
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>返回继续检查</button>
          {canMerge && (
            <button type="button" className="btn" onClick={() => onMerge(choices)}>合并选定字段并继续</button>
          )}
          <button type="button" className="btn danger" onClick={onRefresh}>放弃我的修改并刷新</button>
        </div>
      </section>
    </div>
  )
}
