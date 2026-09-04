import { useCallback, useEffect, useState } from 'react'
import { api, type PropItem } from '../api'
import { useNav, useProject } from '../App'
import PrepSubnav from '../components/PrepSubnav'
import QueryState from '../components/QueryState'
import SearchField from '../components/SearchField'
import { SINGLE_ROW_ASSET_PAGE, useFillPageSize } from '../hooks/useFillPageSize'
import { usePrepListState } from '../hooks/usePrepListState'
import { formatBookTitle } from '../lib/bookTitle'

/**
 * 世界书·物件库：关键道具的规范外观与参考图。
 *
 * 道具由映射台在识别人物/场景的同一轮里提名（判据见 app/props/judge.py），参考图生成后
 * 与人物谱定妆照、场景库参考图走同一条路径进分镜与视频生成（提示词里的 @图片N 固定参考）。
 * 本页只做展示与重出图，不提供手工新增——道具必须在原文里站得住脚，不能凭空造。
 * 工具栏（搜索/状态筛选/计数/分页）与场景库同一套 hook 与样式，列表状态按项目记在 sessionStorage。
 */
export type PropStatusFilter = '' | 'ready' | 'missing' | 'failed'

export function propStamp(status: string, hasImage: boolean): { color: string; label: string } {
  if (status === 'ready' && hasImage) return { color: 'green', label: '已出图' }
  if (status === 'running' || status === 'pending') return { color: 'gold', label: '出图中' }
  if (status === 'failed') return { color: 'red', label: '出图失败' }
  return { color: 'grey', label: '未出图' }
}

/** 状态筛选的判据与卡片角标一致：ready 且真有图才算「已出图」。 */
export function propStatusBucket(item: Pick<PropItem, 'status' | 'image_url'>): Exclude<PropStatusFilter, ''> {
  if (item.status === 'failed') return 'failed'
  return item.status === 'ready' && Boolean(item.image_url) ? 'ready' : 'missing'
}

/** 搜索命中名称、别名或规范外观；结果按名称中文排序。 */
export function filterPropItems(items: PropItem[], query: string, status: PropStatusFilter): PropItem[] {
  const q = query.trim()
  return items.filter(item => {
    if (q && !item.name.includes(q) && !item.aliases.some(a => a.includes(q)) && !(item.appearance || '').includes(q)) return false
    if (status && propStatusBucket(item) !== status) return false
    return true
  }).sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
}

export default function PropsPage() {
  const { projectId, toast } = useNav()
  // 只需要项目名与状态，用最轻的 episodes 投影（不带世界书 JSON）。
  const { data: p, refresh, error, status, loading } = useProject(projectId!, undefined, 'episodes')
  const [items, setItems] = useState<PropItem[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [busyName, setBusyName] = useState<string | null>(null)
  const [pageSize, gridRef] = useFillPageSize(SINGLE_ROW_ASSET_PAGE)
  const [listState, setListState] = usePrepListState(projectId!, 'prop-library', pageSize)
  const search = listState.search
  const page = listState.page
  const statusFilter = (listState.filters.status || '') as PropStatusFilter
  const setSearch = (value: string) => setListState(current => ({ ...current, search: value, page: 0 }))
  const setPage = (value: number) => setListState(current => ({ ...current, page: value, scrollY: window.scrollY }))
  const setStatusFilter = (value: string) => setListState(current => ({
    ...current, filters: { ...current.filters, status: value }, page: 0,
  }))
  const resetList = () => {
    setListState(current => ({ ...current, search: '', filters: {}, page: 0 }))
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLInputElement>('input[aria-label="搜索道具"]')?.focus()
    })
  }

  const load = useCallback(async () => {
    if (!projectId) return
    try {
      const res = await api.listProps(projectId)
      setItems(res.items)
      setListError(null)
    } catch (e) {
      setListError(e instanceof Error ? e.message : String(e))
    }
  }, [projectId])
  useEffect(() => { void load() }, [load])

  const all = items ?? []
  const query = search.trim()
  const hasCriteria = Boolean(query) || Boolean(statusFilter)
  const filtered = filterPropItems(all, query, statusFilter)
  const pageCount = pageSize > 0 ? Math.max(1, Math.ceil(filtered.length / pageSize)) : 1
  const curPage = Math.min(page, pageCount - 1)
  const paged = pageSize > 0 ? filtered.slice(curPage * pageSize, (curPage + 1) * pageSize) : filtered
  useEffect(() => {
    if (page > pageCount - 1) setPage(Math.max(0, pageCount - 1))
  }, [page, pageCount])

  if (error && !p) return <QueryState loading={false} error={error} status={status} hasData={false} objectName="物件库" onRetry={refresh}>{null}</QueryState>
  if (!p) return <QueryState loading={loading} hasData={false} objectName="物件库" onRetry={refresh}>{null}</QueryState>

  const regenerate = async (name: string) => {
    if (!projectId || busyName) return
    setBusyName(name)
    try {
      const updated = await api.regenerateProp(projectId, name)
      setItems(current => (current ?? []).map(item => (item.name === name ? { ...item, ...updated } : item)))
      toast(updated.status === 'ready' ? `已重新生成「${name}」参考图` : `「${name}」出图失败，可再试一次`)
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyName(null)
    }
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / {formatBookTitle(p.name)}</div>
        <PrepSubnav current="props" />
        <h1>物件库 <span className="sub">关键道具的规范外观与参考图，随分镜固定传给视频生成</span></h1>
        <hr className="rule" />
      </header>

      <section className="card scene-library">
        <h3>道具参考图
          <span className="hint">道具在映射台随人物、场景一起发现；参考图与定妆照、场景图同一条路径进视频生成</span>
        </h3>
        {listError && (
          <div className="empty" role="alert">
            物件库读取失败：{listError}
            <button type="button" className="btn small" onClick={() => void load()}>重试</button>
          </div>
        )}
        {!listError && items === null && <div className="empty" role="status">正在读取物件库…</div>}
        {items !== null && (
          <>
            <div className="library-toolbar">
              <SearchField value={search} onChange={value => setSearch(value)}
                placeholder="搜索道具名、别名或外观…" ariaLabel="搜索道具" className="library-search" />
              <select aria-label="出图状态筛选" value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
                <option value="">全部出图状态</option><option value="ready">已出图</option>
                <option value="missing">未出图</option><option value="failed">出图失败</option>
              </select>
              {hasCriteria && <button type="button" className="btn small ghost" onClick={resetList}>清除搜索与筛选</button>}
              <span className="library-result-count" role="status">
                共 {all.length} 个道具{hasCriteria ? ` · 当前显示 ${filtered.length}` : ''}
              </span>
            </div>
            <div ref={gridRef} className="figure-grid">
              {paged.map(item => (
                <PropCard key={item.name} item={item} busy={busyName === item.name}
                  onRegenerate={() => void regenerate(item.name)} />
              ))}
            </div>
            {pageSize > 0 && !paged.length && (
              <div className="library-filter-empty" role="status">
                <b>{hasCriteria ? '没有符合当前条件的道具' : '物件库暂无道具'}</b>
                <p>{hasCriteria
                  ? `${query ? `搜索“${query}”` : '当前出图状态筛选'}未命中；清除条件后可恢复全部 ${all.length} 个道具。`
                  : '映射台识别人物与场景时会一并提名关键道具（原文里反复出现、跨段要保持形态一致的物件），识别后会出现在这里。'}</p>
                {hasCriteria && <button type="button" className="btn small" onClick={resetList}>清除搜索与筛选</button>}
              </div>
            )}
            {pageCount > 1 && (
              <div className="library-pagination" aria-label="道具分页">
                <button className="btn small" disabled={curPage <= 0}
                  aria-label={curPage <= 0 ? '上一页，暂不可用：当前已是第一页' : '上一页'}
                  onClick={() => setPage(curPage - 1)}>← 上一页</button>
                <span>第 {curPage + 1} / {pageCount} 页</span>
                <button className="btn small" disabled={curPage >= pageCount - 1}
                  aria-label={curPage >= pageCount - 1 ? '下一页，暂不可用：当前已是最后一页' : '下一页'}
                  onClick={() => setPage(curPage + 1)}>下一页 →</button>
              </div>
            )}
          </>
        )}
      </section>
    </>
  )
}

function PropCard({ item, busy, onRegenerate }: { item: PropItem; busy: boolean; onRegenerate: () => void }) {
  const stamp = propStamp(item.status, Boolean(item.image_url))
  return (
    <article className="figure scene-card">
      <div className="f-name">{item.name}
        <span className={`stamp ${stamp.color}`}>{stamp.label}</span>
      </div>
      {item.image_url ? (
        <div className="scene-visual">
          <img src={item.image_url} alt={item.name} loading="lazy" decoding="async" />
        </div>
      ) : (
        <div className="scene-visual scene-image-error" role="status">
          <span>{item.status === 'failed' ? '参考图生成失败，可重新生成' : '尚未生成参考图'}</span>
        </div>
      )}
      <div className="scene-card-summary">
        <p className="hint">{item.appearance || '（世界书里还没有规范外观描述）'}</p>
        {item.aliases.length > 0 && <small className="hint">别名：{item.aliases.join('、')}</small>}
        <button type="button" className="btn small" disabled={busy} onClick={onRegenerate}>
          {busy ? '生成中…' : '重新生成参考图'}
        </button>
      </div>
    </article>
  )
}
