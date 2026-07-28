import { FormEvent, useEffect, useState } from 'react'
import { api, ChapterContent } from '../api'
import { useNav } from '../App'

export default function ReaderPage() {
  const { projectId, chapterIdx, go, toast } = useNav()
  const [data, setData] = useState<ChapterContent | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const [jumpValue, setJumpValue] = useState('')
  const idx = chapterIdx ?? 1

  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setData(null)
    setJumpValue(String(idx))
    api.get(`/projects/${projectId}/chapters/${idx}`)
      .then((d: ChapterContent) => {
        if (cancelled) return
        setData(d)
        setJumpValue(String(d.idx))
        window.scrollTo({ top: 0, behavior: 'auto' })
      })
      .catch((e: Error) => {
        if (cancelled) return
        const message = /404|not found/i.test(e.message)
          ? `未找到第 ${idx} 章，请检查章节编号`
          : `正文加载失败：${e.message}`
        setError(message)
        toast(message, true)
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [projectId, idx, reloadToken, toast])

  const goChapter = (target: number | null | undefined) => {
    if (target == null || !projectId) return
    go('reader', projectId, null, target)
  }

  const submitJump = (event: FormEvent) => {
    event.preventDefault()
    const target = Number(jumpValue)
    const last = data?.last_idx
    if (!Number.isInteger(target) || target < 1 || (last != null && target > last)) {
      toast(last ? `请输入 1–${last} 之间的章节编号` : '请输入有效的章节编号', true)
      setJumpValue(String(data?.idx ?? idx))
      return
    }
    if (target === data?.idx) {
      toast(`当前已是第 ${target} 章`)
      return
    }
    goChapter(target)
  }

  const paragraphs = (data?.content ?? '').split(/\n+/).map(s => s.trim()).filter(Boolean)
  const hasPrev = data?.prev_idx != null
  const hasNext = data?.next_idx != null
  const prevDisabledReason = loading
    ? `正在加载第 ${idx} 章`
    : !hasPrev
      ? '当前已是第一章'
      : ''
  const nextDisabledReason = loading
    ? `正在加载第 ${idx} 章`
    : !hasNext
      ? '当前已是最后一章'
      : ''

  const Nav = ({ top = false }: { top?: boolean }) => (
    <nav
      className={`reader-nav ${top ? 'reader-nav-top' : 'reader-nav-bottom'}`}
      aria-label={top ? '章节导航（顶部）' : '章节导航（底部）'}
    >
      <button
        type="button"
        className="btn"
        disabled={Boolean(prevDisabledReason)}
        aria-label={prevDisabledReason ? `上一章，暂不可用：${prevDisabledReason}` : `上一章：第 ${data?.prev_idx} 章`}
        title={prevDisabledReason || undefined}
        onClick={() => goChapter(data?.prev_idx)}
      >
        ← 上一章
      </button>
      <span className="reader-position" aria-live="polite">
        {loading
          ? `正在加载第 ${idx} 章…`
          : data
            ? `第 ${data.idx} / ${data.last_idx} 章`
            : `第 ${idx} 章`}
      </span>
      <button
        type="button"
        className="btn"
        disabled={Boolean(nextDisabledReason)}
        aria-label={nextDisabledReason ? `下一章，暂不可用：${nextDisabledReason}` : `下一章：第 ${data?.next_idx} 章`}
        title={nextDisabledReason || undefined}
        onClick={() => goChapter(data?.next_idx)}
      >
        下一章 →
      </button>
      {data && (
        <form className="reader-jump" noValidate onSubmit={submitJump}>
          <span>跳至</span>
          <input
            type="number"
            min={1}
            max={data.last_idx}
            inputMode="numeric"
            value={jumpValue}
            aria-label={`${top ? '顶部' : '底部'}章节编号，范围 1 到 ${data.last_idx}`}
            onChange={event => setJumpValue(event.target.value)}
          />
          <button
            type="submit"
            className="btn small"
            disabled={loading}
            aria-label={loading ? '跳转章节，暂不可用：正在加载当前章节' : '跳转到输入的章节'}
          >
            跳转
          </button>
        </form>
      )}
    </nav>
  )

  return (
    <>
      <header className="desk-head">
        <div className="crumb crumb-switch">
          <button
            className="crumb-btn"
            type="button"
            aria-label="返回分集规划"
            onClick={() => go('episodes', projectId)}
          >
            分集规划
          </button>
          <span className="crumb-sep">/</span>
          <span>原著阅读</span>
        </div>
        <h1>
          {data?.title || `第 ${idx} 章`}
          <span className="sub">
            {data ? `原著阅读 · 共 ${data.last_idx} 章` : '原著阅读'}
          </span>
        </h1>
        <hr className="rule" />
      </header>

      <section className="card reader-card" aria-busy={loading}>
        <Nav top />
        <div className="reader-scroll">
          {error && !data ? (
            <div className="reader-state reader-error" role="alert">
              <strong>这一章暂时打不开</strong>
              <p>当前章节编号与阅读位置已保留，可重新加载或返回分集规划。</p>
              <details>
                <summary>查看错误详情</summary>
                <pre>{error}</pre>
              </details>
              <div>
                <button type="button" className="btn primary" onClick={() => setReloadToken(token => token + 1)}>
                  重新加载第 {idx} 章
                </button>
                <button type="button" className="btn" onClick={() => go('episodes', projectId)}>
                  返回分集规划
                </button>
              </div>
            </div>
          ) : loading && !data ? (
            <div className="empty reader-loading" role="status">
              正在加载第 {idx} 章正文…
            </div>
          ) : (
            <article className="reader-article" aria-label={data ? `${data.title}正文` : '章节正文'}>
              {paragraphs.length ? paragraphs.map((p, i) => (
                <p key={i}>{p}</p>
              )) : <div className="empty">本章暂无正文</div>}
            </article>
          )}
        </div>
        {data && <Nav />}
      </section>
    </>
  )
}
