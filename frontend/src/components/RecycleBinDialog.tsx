import { useEffect } from 'react'
import QueryState from './QueryState'
import type { DeletedProject } from '../api'

/**
 * 回收站模态。
 *
 * 做成模态而不是页内展开区：它是「偶尔进去处理一下」的场所，页内展开会把正在
 * 看的项目列表整个顶下去（用户 2026-08-30 反馈「为什么只是页面内的一个窗口」）。
 * 复用全站通用的 .dialog-backdrop / .dialog，不自创第二套模态体系。
 *
 * 从 Studio.tsx 抽出：加完模态包裹后该文件 518/497 行撞线，按 CLAUDE.md
 * 「装不下时先想怎么拆，不要先想加基线」。
 */
function formatRetention(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (hours > 0) return `约 ${hours} 小时 ${minutes} 分钟后彻底清理`
  if (minutes > 0) return `约 ${minutes} 分钟后彻底清理`
  return '即将彻底清理'
}

export interface RecycleBinDialogProps {
  deletedProjects: DeletedProject[] | null | undefined
  deletedCount: number
  deletedLoading: boolean
  deletedError: string | null | undefined
  busyId: string | null
  purgingAll: boolean
  onRestore: (p: DeletedProject) => void
  onPurge: (p: DeletedProject) => void
  onPurgeAll: () => void
  onClose: () => void
  onRefresh: () => void
}

export function RecycleBinDialog(props: RecycleBinDialogProps) {
  const {
    deletedProjects, deletedCount, deletedLoading, deletedError,
    busyId, purgingAll, onRestore, onPurge, onPurgeAll, onClose, onRefresh,
  } = props

  // 模态必须能用 Esc 关掉：没有键盘出口的弹窗会把键盘用户和读屏用户困在里面。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose() }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  return (
    <div
      className="dialog-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="回收站"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
    <section id="recycle-bin-panel" className="dialog recycle-bin-panel">
      <div className="section-heading">
        <div><span className="eyebrow">回收站</span><h3>已删除的项目</h3></div>
        <button
          className="btn small ghost"
          type="button"
          aria-label="关闭回收站"
          onClick={() => onClose()}
        >关闭</button>
      </div>
      <p className="dialog-hint">24 小时保留期内可随时恢复；到期或手动彻底删除后不可恢复</p>
      <QueryState
        loading={deletedLoading}
        error={deletedError}
        hasData={deletedCount > 0}
        objectName="回收站项目"
        emptyText="回收站是空的。"
        onRetry={onRefresh}
      >
        {deletedCount > 0 && (
          <>
            <ul className="recycle-bin-list">
              {deletedProjects!.map(p => (
                <li key={p.id} className="recycle-bin-row">
                  <div className="recycle-bin-info">
                    <b>{p.name}</b>
                    <span>{p.chapter_count} 章 · {p.episode_count} 集 · {formatRetention(p.retention_seconds_remaining)}</span>
                  </div>
                  <div className="recycle-bin-actions">
                    <button
                      className="btn small"
                      type="button"
                      disabled={busyId === p.id}
                      aria-label={`恢复项目《${p.name}》`}
                      onClick={() => { void onRestore(p) }}
                    >
                      {busyId === p.id ? '处理中…' : '恢复'}
                    </button>
                    <button
                      className="btn small danger"
                      type="button"
                      disabled={busyId === p.id}
                      aria-label={`彻底删除项目《${p.name}》，不可恢复`}
                      onClick={() => { void onPurge(p) }}
                    >
                      彻底删除
                    </button>
                  </div>
                </li>
              ))}
            </ul>
            <div className="recycle-bin-footer">
              <button
                className="btn danger"
                type="button"
                disabled={purgingAll}
                aria-label={purgingAll
                  ? '清空回收站正在后台执行，可关闭本窗口，完成后会有提示'
                  : '清空回收站，彻底删除全部已软删除的项目'}
                onClick={() => { void onPurgeAll() }}
              >
                {/* 后台执行中：不装一个笼统的"清空中…"，用列表本身当前还剩几个
                    项目做进度指示——deletedCount 随轮询更新，见 useRecycleBin。 */}
                {purgingAll ? `清空中…（剩 ${deletedCount} 个，可关闭本窗口）` : '清空回收站'}
              </button>
            </div>
          </>
        )}
      </QueryState>
    </section>
    </div>

  )
}
