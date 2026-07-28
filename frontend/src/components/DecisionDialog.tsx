import { useId } from 'react'
import { useFocusTrap } from '../hooks/useFocusTrap'

export default function DecisionDialog({
  title,
  summary,
  message,
  details = [],
  confirmLabel,
  cancelLabel,
  danger = false,
  returnFocus,
  onConfirm,
  onClose,
}: {
  title: string
  summary: string
  message: string
  details?: string[]
  confirmLabel: string
  cancelLabel: string
  danger?: boolean
  returnFocus?: HTMLElement | null
  onConfirm: () => void
  onClose: () => void
}) {
  const titleId = useId()
  const trapRef = useFocusTrap(true, onClose, { returnFocus })
  return (
    <div
      className="evidence-backdrop"
      role="presentation"
      onMouseDown={event => {
        if (event.currentTarget === event.target) onClose()
      }}
    >
      <section
        ref={trapRef}
        className="impact-dialog decision-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <h3 id={titleId}>{title}</h3>
        <div className={`review-impact${danger ? ' danger' : ''}`}>
          <b>{summary}</b>
          <p>{message}</p>
          {!!details.length && (
            <ul>
              {details.map(detail => <li key={detail}>{detail}</li>)}
            </ul>
          )}
        </div>
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>{cancelLabel}</button>
          <button type="button" className={`btn ${danger ? 'danger' : 'primary'}`} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>
  )
}
