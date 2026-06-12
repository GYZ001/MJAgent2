import { useRef, useState } from 'react'
import { api, Project } from '../api'
import { useNav, usePoll } from '../App'

const STATUS_LABEL: Record<string, [string, string]> = {
  created: ['新建', 'grey'], ingested: ['已摄入', 'blue'],
  bible_ready: ['谱成', 'blue'], planned: ['已分集', 'green'],
}

export default function Studio() {
  const { go, toast } = useNav()
  const { data: projects, refresh } = usePoll<Project[]>(() => api.get('/projects'), 6000)
  const [uploading, setUploading] = useState(false)
  const [name, setName] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const [drag, setDrag] = useState(false)

  async function submit(file: File) {
    if (uploading) return
    setUploading(true)
    try {
      const form = new FormData()
      form.append('name', name || file.name.replace(/\.txt$/i, ''))
      form.append('file', file)
      const res = await api.upload('/projects', form)
      toast(`《${name || file.name}》已摄入：${res.ingestion.chapter_count} 章，${res.ingestion.total_chars} 字${res.ingestion.auto_split ? '（未识别到章节标题，已按字数切分）' : ''}`)
      setName('')
      refresh()
      go('bible', res.project_id, null)
    } catch (e: unknown) {
      toast(`摄入失败：${(e as Error).message}`, true)
    } finally {
      setUploading(false)
    }
  }

  async function remove(p: Project, ev: React.MouseEvent) {
    ev.stopPropagation()
    if (!window.confirm(`确定删除《${p.name}》？将删除全部章节、剧集、分镜与已生成视频，不可恢复。`)) return
    try {
      await api.del(`/projects/${p.id}`)
      toast(`已删除《${p.name}》`)
      refresh()
    } catch (e: unknown) { toast((e as Error).message, true) }
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">漫剧案头 / 书房</div>
        <h1>书房 <span className="sub">一书一世界，自此开卷</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <h3>开新卷 <span className="hint">上传 TXT 小说，自动识别编码与章节</span></h3>
        <div style={{ display: 'flex', gap: 12, marginBottom: 14 }}>
          <div style={{ flex: 1 }}>
            <label className="f">书名（留空则取文件名）</label>
            <input type="text" style={{ width: '100%' }} value={name} onChange={e => setName(e.target.value)} placeholder="如：凡人修仙传" />
          </div>
        </div>
        <div
          className={`upload-zone ${drag ? 'drag' : ''}`}
          onClick={() => fileRef.current?.click()}
          onDragOver={e => { e.preventDefault(); setDrag(true) }}
          onDragLeave={() => setDrag(false)}
          onDrop={e => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files[0]; if (f) submit(f) }}
        >
          {uploading ? '摄入中，正在切章与清洗……' : '点击选择或拖入 .txt 文件'}
        </div>
        <input ref={fileRef} type="file" accept=".txt" hidden
          onChange={e => { const f = e.target.files?.[0]; if (f) submit(f); e.target.value = '' }} />
      </section>

      <div style={{ height: 26 }} />

      {!projects?.length ? (
        <div className="empty"><div className="big">卷</div>书房尚空<br />上传第一本小说开始</div>
      ) : (
        <div className="shelf">
          {projects.map(p => {
            const [label, color] = STATUS_LABEL[p.status] ?? [p.status, 'grey']
            return (
              <div key={p.id} className="volume" onClick={() => go('bible', p.id, null)}>
                <div className="v-title">{p.name}</div>
                <div className="v-meta">
                  {(p.novel_chars / 10000).toFixed(1)} 万字 · {p.chapter_count} 章<br />
                  已规划 {p.episode_count} 集
                </div>
                <div className="v-foot">
                  <span className={`stamp ${color}`}>{label}</span>
                  {p.bible_status === 'running' && <span className="stamp gold">谱写中</span>}
                  {p.plan_status === 'running' && <span className="stamp gold">分集中</span>}
                  {(p.bible_status === 'failed' || p.plan_status === 'failed') && <span className="stamp red">有失败</span>}
                  <span style={{ flex: 1 }} />
                  <button className="btn small ghost" onClick={e => remove(p, e)}>毁版</button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
