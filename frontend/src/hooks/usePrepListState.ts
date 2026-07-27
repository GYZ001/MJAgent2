import { useEffect, useState } from 'react'

export interface PrepListState {
  search: string
  page: number
  pageSize: number
  filters: Record<string, string>
  sort: string
  scrollY: number
}

const DEFAULT: PrepListState = {
  search: '',
  page: 0,
  pageSize: 0,
  filters: {},
  sort: 'name',
  scrollY: 0,
}

function storageKey(projectId: string, pageKey: string) {
  return `prep-list:${projectId}:${pageKey}`
}

/** 前期准备列表状态：跨详情返回时恢复筛选/页码/滚动。 */
export function usePrepListState(projectId: string, pageKey: string, fallbackPageSize: number) {
  const [state, setState] = useState<PrepListState>(() => {
    try {
      const raw = sessionStorage.getItem(storageKey(projectId, pageKey))
      if (!raw) return { ...DEFAULT, pageSize: fallbackPageSize }
      return { ...DEFAULT, ...JSON.parse(raw), pageSize: fallbackPageSize || DEFAULT.pageSize }
    } catch {
      return { ...DEFAULT, pageSize: fallbackPageSize }
    }
  })

  useEffect(() => {
    setState(prev => ({ ...prev, pageSize: fallbackPageSize || prev.pageSize }))
  }, [fallbackPageSize])

  useEffect(() => {
    try {
      sessionStorage.setItem(storageKey(projectId, pageKey), JSON.stringify(state))
    } catch { /* ignore quota */ }
  }, [projectId, pageKey, state])

  return [state, setState] as const
}
