import { useEffect, useState } from 'react'
import { getProjectVoices, type ProjectVoices, type VoiceItem, type VoiceVersion } from '../../api/voices'
import { characterNameFromIdentity } from '../../lib/bibleAssets'

/**
 * 项目声音数据的共享缓存（方案 5.4 节：「同一页面多个段落面板共用一次请求」）。
 * 分镜台/生成台一页会挂很多个 SegmentResourcePanel 实例，每个都要查"这个角色
 * 有没有当前声音"——如果各自独立 usePoll，N 个面板就是 N 份轮询。这里按
 * projectId 做模块级缓存 + 订阅：谁先挂载谁触发首次请求，后来者共享同一份数据，
 * 只有 items 里存在 generating 时才安排下一次刷新，没有就停（不拖累后端）。
 *
 * 没有走 App.tsx 的 usePoll：usePoll 每个调用点各自起一个 AdaptivePoller，无法
 * 跨组件实例共享同一个定时器；这里需要的恰恰是"多处订阅、一次请求"。
 */

interface CacheEntry {
  data: ProjectVoices | null
  loading: boolean
  error: string | null
  listeners: Set<() => void>
  timer: ReturnType<typeof setTimeout> | null
  inFlight: Promise<void> | null
}

/** 导出仅供测试引用，避免测试里硬编码一份重复的魔数。 */
export const GENERATING_POLL_MS = 3000

const cache = new Map<string, CacheEntry>()

function entryFor(projectId: string): CacheEntry {
  let entry = cache.get(projectId)
  if (!entry) {
    entry = { data: null, loading: true, error: null, listeners: new Set(), timer: null, inFlight: null }
    cache.set(projectId, entry)
  }
  return entry
}

function notify(entry: CacheEntry): void {
  entry.listeners.forEach(listener => listener())
}

function scheduleIfGenerating(projectId: string, entry: CacheEntry): void {
  if (entry.timer) {
    clearTimeout(entry.timer)
    entry.timer = null
  }
  if (entry.listeners.size === 0) return
  const busy = entry.data?.items.some(item => item.generating) ?? false
  if (!busy) return
  entry.timer = setTimeout(() => { void load(projectId, { silent: true }) }, GENERATING_POLL_MS)
}

function load(projectId: string, options: { silent?: boolean } = {}): Promise<void> {
  const entry = entryFor(projectId)
  if (entry.inFlight) return entry.inFlight
  if (!options.silent) {
    entry.loading = true
    notify(entry)
  }
  const task = getProjectVoices(projectId)
    .then(data => {
      entry.data = data
      entry.error = null
    })
    .catch((err: unknown) => {
      entry.error = err instanceof Error ? err.message : String(err)
    })
    .finally(() => {
      entry.loading = false
      entry.inFlight = null
      notify(entry)
      scheduleIfGenerating(projectId, entry)
    })
  entry.inFlight = task
  return task
}

export interface UseProjectVoicesResult {
  data: ProjectVoices | null
  loading: boolean
  error: string | null
  /** 生成/采用成功后调用方主动调用一次，立刻拉最新状态，不等 3 秒轮询。 */
  refresh: () => void
}

export function useProjectVoices(projectId: string | null | undefined): UseProjectVoicesResult {
  const [, forceRender] = useState(0)

  useEffect(() => {
    if (!projectId) return
    const entry = entryFor(projectId)
    const listener = () => forceRender(tick => tick + 1)
    const isFirstListener = entry.listeners.size === 0
    entry.listeners.add(listener)
    if (isFirstListener) void load(projectId)
    return () => {
      entry.listeners.delete(listener)
      if (entry.listeners.size === 0 && entry.timer) {
        clearTimeout(entry.timer)
        entry.timer = null
      }
    }
  }, [projectId])

  if (!projectId) return { data: null, loading: false, error: null, refresh: () => undefined }
  const entry = entryFor(projectId)
  return {
    data: entry.data,
    loading: entry.loading,
    error: entry.error,
    refresh: () => { void load(projectId) },
  }
}

export function findVoiceItem(data: ProjectVoices | null, characterName: string | null | undefined): VoiceItem | null {
  if (!data || !characterName) return null
  return data.items.find(item => item.character_name === characterName) ?? null
}

/** 段落素材面板按 identity_id（`bible:名字`/`entity:哈希`）查角色当前声音；
 *  群演（无 bible: 前缀）恒为 null——声音只覆盖有卡的具名角色。 */
export function findCurrentVoiceByIdentity(
  data: ProjectVoices | null,
  identityId: string | null | undefined,
): VoiceVersion | null {
  const name = characterNameFromIdentity(identityId)
  return findVoiceItem(data, name)?.current ?? null
}
