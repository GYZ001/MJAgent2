import { useEffect, useId, useRef, useState } from 'react'
import { api, Project } from '../api'
import { useNav, usePoll } from '../App'
import QueryState from '../components/QueryState'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { formatFileSize, novelTitleFromFilename, projectEntry, validateNovelFile } from './studioImport'

const STATUS_LABEL: Record<string, [string, string]> = {
  created: ['新建', 'grey'], ingested: ['已导入', 'blue'],
  bible_ready: ['人物谱就绪', 'blue'], planned: ['分集已规划', 'green'],
}

type ImportStage = 'idle' | 'selected' | 'uploading' | 'creating' | 'error'

export default function Studio() {
  const { go, toast } = useNav()
  const { data: projects, refresh, error, loading } = usePoll<Project[]>(() => api.get('/projects'), 6000)
  const [name, setName] = useState('')
  const [showImport, setShowImport] = useState(window.location.pathname === '/workspaces/new')
  const importTriggerRef = useRef<HTMLButtonElement | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [drag, setDrag] = useState(false)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [pendingAttachment, setPendingAttachment] = useState<{ fileKey: string; token: string } | null>(null)
  const [importStage, setImportStage] = useState<ImportStage>('idle')
  const [importError, setImportError] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null)
  const [deleting, setDeleting] = useState(false)
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null)
  const projectNameId = useId()
  const importPanelId = useId()
  const importHelpId = useId()
  const uploading = importStage === 'uploading' || importStage === 'creating'
  const emptyProjectList = !loading && !error && projects?.length === 0
  const importVisible = showImport || emptyProjectList
  const observabilityIntent = new URLSearchParams(window.location.search).get('intent') === 'observability'

  useEffect(() => {
    const sync = () => setShowImport(window.location.pathname === '/workspaces/new')
    window.addEventListener('popstate', sync)
    window.addEventListener('manju:locationchange', sync)
    return () => {
      window.removeEventListener('popstate', sync)
      window.removeEventListener('manju:locationchange', sync)
    }
  }, [])

  function rejectFile(message: string) {
    setSelectedFile(null)
    setPendingAttachment(null)
    setImportStage('error')
    setImportError(message)
    toast(message, true)
  }

  function selectFiles(files: FileList | null) {
    if (!files?.length || uploading) return
    if (files.length > 1) {
      rejectFile('一次只能导入一份小说，请重新选择')
      return
    }
    const file = files[0]
    const validationError = validateNovelFile(file)
    if (validationError) {
      rejectFile(validationError)
      return
    }
    setSelectedFile(file)
    setPendingAttachment(null)
    setImportStage('selected')
    setImportError(null)
  }

  async function submit(file: File) {
    if (uploading) return
    const validationError = validateNovelFile(file)
    if (validationError) {
      rejectFile(validationError)
      return
    }
    const projectName = name.trim() || novelTitleFromFilename(file.name)
    setSelectedFile(file)
    setImportError(null)
    setImportStage('uploading')
    try {
      const fileKey = `${file.name}:${file.size}:${file.lastModified}`
      let attachmentToken = pendingAttachment?.fileKey === fileKey ? pendingAttachment.token : ''
      if (!attachmentToken) {
        const form = new FormData()
        form.append('file', file)
        const attachment = await api.upload('/attachments/novel', form) as { attachment_token: string }
        attachmentToken = attachment.attachment_token
        setPendingAttachment({ fileKey, token: attachmentToken })
      }
      setImportStage('creating')
      const res = await api.post('/projects/import', {
        attachment_token: attachmentToken,
        name: projectName,
      })
      const planningRunning = res.episode_planning?.status === 'running'
      const assetStatus = res.asset_generation?.status
      const bootstrapMessage = planningRunning && assetStatus === 'running'
        ? '；自动分集、人物谱和素材准备已启动'
        : planningRunning && assetStatus === 'awaiting_confirmation'
          ? '；自动分集已启动，人物谱与定妆将在确认费用后继续'
          : '；部分后台准备未能启动，原文和项目已保留，请进入项目重试'
      toast(`《${projectName}》导入完成：${res.ingestion.chapter_count} 章，${res.ingestion.total_chars} 字${res.ingestion.auto_split ? '（未识别到章节标题，已按字数切分）' : ''}${bootstrapMessage}`)
      setName('')
      setSelectedFile(null)
      setPendingAttachment(null)
      setImportError(null)
      setImportStage('idle')
      setShowImport(false)
      void refresh()
      window.dispatchEvent(new Event('manju:projects-changed'))
      go('bible', res.project_id, null)
    } catch (e: unknown) {
      const message = (e as Error).message
      if (message.includes('附件凭证') && (message.includes('过期') || message.includes('不存在'))) {
        setPendingAttachment(null)
      }
      setImportStage('error')
      setImportError(message)
      toast(`导入未完成：${message}`, true)
    }
  }

  async function remove(p: Project) {
    setDeleting(true)
    try {
      await api.del(`/projects/${p.id}`)
      toast(`已删除《${p.name}》`)
      setDeleteTarget(null)
      window.requestAnimationFrame(() => {
        importTriggerRef.current?.focus()
      })
      refresh()
      window.dispatchEvent(new Event('manju:projects-changed'))
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setDeleting(false)
    }
  }

  function closeDeleteDialog() {
    setDeleteTarget(null)
    window.requestAnimationFrame(() => {
      const trigger = deleteTriggerRef.current
      if (trigger?.isConnected) trigger.focus()
      else importTriggerRef.current?.focus()
    })
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">漫剧案头 / 项目空间</div>
        <div className="page-title-row">
          <h1>项目空间 <span className="sub">每本小说都是一个独立的创作工作空间</span></h1>
          {!emptyProjectList && (
            <button
              ref={importTriggerRef}
              className="btn primary"
              type="button"
              aria-expanded={importVisible}
              aria-controls={importPanelId}
              disabled={uploading}
              aria-label={uploading
                ? '创建项目空间，暂不可用：小说正在导入，请等待完成'
                : importVisible ? '收起项目空间创建区' : '展开项目空间创建区'}
              title={uploading ? '小说正在导入，请等待完成' : undefined}
              onClick={() => {
                const next = !importVisible
                setShowImport(next)
                const target = next ? '/workspaces/new' : '/workspaces'
                window.history.pushState({}, '', target)
                window.dispatchEvent(new Event('manju:locationchange'))
              }}
            >
              {importVisible ? '收起创建' : '＋ 创建项目空间'}
            </button>
          )}
        </div>
        <hr className="rule" />
      </header>

      {observabilityIntent && (
        <div className="monitor-state ready" role="status">
          旧观测链接未携带可验证的项目。请先从左侧项目空间切换器选择小说，系统会继续打开对应观测页。
        </div>
      )}

      {importVisible && <section id={importPanelId} className="card import-panel" aria-busy={uploading || undefined}>
        <div className="section-heading">
          <div><span className="eyebrow">新项目空间</span><h3>上传小说并创建创作空间</h3></div>
          <span className="hint">支持 TXT、EPUB · TXT 自动识别 UTF-8、GB18030 和 Big5</span>
        </div>
        <div className="import-grid">
          <div>
            <label className="f" htmlFor={projectNameId}>书名（留空则取文件名）</label>
            <input
              id={projectNameId}
              type="text"
              value={name}
              maxLength={120}
              disabled={uploading}
              aria-describedby={importHelpId}
              onChange={e => setName(e.target.value)}
              placeholder="例如：凡人修仙传"
            />
          </div>
          <button
            type="button"
            className={`upload-zone ${drag ? 'drag' : ''}`}
            disabled={uploading}
            aria-busy={uploading || undefined}
            aria-label={uploading
              ? `选择小说文件，暂不可用：${importStage === 'uploading' ? '正在上传文件' : '正在创建项目'}`
              : '选择一份 TXT 或 EPUB 小说文件，或拖放到此处'}
            aria-describedby={importHelpId}
            onClick={() => fileRef.current?.click()}
            onDragOver={e => { e.preventDefault(); setDrag(true) }}
            onDragLeave={() => setDrag(false)}
            onDrop={e => {
              e.preventDefault()
              setDrag(false)
              selectFiles(e.dataTransfer.files)
            }}
          >
            <b>{importStage === 'uploading' ? '正在上传小说…' : importStage === 'creating' ? '正在创建项目…' : '选择 TXT / EPUB 文件'}</b>
            <span>{uploading ? '请保持页面开启，不要重复提交' : '或将一份文件拖到这里'}</span>
          </button>
        </div>
        <p id={importHelpId} className="import-guidance">
          选择文件只会在本页预览；确认后才上传并创建项目。现有项目不会被覆盖。
        </p>
        {(selectedFile || importError) && (
          <div
            className={`import-file-state ${importStage === 'error' ? 'error' : 'working'}`}
            role={importStage === 'error' ? 'alert' : 'status'}
            aria-live="polite"
          >
            <div className="import-file-copy">
              <b>
                {importStage === 'uploading'
                  ? '正在上传文件'
                  : importStage === 'creating'
                    ? '正在切分章节并创建项目'
                    : importStage === 'selected'
                      ? '已选择，等待确认'
                      : '导入未完成'}
              </b>
              {selectedFile && <span>{selectedFile.name} · {formatFileSize(selectedFile.size)}</span>}
              {importStage === 'selected' && selectedFile && (
                <p className="import-impact">
                  将创建《{name.trim() || novelTitleFromFilename(selectedFile.name)}》；导入后自动启动分集规划、
                  人物谱和素材准备，可能产生模型费用。
                </p>
              )}
              {importError && (
                <details>
                  <summary>查看错误详情</summary>
                  <pre>{importError}</pre>
                </details>
              )}
            </div>
            {importStage === 'selected' && selectedFile && (
              <div className="import-file-actions">
                <button className="btn primary small" type="button" onClick={() => { void submit(selectedFile) }}>
                  确认导入并启动后台准备
                </button>
                <button className="btn small" type="button" onClick={() => fileRef.current?.click()}>
                  选择其他文件
                </button>
              </div>
            )}
            {importStage === 'error' && (
              <div className="import-file-actions">
                {selectedFile && (
                  <button className="btn primary small" type="button" onClick={() => { void submit(selectedFile) }}>
                    重试导入这份文件
                  </button>
                )}
                <button className="btn small" type="button" onClick={() => fileRef.current?.click()}>
                  选择其他文件
                </button>
              </div>
            )}
          </div>
        )}
        <input
          ref={fileRef}
          type="file"
          accept=".txt,.epub,text/plain,application/epub+zip"
          disabled={uploading}
          hidden
          onChange={e => {
            selectFiles(e.target.files)
            e.target.value = ''
          }}
        />
      </section>}

      <QueryState
        loading={loading}
        error={error}
        hasData={Boolean(projects?.length)}
        objectName="项目"
        emptyText="书房尚空。请在上方导入区选择一份 TXT 或 EPUB，创建第一个项目。"
        onRetry={refresh}
      >
        {projects?.length ? (
        <section className="project-section">
          <div className="section-heading">
            <div><span className="eyebrow">我的项目</span><h2>正在制作</h2></div>
            <span className="hint">{projects.length} 个项目</span>
          </div>
          <div className="shelf">
            {projects.map(p => {
              const knownStatus = STATUS_LABEL[p.status]
              const [label, color] = knownStatus ?? ['项目状态待确认', 'grey']
              const failed = p.bible_status === 'failed' || p.plan_status === 'failed'
              const entry = projectEntry(p)
              return (
                <article key={p.id} className="volume">
                  <button
                    className="volume-open"
                    type="button"
                    title={`${entry.label}：${p.name}`}
                    aria-label={`${entry.label}《${p.name}》；${p.chapter_count} 章，${p.episode_count} 集`}
                    onClick={() => go(entry.view, p.id, null)}
                  >
                    <div className="volume-cover" aria-hidden="true"><span>漫</span></div>
                    <div className="volume-content">
                      <div className="v-title">{p.name}</div>
                      <div className="v-meta">{(p.novel_chars / 10000).toFixed(1)} 万字 · {p.chapter_count} 章 · {p.episode_count} 集</div>
                      <div className="project-progress" aria-label="项目制作阶段">
                        <span className="done">原著</span><i /><span className={p.bible_status === 'ready' ? 'done' : ''}>人物</span><i />
                        <span className={p.plan_status === 'ready' ? 'done' : ''}>分集</span><i /><span>成片</span>
                      </div>
                    </div>
                    <span className="project-enter">{entry.label} →</span>
                  </button>
                  <div className="v-foot">
                    <span
                      className={`stamp ${failed ? 'red' : color}`}
                      title={!failed && !knownStatus ? '项目状态待确认，请进入项目查看' : undefined}
                    >
                      {failed ? '需要处理' : label}
                    </span>
                    {p.bible_status === 'running' && <span className="stamp gold">人物谱生成中</span>}
                    {p.plan_status === 'running' && <span className="stamp gold">分集生成中</span>}
                    <button className="project-delete" type="button" onClick={event => {
                      deleteTriggerRef.current = event.currentTarget
                      setDeleteTarget(p)
                    }} aria-label={`永久删除项目《${p.name}》`}>删除项目</button>
                  </div>
                </article>
              )
            })}
          </div>
        </section>
        ) : null}
      </QueryState>
      {deleteTarget && (
        <ProjectDeleteDialog
          project={deleteTarget}
          deleting={deleting}
          onClose={closeDeleteDialog}
          onConfirm={() => { void remove(deleteTarget) }}
        />
      )}
    </>
  )
}

function ProjectDeleteDialog({
  project,
  deleting,
  onClose,
  onConfirm,
}: {
  project: Project
  deleting: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  const trapRef = useFocusTrap(true, onClose)
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target && !deleting) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog project-delete-dialog" role="dialog" aria-modal="true"
        aria-labelledby="project-delete-title">
        <h3 id="project-delete-title">删除《{project.name}》？</h3>
        <p className="error-banner">此操作不可恢复。</p>
        <dl>
          <div><dt>原著</dt><dd>{project.chapter_count} 章 · {(project.novel_chars / 10000).toFixed(1)} 万字</dd></div>
          <div><dt>分集</dt><dd>{project.episode_count} 集</dd></div>
          <div><dt>同时删除</dt><dd>人物谱、场景、剧本、分镜、参考图、视频与交付记录</dd></div>
        </dl>
        <div className="dialog-actions">
          <button className="btn" type="button" disabled={deleting}
            aria-label={deleting ? '保留项目，暂不可用：正在删除项目' : '保留项目，不执行删除'}
            onClick={onClose}>保留项目</button>
          <button className="btn danger" type="button" disabled={deleting}
            aria-label={deleting ? '确认永久删除，暂不可用：删除请求正在处理' : `确认永久删除项目《${project.name}》`}
            onClick={onConfirm}>
            {deleting ? '删除中…' : '确认永久删除'}
          </button>
        </div>
      </section>
    </div>
  )
}
