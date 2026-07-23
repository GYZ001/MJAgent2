import { ButtonHTMLAttributes, useRef, useState } from 'react'

type AsyncFn = () => Promise<unknown>

/** 带提交锁的异步按钮：进行中禁用，避免重复点击；失败可抛给调用方或本地吞掉。 */
export default function AsyncButton({
  onAction,
  busyLabel,
  children,
  disabled,
  className,
  onError,
  ...rest
}: {
  onAction: AsyncFn
  busyLabel?: string
  onError?: (error: Error) => void
} & Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onClick'>) {
  const [busy, setBusy] = useState(false)
  const locked = useRef(false)

  const click = async () => {
    if (locked.current || disabled) return
    locked.current = true
    setBusy(true)
    try {
      await onAction()
    } catch (e: unknown) {
      onError?.(e instanceof Error ? e : new Error(String(e)))
    } finally {
      locked.current = false
      setBusy(false)
    }
  }

  return (
    <button
      type="button"
      className={className}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      onClick={() => { void click() }}
      {...rest}
    >
      {busy ? (busyLabel ?? '处理中…') : children}
    </button>
  )
}
