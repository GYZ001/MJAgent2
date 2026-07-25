import { ReactNode, useEffect, useId, useRef } from 'react'

export default function GenerationParamsDialog({ title, subtitle, children, onClose }: {
  title: string
  subtitle?: string
  children: ReactNode
  onClose: () => void
}) {
  const titleId = useId()
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', closeOnEscape)
    closeRef.current?.focus()
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', closeOnEscape)
    }
  }, [onClose])

  return (
    <div className="generation-params-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section className="generation-params-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="generation-params-modal-head">
          <div>
            <span className="eyebrow">GENERATION SETTINGS</span>
            <h2 id={titleId}>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button ref={closeRef} type="button" aria-label="关闭生成参数" onClick={onClose}>×</button>
        </header>
        <div className="generation-params-modal-body">{children}</div>
        <footer>
          <span>修改生成词后，可选择保存或立即重新生成。</span>
          <button className="btn" type="button" onClick={onClose}>完成</button>
        </footer>
      </section>
    </div>
  )
}
