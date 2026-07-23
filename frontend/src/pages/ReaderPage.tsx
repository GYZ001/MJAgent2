import { useEffect, useState } from 'react'
import { api, ChapterContent } from '../api'
import { useNav } from '../App'

export default function ReaderPage() {
  const { projectId, chapterIdx, go, toast } = useNav()
  const [data, setData] = useState<ChapterContent | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const idx = chapterIdx ?? 1

  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    api.get(`/projects/${projectId}/chapters/${idx}`)
      .then((d: ChapterContent) => {
        if (cancelled) return
        setData(d)
        window.scrollTo({ top: 0, behavior: 'auto' })
      })
      .catch((e: Error) => {
        if (cancelled) return
        setError(e.message)
        toast(e.message, true)
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [projectId, idx, toast])

  const goChapter = (target: number | null | undefined) => {
    if (target == null || !projectId) return
    go('reader', projectId, null, target)
  }

  const paragraphs = (data?.content ?? '').split(/\n+/).map(s => s.trim()).filter(Boolean)
  const hasPrev = data?.prev_idx != null
  const hasNext = data?.next_idx != null

  const Nav = ({ top = false }: { top?: boolean }) => (
    <div style={{ display: 'flex', gap: 12, alignItems: 'center', justifyContent: 'center',
                  margin: top ? '0 0 18px' : '28px 0 8px' }}>
      <button className="btn" disabled={!hasPrev || loading} onClick={() => goChapter(data?.prev_idx)}>← 上一章</button>
      <span style={{ fontSize: 13, color: 'var(--ink-faint)' }}>
        {data ? `第 ${data.idx} / ${data.last_idx} 章` : ''}
      </span>
      <button className="btn" disabled={!hasNext || loading} onClick={() => goChapter(data?.next_idx)}>下一章 →</button>
    </div>
  )

  return (
    <>
      <header className="desk-head">
        <div className="crumb crumb-switch">
          <button className="crumb-btn" type="button" onClick={() => go('episodes', projectId)}>分集</button>
          <span className="crumb-sep">/</span>
          <span>看正文</span>
        </div>
        <h1>{data?.title || '看正文'} <span className="sub">沉浸式阅读 · 上一章 / 下一章翻页</span></h1>
        <hr className="rule" />
      </header>

      <section className="card reader-card">
        <Nav top />
        <div className="reader-scroll">
          {error && !data ? (
            <div className="empty">{error}</div>
          ) : loading && !data ? (
            <div className="empty">展卷中……</div>
          ) : (
            <article className="reader-article">
              {paragraphs.length ? paragraphs.map((p, i) => (
                <p key={i}>{p}</p>
              )) : <div className="empty">本章暂无正文</div>}
            </article>
          )}
        </div>
        <Nav />
      </section>
    </>
  )
}
