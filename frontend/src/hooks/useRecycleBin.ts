import { useState } from 'react'
import { api, DeletedProject } from '../api'
import { usePoll } from '../App'
import { useDeleteConfirm } from './useDeleteConfirm'

/** 轮询间隔：清空/彻底删除在途时加快到 2s，让列表本身如实反映"还剩几个"；
 *  空闲时退回 15s，不拖累后端。 */
const POLL_IDLE_MS = 15000
const POLL_ACTIVE_MS = 2000

/**
 * 回收站的列表轮询 + 恢复/彻底删除/清空三个动作，从 Studio.tsx 抽出
 * （2026-08-31，见 useDeleteConfirm.runBackground 的说明）。
 *
 * purge / purgeAll 用 runBackground：用户点"确认删除"后立刻放行，不再把
 * 后端那次同步到底的清理请求焊在 UI 上——用户原话"不要阻挡用户的后续操作"。
 * 真正结果（含部分失败）到达时用全站 toast 提醒，不吞、不自造第二套通知。
 * restore 是单行 UPDATE，本就很快，维持原样同步 await。
 */
export function useRecycleBin(
  toast: (msg: string, isErr?: boolean) => void,
  refreshProjects: () => void,
) {
  const [busyId, setBusyId] = useState<string | null>(null)
  const [purgingAll, setPurgingAll] = useState(false)
  const {
    data: deletedProjects, refresh: refreshDeleted, error: deletedError, loading: deletedLoading,
  } = usePoll<DeletedProject[]>(
    () => api.listDeletedProjects(),
    () => (purgingAll || busyId ? POLL_ACTIVE_MS : POLL_IDLE_MS),
  )
  const deleteConfirm = useDeleteConfirm()
  const deletedCount = deletedProjects?.length ?? 0

  async function restore(p: DeletedProject) {
    setBusyId(p.id)
    try {
      await api.restoreProject(p.id)
      toast(`《${p.name}》已恢复`)
      refreshProjects()
      refreshDeleted()
      window.dispatchEvent(new Event('manju:projects-changed'))
    } catch (e: unknown) { toast((e as Error).message, true) } finally {
      setBusyId(null)
    }
  }

  function purge(p: DeletedProject) {
    setBusyId(p.id)
    void deleteConfirm.runBackground(
      () => api.purgeProject(p.id),
      outcome => {
        setBusyId(null)
        refreshDeleted()
        if (!outcome.ok) {
          toast(`《${p.name}》彻底删除失败：${(outcome.error as Error).message}`, true)
          return
        }
        toast(`《${p.name}》已彻底删除`)
      },
    ).then(submitted => { if (!submitted) setBusyId(null) })
  }

  function purgeAll() {
    setPurgingAll(true)
    void deleteConfirm.runBackground(
      () => api.purgeAllDeletedProjects(),
      outcome => {
        setPurgingAll(false)
        refreshDeleted()
        if (!outcome.ok) {
          toast(`清空回收站失败：${(outcome.error as Error).message}`, true)
          return
        }
        const { purged_count, failed } = outcome.value
        const msg = failed.length
          ? `已彻底删除 ${purged_count} 个项目，${failed.length} 个失败：${failed.map(f => f.error).join('；')}`
          : `回收站已清空，彻底删除 ${purged_count} 个项目`
        toast(msg, Boolean(failed.length))
      },
    ).then(submitted => { if (!submitted) setPurgingAll(false) })
  }

  return {
    deletedProjects, deletedCount, deletedLoading, deletedError,
    busyId, purgingAll, restore, purge, purgeAll, refreshDeleted,
    dialog: deleteConfirm.dialog,
  }
}
