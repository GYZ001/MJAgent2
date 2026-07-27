import { useEffect, useRef, useState } from 'react'
import {
  api, ApiError, Bible, BibleImpactPreview, Character, Portrait, PortraitView, RefsCostPrecheck,
} from '../api'
import { useNav, useProject } from '../App'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import SearchField from '../components/SearchField'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import ImpactDialog, { ImpactSummary } from '../components/harness/ImpactDialog'
import PaymentConfirmDialog from '../components/PaymentConfirmDialog'
import GenerationParamsDialog from '../components/GenerationParamsDialog'
import QueryState from '../components/QueryState'
import PrepSubnav from '../components/PrepSubnav'
import { useFillPageSize } from '../hooks/useFillPageSize'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { usePrepListState } from '../hooks/usePrepListState'
import { formatBookTitle } from '../lib/bookTitle'
import type { PrepStepStatus } from '../lib/statusLabels'
import CharacterFilters, {
  EMPTY_CHARACTER_FILTERS,
  matchCharacterFilters,
  type CharacterFilterState,
} from '../components/CharacterFilters'
import CharacterQaPanel from '../components/CharacterQaPanel'
import ImageCompareModal from '../components/ImageCompareModal'
import AutoChangeQueue from '../components/AutoChangeQueue'

const CHAR_QA_PASS = 0.6

type RefsProgress = Awaited<ReturnType<typeof api.refsProgress>>
type PaymentQuote = RefsCostPrecheck & {
  estimated_duration_min?: number[]
  estimate_note?: string
  character_names?: string[]
}

type PaymentSelection = { characters: string[] }

function currentPortrait(character: Character): Portrait | null {
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

function portraitAvailability(character: Character, fitting: boolean): PortraitAvailability {
  if (fitting) return 'generating'
  const portrait = currentPortrait(character)
  if (!portrait || (!portrait.image_url && !(portrait.views ?? []).some(v => v.image_url))) {
    return character.ref_image_url ? 'unverified' : 'missing'
  }
  const status = portrait.pack_status
  if (status === 'generating' || status === 'qa_pending') return 'generating'
  if (status === 'failed') return 'failed'
  const qa = portrait.group_qa
  const hard = (qa?.hard_failures ?? []).length > 0 || qa?.status === 'failed'
  if (hard) return 'failed'
  if (status === 'ready' || !status) {
    if (typeof qa?.overall === 'number') {
      if (qa.overall >= CHAR_QA_PASS) {
        return (qa.issues ?? []).length ? 'warning' : 'passed'
      }
      return 'failed'
    }
    return status === 'ready' ? 'unverified' : 'unverified'
  }
  return 'unverified'
}

function availabilityStamp(state: PortraitAvailability): { label: string; color: string } {
  switch (state) {
    case 'generating': return { label: '生成/验证中', color: 'gold' }
    case 'passed': return { label: '已采用且通过', color: 'green' }
    case 'warning': return { label: '已采用但有警告', color: 'gold' }
    case 'failed': return { label: '硬失败/不可用', color: 'red' }
    case 'missing': return { label: '未出图', color: 'grey' }
    default: return { label: '待复核/未验证', color: 'grey' }
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

function characterIsFitting(project: { refs_status?: string; refs_target?: string | null }, character: Character): boolean {
  const portraits = character.portraits ?? []
  return project.refs_status === 'running' && (
    project.refs_target === character.name
    || (!project.refs_target && portraits.some(portrait => portrait.pack_status === 'generating'))
  )
}

function characterAvailabilityForFilter(
  project: { refs_status?: string; refs_target?: string | null } | null | undefined,
  character: Character,
): PortraitAvailability {
  return portraitAvailability(character, !!project && characterIsFitting(project, character))
}

function bibleStepStatus(project: {
  bible?: Bible | null
  bible_status?: string
  refs_status?: string
}): PrepStepStatus {
  if (project.bible_status === 'running' || project.refs_status === 'running') return 'running'
  if (['failed', 'warning'].includes(project.bible_status || '')
    || ['failed', 'warning'].includes(project.refs_status || '')) return 'problem'
  if (project.bible) {
    const states = (project.bible.characters ?? []).map(character => portraitAvailability(character, false))
    if (states.some(state => state === 'failed' || state === 'missing')) return 'problem'
    if (states.length > 0 && states.every(state => state === 'passed' || state === 'warning')) return 'done'
  }
  return 'idle'
}

function sceneStepStatus(project: { bible?: Bible | null; scene_refs_status?: string }): PrepStepStatus {
  const status = project.scene_refs_status
  if (status === 'running') return 'running'
  if (status === 'failed' || status === 'warning') return 'problem'
  if ((project.bible?.scenes ?? []).length > 0) return 'done'
  if (status && ['ready', 'done', 'succeeded'].includes(status)) return 'done'
  return 'idle'
}

function episodeStepStatus(project: { episodes?: unknown[]; episodes_total?: number; episode_count?: number }): PrepStepStatus {
  if (Array.isArray(project.episodes)) return project.episodes.length > 0 ? 'done' : 'idle'
  if (typeof project.episodes_total === 'number') return project.episodes_total > 0 ? 'done' : 'idle'
  if (typeof project.episode_count === 'number') return project.episode_count > 0 ? 'done' : 'idle'
  return 'idle'
}

function readyPortraitMosaic(bible: Bible): { src: string; label: string }[] {
  const images: { src: string; label: string; ep: number }[] = []
  for (const character of bible.characters ?? []) {
    for (const portrait of character.portraits ?? []) {
      if (portrait.pack_status && portrait.pack_status !== 'ready') continue
      const view = (portrait.views ?? []).find(item => !!item.image_url)
      const src = view?.image_url || portrait.image_url
      if (src) images.push({ src, label: character.name, ep: portrait.ep_start })
    }
  }
  return images.sort((a, b) => b.ep - a.ep).slice(0, 4)
}

function characterCompareImages(character: Character): { src: string; label: string }[] {
  const images: { src: string; label: string; ep: number }[] = []
  for (const portrait of character.portraits ?? []) {
    for (const view of portrait.views ?? []) {
      if (view.image_url) {
        images.push({
          src: view.image_url,
          label: `${portraitVersionLabel(portrait)} · ${VIEW_ROLE_LABELS[view.view_role || ''] || view.view_role || '视角'}`,
          ep: portrait.ep_start,
        })
      }
    }
    if (portrait.image_url) {
      images.push({ src: portrait.image_url, label: portraitVersionLabel(portrait), ep: portrait.ep_start })
    }
  }
  if (!images.length && character.ref_image_url) {
    images.push({ src: character.ref_image_url, label: '历史定妆照', ep: 0 })
  }
  return images.sort((a, b) => b.ep - a.ep).map(({ src, label }) => ({ src, label }))
}

function summarizeProgress(progress: RefsProgress | null): string {
  if (!progress) return ''
  return `定妆进度：已完成 ${progress.ready} / ${progress.total}，失败 ${progress.failed}，缺失 ${progress.missing}`
}

function progressProblemNames(progress: RefsProgress | null): string[] {
  return (progress?.items ?? [])
    .filter(item => item.status === 'missing' || item.status === 'failed')
    .map(item => item.character)
    .filter(Boolean)
}

function mergeServerOnlyCharacters(local: Bible, server: Bible): Bible {
  const localNames = new Set((local.characters ?? []).map(character => character.name))
  const serverOnly = (server.characters ?? []).filter(character => !localNames.has(character.name))
  return { ...local, characters: [...local.characters, ...serverOnly.map(cloneCharacter)] }
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
    { label: '全局画风', text: parts[0] || fallback || '未生成' },
    { label: '外观锚点', text: parts[1] || parts[0] || fallback || '未生成' },
    { label: '姿态与约束', text: parts.slice(2).join('。') || parts[1] || fallback || '未生成' },
  ]
}

export default function BiblePage() {
  const { projectId, toast, go } = useNav()
  const { data: p, refresh, error, loading } = useProject(projectId!, undefined, 'bible')
  const [editing, setEditing] = useState<Bible | null>(null)
  const [editBaseVersion, setEditBaseVersion] = useState<number | null>(null)
  const [undoStack, setUndoStack] = useState<Bible[]>([])
  const [draftState, setDraftState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [busy, setBusy] = useState(false)
  const pageSize = useFillPageSize({ minCardWidth: 270, rows: 3, floor: 8, ceiling: 24 })
  const [listState, setListState] = usePrepListState(projectId!, 'bible-characters', pageSize)
  const charSearch = listState.search
  const charFilters: CharacterFilterState = { ...EMPTY_CHARACTER_FILTERS, ...(listState.filters as Partial<CharacterFilterState>) }
  const charPage = listState.page
  const setCharSearch = (value: string) => setListState(current => ({ ...current, search: value, page: 0 }))
  const setCharFilters = (value: CharacterFilterState) => setListState(current => ({ ...current, filters: value, page: 0 }))
  const setCharPage = (value: number) => setListState(current => ({ ...current, page: value, scrollY: window.scrollY }))
  const [paramsCharacterName, setParamsCharacterName] = useState<string | null>(null)
  const [qaDetail, setQaDetail] = useState<{ characterName: string; portrait: Portrait } | null>(null)
  const [compareDetail, setCompareDetail] = useState<{ title: string; images: { src: string; label: string }[] } | null>(null)
  const [timelineCharacter, setTimelineCharacter] = useState('')
  const [refsProgress, setRefsProgress] = useState<RefsProgress | null>(null)
  const [skipConfirm, setSkipConfirm] = useState<{ count: number; names: string[] } | null>(null)
  const [impactOpen, setImpactOpen] = useState(false)
  const [impactLoading, setImpactLoading] = useState(false)
  const [impactError, setImpactError] = useState<string | null>(null)
  const [impactPreview, setImpactPreview] = useState<BibleImpactPreview | null>(null)
  const [conflict, setConflict] = useState<{
    message: string
    current_version?: number
    character_names?: string[]
    server_bible?: Bible | null
  } | null>(null)
  const [payOpen, setPayOpen] = useState(false)
  const [payTitle, setPayTitle] = useState('')
  const [payLoading, setPayLoading] = useState(false)
  const [payError, setPayError] = useState<string | null>(null)
  const [payPrecheck, setPayPrecheck] = useState<RefsCostPrecheck | null>(null)
  const [paySelectable, setPaySelectable] = useState(false)
  const payActionRef = useRef<null | ((selection: PaymentSelection) => Promise<void>)>(null)
  const [impactMode, setImpactMode] = useState<'bible' | 'character'>('bible')
  const [pendingCharacterSave, setPendingCharacterSave] = useState<{ name: string; character: Character } | null>(null)
  const editingRef = useRef<Bible | null>(null)
  const bibleTimer = useTaskTimer(`project.${projectId}.bible`, p?.bible_status === 'running')
  const refsTimer = useTaskTimer(`project.${projectId}.refs`, p?.refs_status === 'running')

  const biblePreview = editing ?? p?.bible
  const charQuery = charSearch.trim()
  const indexedCharsPreview = (biblePreview?.characters ?? []).map((c, i) => ({ c, i }))
  const filteredCharsPreview = indexedCharsPreview.filter(({ c }) =>
    matchCharacterFilters(c, charQuery, charFilters, {
      availability: characterAvailabilityForFilter(p, c),
      hasPortrait: characterHasPortrait(c),
    }),
  )
  const charPageCount = Math.max(1, Math.ceil(filteredCharsPreview.length / pageSize))
  const dirtyCount = countBibleChanges(editing, p?.bible)
  const dirty = dirtyCount > 0
  const currentEditVersion = editBaseVersion ?? p?.bible_version ?? 0

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
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  useEffect(() => {
    editingRef.current = editing
  }, [editing])

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

  useEffect(() => {
    if (!projectId || !dirty) return
    const id = window.setInterval(() => {
      const latest = editingRef.current
      if (!latest) return
      setDraftState('saving')
      api.saveBibleDraft(projectId, { bible: latest, expected_version: currentEditVersion })
        .then(() => setDraftState('saved'))
        .catch(() => setDraftState('error'))
    }, 8000)
    return () => window.clearInterval(id)
  }, [projectId, dirty, currentEditVersion])

  useEffect(() => {
    if (!p || !(p.bible_status === 'running' || p.refs_status === 'running')) return
    let cancelled = false
    const load = async () => {
      try {
        const progress = await api.refsProgress(p.id)
        if (!cancelled) setRefsProgress(progress)
      } catch {
        if (!cancelled) setRefsProgress(null)
      }
    }
    void load()
    const id = window.setInterval(load, 3500)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [p?.id, p?.bible_status, p?.refs_status])

  if (error && !p) return <QueryState loading={false} error={error} hasData={false} objectName="人物谱" onRetry={refresh}>{null}</QueryState>
  if (!p) return <QueryState loading={loading !== false} error={null} hasData={false} objectName="人物谱" onRetry={refresh}>{null}</QueryState>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { await fn(); if (doneMsg) toast(doneMsg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const bible = editing ?? p.bible
  const indexedChars = (bible?.characters ?? []).map((c, i) => ({ c, i }))
  const filteredChars = indexedChars.filter(({ c }) =>
    matchCharacterFilters(c, charQuery, charFilters, {
      availability: characterAvailabilityForFilter(p, c),
      hasPortrait: characterHasPortrait(c),
    }),
  )
  const curCharPage = Math.min(charPage, charPageCount - 1)
  const pagedChars = filteredChars.slice(curCharPage * pageSize, curCharPage * pageSize + pageSize)
  const generating = p.bible_status === 'running' || p.refs_status === 'running'
  const paramsCharacter = paramsCharacterName
    ? bible?.characters.find(character => character.name === paramsCharacterName) ?? null
    : null
  const prepStatuses: Partial<Record<'bible' | 'scenes' | 'episodes', PrepStepStatus>> = {
    bible: bibleStepStatus(p),
    scenes: sceneStepStatus(p),
    episodes: episodeStepStatus(p),
  }
  const characterRoles = Array.from(new Set((bible?.characters ?? []).map(character => character.role).filter(Boolean)))
  const mosaicImages = bible ? readyPortraitMosaic(bible) : []
  const timelineQuery = timelineCharacter.trim()
  const timelineNames = (bible?.characters ?? []).map(character => character.name).filter(Boolean)
  const filteredTimeline = (p.key_timeline ?? []).filter(item => {
    if (!timelineQuery) return true
    return item.includes(timelineQuery)
      || timelineNames.some(name => name.includes(timelineQuery) && item.includes(name))
  })

  const openPayment = async (
    title: string,
    precheckBody: { character?: string; characters?: string[]; resume?: boolean; view_role?: string },
    action: (quote: RefsCostPrecheck, selection: PaymentSelection) => Promise<void>,
    precheckLoader?: () => Promise<PaymentQuote>,
    options?: { enableScopeSelection?: boolean },
  ) => {
    setPayTitle(title)
    setPayOpen(true)
    setPayLoading(true)
    setPayError(null)
    setPayPrecheck(null)
    setPaySelectable(!!options?.enableScopeSelection)
    try {
      const quote = precheckLoader
        ? await precheckLoader()
        : await api.refsPrecheck(p.id, precheckBody)
      setPayPrecheck(quote)
      payActionRef.current = async (selection: PaymentSelection) => {
        await action(quote, selection)
      }
    } catch (e: unknown) {
      setPayError((e as Error).message)
      payActionRef.current = null
    } finally {
      setPayLoading(false)
    }
  }

  const updateEditing = (updater: (current: Bible) => Bible) => {
    setEditing(current => {
      if (!current) return current
      const snapshot = cloneBible(current)
      const next = updater(cloneBible(current))
      setUndoStack(stack => [snapshot, ...stack].slice(0, 20))
      return next
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

  const startBible = async () => {
    await openPayment(
      p.bible ? '重新生成人物谱和定妆照' : '开始生成人物谱和定妆照',
      {},
      async (quote) => {
        bibleTimer.start()
        refsTimer.start()
        await api.post(`/projects/${p.id}/bible`, { confirm: true, quote_id: quote.quote_id })
        toast('人物谱与定妆照生成已开始')
        refresh()
      },
      () => api.bibleGeneratePrecheck(p.id),
    )
  }

  const stopGeneration = async () => {
    setBusy(true)
    let stopped = ''
    try {
      if (p.bible_status === 'running') {
        await api.post(`/projects/${p.id}/bible/cancel`)
        stopped = '已停止谱写；已落盘资产保留'
      } else {
        await api.post(`/projects/${p.id}/refs/cancel`)
        stopped = '已停止定妆；已落盘资产保留'
      }
      let summary = ''
      try {
        const progress = await api.refsProgress(p.id)
        setRefsProgress(progress)
        summary = summarizeProgress(progress)
      } catch { /* keep original stop toast */ }
      toast(summary ? `${stopped}；${summary}` : stopped)
      refresh()
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const retryRefs = async () => {
    await openPayment(
      '补齐缺失的定妆照',
      { resume: true },
      async (quote, selection) => {
        refsTimer.start()
        const selectedCharacters = selection.characters.filter(Boolean)
        const effectiveQuote = selectedCharacters.length
          ? await api.refsPrecheck(p.id, { resume: true, characters: selectedCharacters })
          : quote
        await api.post(`/projects/${p.id}/refs`, {
          resume: true,
          characters: selectedCharacters.length ? selectedCharacters : undefined,
          confirm: true,
          quote_id: effectiveQuote.quote_id,
        })
        toast('已开始补齐缺失的定妆照，已有成品会保留')
        refresh()
      },
      async () => {
        const gaps = await api.refsGaps(p.id)
        return gaps.precheck
      },
      { enableScopeSelection: true },
    )
  }

  const skipToEpisodes = async () => {
    try {
      let names: string[] = []
      try {
        const progress = await api.refsProgress(p.id)
        setRefsProgress(progress)
        names = progressProblemNames(progress)
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

  const openImpactPreview = async () => {
    if (!editing) return
    setImpactMode('bible')
    setPendingCharacterSave(null)
    setImpactOpen(true)
    setImpactLoading(true)
    setImpactError(null)
    setImpactPreview(null)
    try {
      const preview = await api.bibleImpactPreview(p.id, {
        bible: editing,
        expected_version: currentEditVersion,
      })
      setImpactPreview(preview)
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
          setImpactOpen(false)
          setConflict({
            message: detail.message || e.message,
            current_version: detail.current_version,
            character_names: detail.character_names,
            server_bible: detail.server_bible,
          })
          return
        }
      }
      setImpactError((e as Error).message)
    } finally {
      setImpactLoading(false)
    }
  }

  const saveBible = async () => {
    if (!editing || !impactPreview) return
    if (impactMode === 'character' && pendingCharacterSave) {
      await saveCharacterDraft(pendingCharacterSave.character, impactPreview.fingerprint)
      return
    }
    setBusy(true)
    try {
      const r = await api.put(`/projects/${p.id}/bible`, {
        bible: editing,
        expected_version: currentEditVersion,
        confirm: true,
        impact_preview_fingerprint: impactPreview.fingerprint,
      }) as {
        style_changed?: boolean
        purged?: { versions: number } | null
        impact?: ImpactSummary
      }
      setEditing(null)
      setEditBaseVersion(null)
      setUndoStack([])
      setImpactOpen(false)
      setImpactPreview(null)
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
          setImpactOpen(false)
          setConflict({
            message: detail.message || e.message,
            current_version: detail.current_version,
            character_names: detail.character_names,
            server_bible: detail.server_bible,
          })
          return
        }
        if (detail?.code === 'IMPACT_PREVIEW_STALE' && detail.preview) {
          setImpactPreview(detail.preview)
          setImpactError('影响预检已过期，已刷新最新结果，请再次确认')
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
      if (typeof result.bible_version === 'number') setEditBaseVersion(result.bible_version)
      setImpactOpen(false)
      setImpactPreview(null)
      setPendingCharacterSave(null)
      setImpactMode('bible')
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
          setPendingCharacterSave({ name: character.name, character: cloneCharacter(character) })
          setImpactMode('character')
          setImpactOpen(true)
          setImpactError(null)
          setImpactPreview(preview)
          return
        }
      }
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const abandonEditing = () => {
    if (!dirty) {
      setEditing(null)
      return
    }
    if (window.confirm('有未保存的人物谱修订，确定放弃吗？')) {
      setEditing(null)
      setEditBaseVersion(null)
      setUndoStack([])
    }
  }

  const stopLabel = p.bible_status === 'running' ? '停止谱写' : '停止定妆'
  const conflictServerNames = new Set((conflict?.server_bible?.characters ?? []).map(character => character.name))
  const conflictLocalNames = new Set((editing?.characters ?? []).map(character => character.name))
  const conflictOnlyServer = [...conflictServerNames].filter(name => !conflictLocalNames.has(name))
  const conflictOnlyLocal = [...conflictLocalNames].filter(name => !conflictServerNames.has(name))
  const conflictBoth = [...conflictLocalNames].filter(name => conflictServerNames.has(name))

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / {formatBookTitle(p.name)}</div>
        <PrepSubnav
          current="bible"
          statuses={prepStatuses}
          onBeforeNavigate={() => {
            if (!dirty) return true
            return window.confirm('有未保存的人物谱修订。离开会保留草稿但不会定稿，确定离开？')
          }}
          onProblemClick={(key) => {
            if (key === 'bible') {
              setCharFilters({ ...EMPTY_CHARACTER_FILTERS, missing: 'yes' })
              setCharPage(0)
            }
          }}
        />
        <h1>人物谱 <span className="sub">角色资产与定妆版本中心 · 保持跨镜头、跨分集一致</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <h3>原著 <span className="hint">{(p.novel_chars / 10000).toFixed(1)} 万字 · {p.chapter_count ?? p.chapters?.length ?? 0} 章</span></h3>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          {!p.bible && !generating && (
            <button className="btn primary" disabled={busy} onClick={() => void startBible()}>
              {p.bible_status === 'failed' ? '重新生成人物谱和定妆照' : '开始生成人物谱和定妆照'}
            </button>
          )}
          {p.bible && p.refs_status === 'failed' && !generating && (
            <>
              <button className="btn primary" disabled={busy} onClick={() => void retryRefs()}>
                补齐缺失的定妆照
              </button>
              <button className="btn ghost" disabled={busy} onClick={() => void skipToEpisodes()}>
                暂时跳过，继续分集
              </button>
            </>
          )}
          {generating && (
            <button className="btn ghost" disabled={busy} onClick={stopGeneration}>
              {stopLabel}
            </button>
          )}
          {p.bible_status === 'running' && <span className="stamp gold">谱写中（约 1~3 分钟）</span>}
          {p.refs_status === 'running' && <span className="stamp gold">定妆中</span>}
          {p.bible && <span className="stamp green">第 {`${p.bible_version ?? ''}`} 稿</span>}
          {dirty && <span className="stamp gold">未保存修订 · {dirtyCount} 项</span>}
          {editing && draftState === 'saving' && <span className="stamp grey">草稿保存中</span>}
          {editing && draftState === 'saved' && <span className="stamp green">草稿已自动保存</span>}
          {editing && draftState === 'error' && <span className="stamp red">草稿保存失败，已保留本地备份</span>}
          {p.bible_evidence && <EvidenceDrawer evidence={p.bible_evidence} label="人物谱证据" />}
          <TaskTimer label="人物谱" timer={bibleTimer} />
          <TaskTimer label="定妆照" timer={refsTimer} />
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
                {(['ready', 'failed', 'missing'] as const).map(status => {
                  const items = refsProgress.items.filter(item => item.status === status)
                  if (!items.length) return null
                  const label = status === 'ready' ? '已完成' : status === 'failed' ? '失败' : '缺失'
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
          <div className="hint" style={{ marginTop: 10 }}>
            已启动过后端持续生成人物谱链路；后续在分镜阶段按集自动判定角色外观是否变化、按需重绘定妆照，前端不再提供打回重生入口。
          </div>
        )}
        {p.bible_status === 'failed' && <div className="error-banner">人物谱生成失败（原始错误如下，不做静默兜底）：{'\n'}{p.bible_error}</div>}
        {p.bible_status === 'warning' && <div className="error-banner">人物谱存在未解决的门禁问题，下游已暂停：{'\n'}{p.bible_error}</div>}
      </section>

      {bible && (
        <section className="card bible-library">
          <h3>世界观
            <span className="hint">era {bible.world.era} · genre {bible.world.genre}</span>
            {!editing
              ? <button className="btn small" style={{ marginLeft: 14 }} onClick={() => void beginRevision()}>修订</button>
              : <>
                <button className="btn small primary" style={{ marginLeft: 14 }} disabled={busy}
                  onClick={() => void openImpactPreview()}>定稿</button>
                <button className="btn small" style={{ marginLeft: 8 }} disabled={!undoStack.length}
                  onClick={undoEdit}>撤销</button>
                <button className="btn small ghost" style={{ marginLeft: 8 }} onClick={abandonEditing}>放弃</button>
              </>}
          </h3>
          <label className="f">全局画风锚点串（逐字注入每个镜头 prompt）</label>
          {editing
            ? <textarea rows={2} value={editing.world.visual_style_canonical}
                onChange={e => updateEditing(current => ({
                  ...current,
                  world: { ...current.world, visual_style_canonical: e.target.value },
                }))} />
            : <div style={{ fontSize: 14, background: 'rgba(181,68,52,0.05)', borderLeft: '3px solid var(--cinnabar)', padding: '8px 12px', borderRadius: '0 6px 6px 0', lineHeight: 1.9 }}>{bible.world.visual_style_canonical}</div>}
          {!!mosaicImages.length && (
            <div className="worldview-mosaic" aria-label="最近可用定妆照">
              {mosaicImages.map(image => (
                <figure key={`${image.label}:${image.src}`}>
                  <img src={image.src} alt={`${image.label} 定妆照`} />
                  <figcaption>{image.label}</figcaption>
                </figure>
              ))}
            </div>
          )}
          {!mosaicImages.length && (
            <div className="hint" style={{ marginTop: 10 }}>
              暂无可用定妆样图：当前角色尚未生成通过 QA 的定妆包，或已有包仍在生成/验证中。
            </div>
          )}

          <div style={{ height: 16 }} />
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 12, flexWrap: 'wrap' }}>
            {p.refs_status === 'running' && <span className="stamp gold">定妆中</span>}
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              启动后会先为全部角色生成初始定妆照；随后在分镜阶段按集判断角色外观是否相比当前定妆照大变，大变才图生图重绘并切分适用集，新登场重要人物会自动补人物卡并生成定妆照
            </span>
          </div>
          {p.refs_status === 'failed' && <div className="error-banner">定妆照生成失败：{'\n'}{p.refs_error}</div>}
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', margin: '4px 0 12px' }}>
            <SearchField value={charSearch} onChange={value => { setCharSearch(value); setCharPage(0) }}
              placeholder="搜索角色名…" ariaLabel="搜索角色" className="library-search" />
            <CharacterFilters
              value={charFilters}
              onChange={value => { setCharFilters(value); setCharPage(0) }}
              roles={characterRoles}
            />
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              共 {bible.characters.length} 个角色{charQuery ? ` · 命中 ${filteredChars.length}` : ''}
            </span>
          </div>
          <div className="figure-grid">
            {pagedChars.map(({ c, i }: { c: Character; i: number }) => {
              const portraits = c.portraits ?? []
              const hasPortraitImage = portraits.some(portrait =>
                (!!portrait.image_url || (portrait.views ?? []).some(view => !!view.image_url)),
              )
              const fitting = p.refs_status === 'running' && (
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
                {active?.group_qa && (
                  <CharacterQaLine qa={active.group_qa} availability={availability} />
                )}
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
                  <button className="btn small" type="button" disabled={!active}
                    onClick={() => active && setQaDetail({ characterName: c.name, portrait: active })}>
                    QA详情
                  </button>
                  <button className="btn small" type="button" disabled={!characterCompareImages(c).length}
                    onClick={() => setCompareDetail({ title: `${c.name} · 定妆图对比`, images: characterCompareImages(c) })}>
                    放大对比
                  </button>
                </div>
                <label className="f">外观锚点串（40~60 字，定稿后锁定）</label>
                {editing
                  ? <textarea rows={3} value={editing.characters[i].appearance_canonical}
                      onChange={e => {
                        updateEditing(current => {
                          const next = { ...current, characters: [...current.characters] }
                          next.characters[i] = { ...next.characters[i], appearance_canonical: e.target.value }
                          return next
                        })
                      }} />
                  : <div className="f-anchor">{c.appearance_canonical}</div>}
                {editing && (
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', margin: '8px 0' }}>
                    <button
                      type="button"
                      className="btn small primary"
                      disabled={busy || !characterDirty}
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
          {!pagedChars.length && (
            <div className="empty">{charQuery ? `没有匹配「${charQuery}」的角色` : '暂无角色'}</div>
          )}
          {charPageCount > 1 && (
            <div style={{ display: 'flex', gap: 10, alignItems: 'center', justifyContent: 'center', marginTop: 14 }}>
              <button className="btn small" disabled={curCharPage <= 0} onClick={() => setCharPage(curCharPage - 1)}>← 上一页</button>
              <span style={{ fontSize: 13, color: 'var(--ink-faint)' }}>第 {curCharPage + 1} / {charPageCount} 页</span>
              <button className="btn small" disabled={curCharPage >= charPageCount - 1} onClick={() => setCharPage(curCharPage + 1)}>下一页 →</button>
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
              const parts: Array<string | { episode: number; text: string }> = []
              let lastIndex = 0
              for (const match of k.matchAll(/第([零一二两三四五六七八九十\d]+)集/g)) {
                const episode = cnEpisodeToNumber(match[1])
                if (!episode) continue
                if (match.index !== undefined && match.index > lastIndex) parts.push(k.slice(lastIndex, match.index))
                parts.push({ episode, text: match[0] })
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
                        key={`${index}:${part.episode}`}
                        type="button"
                        className="timeline-episode-link"
                        onClick={() => {
                          window.sessionStorage.setItem(`prep-episodes-focus:${p.id}`, JSON.stringify({ episode_no: part.episode }))
                          go('episodes', p.id)
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
                          onClick={() => setTimelineCharacter(name)}
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
      <AutoChangeQueue projectId={p.id} onChanged={refresh} />
      <ImpactDialog
        open={impactOpen}
        title={impactMode === 'character' && pendingCharacterSave
          ? `保存「${pendingCharacterSave.name}」并传播影响`
          : '定稿人物谱并传播影响'}
        impact={impactPreview}
        loading={impactLoading}
        error={impactError}
        onClose={() => {
          setImpactOpen(false)
          setImpactError(null)
          setImpactMode('bible')
          setPendingCharacterSave(null)
        }}
        onConfirm={() => { void saveBible() }}
      />
      <PaymentConfirmDialog
        open={payOpen}
        title={payTitle}
        precheck={payPrecheck}
        loading={payLoading}
        error={payError}
        enableScopeSelection={paySelectable}
        onClose={() => { setPayOpen(false); payActionRef.current = null }}
        onConfirm={(selection) => {
          const run = payActionRef.current
          setPayOpen(false)
          if (!run) return
          void act(() => run(selection))
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
          canMerge={!!conflict.server_bible && !!editing}
          onClose={() => setConflict(null)}
          onMerge={() => {
            if (!editing || !conflict.server_bible) return
            setEditing(mergeServerOnlyCharacters(editing, conflict.server_bible))
            setEditBaseVersion(conflict.current_version ?? editBaseVersion ?? p.bible_version ?? 0)
            setConflict(null)
            toast('已合并服务端新增角色，请复核后重新定稿')
          }}
          onRefresh={() => {
            setConflict(null)
            setEditing(null)
            setEditBaseVersion(null)
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
          subtitle="查看角色设定、调整定妆照生成词，或重新生成当前角色定妆照。"
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
            disabled={busy || p.refs_status === 'running'} onChanged={refresh}
            regenerate={() => openPayment(
              `重新生成「${paramsCharacter.name}」造型包`,
              { character: paramsCharacter.name },
              async (quote) => {
                await api.post(`/projects/${p.id}/refs`, {
                  character: paramsCharacter.name,
                  confirm: true,
                  quote_id: quote.quote_id,
                })
                toast(`正在为「${paramsCharacter.name}」重新定妆`)
                refresh()
              },
            )} />
        </GenerationParamsDialog>
      )}
    </>
  )
}

function CharacterQaLine({
  qa, availability,
}: {
  qa: NonNullable<Portrait['group_qa']>
  availability: PortraitAvailability
}) {
  const overall = qa.overall
  const issues = [...(qa.hard_failures ?? []), ...(qa.issues ?? [])]
  const color = availability === 'passed' ? 'var(--moss)'
    : availability === 'warning' ? 'var(--gold, #b8860b)'
      : availability === 'failed' ? 'var(--cinnabar)' : 'var(--ink-faint)'
  return (
    <div className="scene-qa-line" style={{ marginBottom: 8 }}>
      <span>整包 QA：{typeof overall === 'number'
        ? <b style={{ color }}>{overall.toFixed(2)}</b>
        : <b style={{ color }}>未验证</b>}
      </span>
      {issues.length ? <span style={{ color: 'var(--ink-faint)' }}>　{issues.slice(0, 2).join('；')}</span> : null}
    </div>
  )
}

function PortraitBlock({ projectId, character: c, disabled, onChanged, regenerate }: {
  projectId: string; character: Character; disabled: boolean
  onChanged: () => void; regenerate: () => void
}) {
  const { toast } = useNav()
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const isOverridden = !!(c.portrait_prompt_override || '').trim()
  const draftLen = (draft ?? '').trim().length
  const draftValid = draft !== null && (draftLen === 0 || (draftLen >= 10 && draftLen <= 400))

  async function save(thenRegen: boolean, value?: string) {
    setSaving(true)
    try {
      const text = value ?? draft ?? ''
      const r = await api.put(`/projects/${projectId}/characters/${encodeURIComponent(c.name)}/portrait`,
        { portrait_prompt: text })
      toast(r.reset_to_default ? `「${c.name}」画像描述已恢复默认` : `「${c.name}」画像描述已保存`)
      setDraft(null); onChanged()
      if (thenRegen) regenerate()
    } catch (e: unknown) { toast((e as Error).message, true) }
    finally { setSaving(false) }
  }

  return (
    <div style={{ marginTop: 10 }}>
      <label className="f">画像描述（定妆照生成词）{isOverridden ? ' · 已自定义' : ' · 默认（由画风+锚点串合成）'}</label>
      {draft === null ? (
        <>
          <div className="prompt-source-chips" aria-label="画像描述来源">
            {promptSegments(c.portrait_prompt_effective).map(segment => (
              <span key={segment.label} title={segment.text}>
                <b>{segment.label}</b>{segment.text}
              </span>
            ))}
          </div>
          <div className="f-misc" style={{ background: 'rgba(91,114,83,0.06)', borderLeft: '3px solid var(--moss)', padding: '6px 10px', borderRadius: '0 6px 6px 0', fontSize: 12.5 }}>
            {c.portrait_prompt_effective}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button className="btn small" disabled={disabled || saving}
              onClick={() => setDraft(c.portrait_prompt_override || c.portrait_prompt_effective || '')}>改画像描述</button>
            <button className="btn small" disabled={disabled || saving} onClick={regenerate}>
              {c.ref_image_url ? '重新生成当前造型包' : '单独生成造型包'}
            </button>
          </div>
        </>
      ) : (
        <>
          <textarea rows={4} style={{ fontSize: 12.5 }} value={draft} onChange={e => setDraft(e.target.value)}
            placeholder="描述定妆照画面：画风、人物外观、姿态、背景……（10~400 字）" />
          <div style={{ fontSize: 12, color: draftValid ? 'var(--ink-faint)' : 'var(--cinnabar)', marginTop: 4 }}>
            {draftLen === 0 ? '留空并保存将恢复默认' : `${draftLen} / 10~400 字`}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button className="btn small primary" disabled={saving || disabled || !draftValid || draftLen === 0}
              onClick={() => save(true)}>保存并重新定妆</button>
            <button className="btn small" disabled={saving || !draftValid} onClick={() => save(false)}>仅保存</button>
            {isOverridden && <button className="btn small" disabled={saving}
              onClick={() => {
                if (!window.confirm('确认恢复默认画像描述？不会触发生成或扣费。')) return
                void save(false, '')
              }}>恢复默认</button>}
            <button className="btn small ghost" disabled={saving} onClick={() => setDraft(null)}>放弃</button>
          </div>
        </>
      )}
    </div>
  )
}

function portraitVersionLabel(portrait: Portrait): string {
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
    title: string,
    precheckBody: { character?: string; characters?: string[]; resume?: boolean; view_role?: string },
    action: (quote: RefsCostPrecheck) => Promise<void>,
  ) => Promise<void>
}) {
  const { toast } = useNav()
  const trackRef = useRef<HTMLDivElement>(null)
  const [redoing, setRedoing] = useState<string | null>(null)
  const portraits = [...(character.portraits ?? [])]
    .filter(portrait =>
      (!!portrait.image_url || (portrait.views ?? []).some(v => v.image_url)),
    )
    .sort((a, b) => b.ep_start - a.ep_start)
  const slides: PortraitSlide[] = []
  portraits.forEach((portrait, versionIndex) => {
    const views = (portrait.views ?? []).filter(view => !!view.image_url)
    if (views.length) {
      slides.push(...views.map(view => ({
        key: `${portrait.id}:${view.id}`,
        imageUrl: view.image_url!,
        portrait,
        versionIndex,
        view,
      })))
      return
    }
    if (portrait.image_url) {
      slides.push({
        key: portrait.id || `${portrait.ep_start}:${portrait.image_url}`,
        imageUrl: portrait.image_url,
        portrait,
        versionIndex,
        view: null,
      })
    }
  })
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

  const scroll = (direction: -1 | 1) => {
    const track = trackRef.current
    if (!track) return
    track.scrollBy({ left: direction * track.clientWidth, behavior: 'smooth' })
  }

  const redoView = async (portraitId: string, viewRole: string) => {
    const label = VIEW_ROLE_LABELS[viewRole] || viewRole
    await onPayRequest(
      `重做「${character.name}」的${label}视角`,
      { character: character.name, view_role: viewRole },
      async (quote) => {
        setRedoing(`${portraitId}:${viewRole}`)
        try {
          const result = await api.regenerateCharacterView(
            projectId, character.name, portraitId, viewRole,
            { confirm: true, quote_id: quote.quote_id },
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
              style={{ opacity: fitting ? 0.45 : 1, transition: 'opacity 0.3s' }} />
            {portrait && <figcaption className="portrait-version-label">
              <span>
                {index + 1}/{count} · {portraitVersionLabel(portrait)} · {VIEW_ROLE_LABELS[view?.view_role || ''] || view?.view_role || '正面'}
                {portrait.ep_end != null
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
                onClick={() => void redoView(portrait.id!, view.view_role!)}
              >
                {redoing === `${portrait.id}:${view.view_role}` ? '受理中…' : '重做当前视角'}
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
          <span>{count} 张视角图 · 横滑</span>
          <button type="button" onClick={() => scroll(-1)} aria-label="上一张定妆照">‹</button>
          <button type="button" onClick={() => scroll(1)} aria-label="下一张定妆照">›</button>
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
        <p>当前仍有 {data.count} 个角色缺少可用定妆照或 QA 未通过。继续后，分镜阶段可能需要自动补图或暂停等待。</p>
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
  canMerge: boolean
  onClose: () => void
  onMerge: () => void
  onRefresh: () => void
}) {
  const trapRef = useFocusTrap(true, onClose)
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label="版本冲突">
        <h3>人物谱版本冲突</h3>
        <p>{conflict.message}</p>
        {typeof conflict.current_version === 'number' && (
          <p>服务端当前版本：第 {conflict.current_version} 稿</p>
        )}
        {conflict.server_bible ? (
          <div className="conflict-merge-grid">
            <div>
              <b>仅服务端新增</b>
              <p>{onlyServer.length ? onlyServer.join('、') : '无'}</p>
            </div>
            <div>
              <b>仅本地修订</b>
              <p>{onlyLocal.length ? onlyLocal.join('、') : '无'}</p>
            </div>
            <div>
              <b>双方都有</b>
              <p>{both.length ? both.join('、') : '无'}</p>
            </div>
          </div>
        ) : !!conflict.character_names?.length && (
          <p>服务端角色：{conflict.character_names.join('、')}</p>
        )}
        <p>请处理冲突后继续；禁止用旧页面静默覆盖。</p>
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>留下继续查看</button>
          {canMerge && (
            <button type="button" className="btn" onClick={onMerge}>采用服务端新角色并继续</button>
          )}
          <button type="button" className="btn primary" onClick={onRefresh}>刷新放弃</button>
        </div>
      </section>
    </div>
  )
}

export function EpStamp({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    planned: ['待分镜', 'grey'], scripting: ['分镜中', 'gold'], scripted: ['待确认', 'blue'],
    script_failed: ['分镜失败', 'red'], confirmed: ['已确认', 'green'],
    generating: ['生成中', 'gold'], done: ['成片', 'green'],
  }
  const [label, color] = map[status] ?? [status, 'grey']
  return <span className={`stamp ${color}`}>{label}</span>
}
