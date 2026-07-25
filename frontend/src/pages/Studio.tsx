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
  const [showImport, setShowImport] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const [drag, setDrag] = useState(false)

  async function submit(file: File) {
    if (uploading) return
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const attachment = await api.upload('/attachments/novel', form) as { attachment_token: string }
      const res = await api.post('/projects/import', {
        attachment_token: attachment.attachment_token,
        name: name || file.name.replace(/\.txt$/i, ''),
      })
      toast(`《${name || file.name}》已摄入：${res.ingestion.chapter_count} 章，${res.ingestion.total_chars} 字${res.ingestion.auto_split ? '（未识别到章节标题，已按字数切分）' : ''}`)
      setName('')
      setShowImport(false)
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
        <div className="crumb">漫剧案头 / 项目中心</div>
        <div className="page-title-row">
          <h1>项目中心 <span className="sub">从原著到成片，继续你的制作进度</span></h1>
          <button className="btn primary" type="button" onClick={() => setShowImport(value => !value)}>
            {showImport ? '收起导入' : '＋ 导入小说'}
          </button>
        </div>
        <hr className="rule" />
      </header>

      {(showImport || !projects?.length) && <section className="card import-panel">
        <div className="section-heading">
          <div><span className="eyebrow">NEW PROJECT</span><h3>导入一部小说</h3></div>
          <span className="hint">支持 TXT，自动识别编码并切分章节</span>
        </div>
        <div className="import-grid">
          <div>
            <label className="f">书名（留空则取文件名）</label>
            <input type="text" value={name} onChange={e => setName(e.target.value)} placeholder="例如：凡人修仙传" />
          </div>
          <div
            className={`upload-zone ${drag ? 'drag' : ''}`}
            onClick={() => fileRef.current?.click()}
            onDragOver={e => { e.preventDefault(); setDrag(true) }}
            onDragLeave={() => setDrag(false)}
            onDrop={e => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files[0]; if (f) submit(f) }}
          >
            <b>{uploading ? '正在导入并切分章节…' : '选择 TXT 文件'}</b>
            <span>{uploading ? '请保持页面开启' : '或将文件拖到这里'}</span>
          </div>
        </div>
        <input ref={fileRef} type="file" accept=".txt" hidden
          onChange={e => { const f = e.target.files?.[0]; if (f) submit(f); e.target.value = '' }} />
      </section>}

      {!projects?.length ? (
        <div className="empty"><div className="big">卷</div>书房尚空<br />上传第一本小说开始</div>
      ) : (
        <section className="project-section">
          <div className="section-heading">
            <div><span className="eyebrow">YOUR PROJECTS</span><h2>正在制作</h2></div>
            <span className="hint">{projects.length} 个项目</span>
          </div>
          <div className="shelf">
            {projects.map(p => {
              const [label, color] = STATUS_LABEL[p.status] ?? [p.status, 'grey']
              const failed = p.bible_status === 'failed' || p.plan_status === 'failed'
              return (
                <article key={p.id} className="volume">
                  <button className="volume-open" type="button" onClick={() => go('bible', p.id, null)}>
                    <div className="volume-cover" aria-hidden="true"><span>漫</span></div>
                    <div className="volume-content">
                      <div className="v-title">{p.name}</div>
                      <div className="v-meta">{(p.novel_chars / 10000).toFixed(1)} 万字 · {p.chapter_count} 章 · {p.episode_count} 集</div>
                      <div className="project-progress" aria-label="项目制作阶段">
                        <span className="done">原著</span><i /><span className={p.bible_status === 'ready' ? 'done' : ''}>人物</span><i />
                        <span className={p.plan_status === 'ready' ? 'done' : ''}>分集</span><i /><span>成片</span>
                      </div>
                    </div>
                    <span className="project-enter">继续制作 →</span>
                  </button>
                  <div className="v-foot">
                    <span className={`stamp ${failed ? 'red' : color}`}>{failed ? '需要处理' : label}</span>
                    {p.bible_status === 'running' && <span className="stamp gold">人物谱生成中</span>}
                    {p.plan_status === 'running' && <span className="stamp gold">分集生成中</span>}
                    <button className="project-delete" type="button" onClick={e => remove(p, e)} aria-label={`删除${p.name}`}>删除项目</button>
                  </div>
                </article>
              )
            })}
          </div>
        </section>
      )}
    </>
  )
}
