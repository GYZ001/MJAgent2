import { useEffect, useRef, useState } from 'react'
import { api, ChapterContent } from '../api'
import { useNav } from '../App'

export default function ReaderPage() {
  const { projectId, chapterIdx, go, toast } = useNav()
  const [idx, setIdx] = useState<number>(chapterIdx ?? 1)
  const [data, setData] = useState<ChapterContent | null>(null)
  const [loading, setLoading] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    setLoading(true)
    api.get(`/projects/${projectId}/chapters/${idx}`)
      .then((d: ChapterContent) => { if (!cancelled) { setData(d); scrollRef.current?.scrollTo({ top: 0 }) } })
      .catch((e: Error) => { if (!cancelled) toast(e.message, true) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [projectId, idx, toast])

  const goChapter = (target: number | null | undefined) => {
    if (target != null) setIdx(target)
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

      <section className="card">
        <Nav top />
        <div ref={scrollRef} style={{ maxHeight: '68vh', overflowY: 'auto', padding: '4px 4px 8px' }}>
          {loading && !data ? (
            <div className="empty">展卷中……</div>
          ) : (
            <article style={{
              maxWidth: 760, margin: '0 auto', fontSize: 17, lineHeight: 2.05,
              color: 'var(--ink, #2b2b2b)', letterSpacing: '0.02em',
              fontFamily: '"Songti SC", "STSong", "Noto Serif SC", serif',
            }}>
              {paragraphs.length ? paragraphs.map((p, i) => (
                <p key={i} style={{ textIndent: '2em', margin: '0 0 1.1em' }}>{p}</p>
              )) : <div className="empty">本章暂无正文</div>}
            </article>
          )}
        </div>
        <Nav />
      </section>
    </>
  )
}
