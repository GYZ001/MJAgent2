import type { ReactNode } from 'react'

export default function OperationError({
  title,
  message,
  guidance,
  variant = 'error',
  detailLabel = '查看错误详情',
  children,
}: {
  title: string
  message?: string | null
  guidance: string
  variant?: 'error' | 'warning'
  detailLabel?: string
  children?: ReactNode
}) {
  return (
    <div
      className={`${variant === 'error' ? 'error-banner' : 'warning-banner'} operation-error`}
      role={variant === 'error' ? 'alert' : 'status'}
    >
      <b className="operation-error-title">{title}</b>
      <p>{guidance}</p>
      {message && (
        <details>
          <summary>{detailLabel}</summary>
          <pre>{message}</pre>
        </details>
      )}
      {children && <div className="operation-error-actions">{children}</div>}
    </div>
  )
}
