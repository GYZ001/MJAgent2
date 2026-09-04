import { useCallback, useEffect, useState } from 'react'
import { api, type PropItem } from '../api'
import { useNav, useProject } from '../App'
import PrepSubnav from '../components/PrepSubnav'
import QueryState from '../components/QueryState'
import { formatBookTitle } from '../lib/bookTitle'

/**
 * 世界书·物件库：关键道具的规范外观与参考图。
 *
 * 道具由映射台在识别人物/场景的同一轮里提名（判据见 app/props/judge.py），参考图生成后
 * 与人物谱定妆照、场景库参考图走同一条路径进分镜与视频生成（提示词里的 @图片N 固定参考）。
 * 本页只做展示与重出图，不提供手工新增——道具必须在原文里站得住脚，不能凭空造。
 */
export default function PropsPage() {
  const { projectId, toast } = useNav()
  const { data: p, refresh, error, status, loading } = useProject(projectId!)
  const [items, setItems] = useState<PropItem[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [busyName, setBusyName] = useState<string | null>(null)

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

      <section className="card">
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
        {!listError && items !== null && items.length === 0 && (
          <div className="empty" role="status">
            还没有道具。映射台识别人物与场景时会一并提名关键道具（原文里反复出现、
            跨段要保持形态一致的物件），识别后会出现在这里。
          </div>
        )}
        {items !== null && items.length > 0 && (
          <div className="figure-grid">
            {items.map(item => (
              <PropCard key={item.name} item={item} busy={busyName === item.name}
                onRegenerate={() => void regenerate(item.name)} />
            ))}
          </div>
        )}
      </section>
    </>
  )
}

export function propStamp(status: string, hasImage: boolean): { color: string; label: string } {
  if (status === 'ready' && hasImage) return { color: 'green', label: '已出图' }
  if (status === 'running' || status === 'pending') return { color: 'gold', label: '出图中' }
  if (status === 'failed') return { color: 'red', label: '出图失败' }
  return { color: 'grey', label: '未出图' }
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
