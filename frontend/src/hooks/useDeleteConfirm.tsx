import { useCallback, useRef, useState } from 'react'
import { ApprovalRequiredError } from '../api/client'
import type { ApprovalPreflight } from '../api/client'
import DeleteConfirmDialog from '../components/DeleteConfirmDialog'

/**
 * 「删除资源」类命令的通用确认流程：与 frontend/src/api/client.ts 配对——
 * 任何调用可能抛出 ApprovalRequiredError 的删除动作（当前有 project.purge /
 * project.purge_all / screenplay.delete，判据挂在后端 catalog 的
 * confirmation=ALWAYS 上，未来任何新登记的删除命令都自动适用，不需要每个
 * 页面各写一遍弹窗逻辑），先执行动作，命中该错误时挂起并展示 `dialog`
 * （渲染 components/DeleteConfirmDialog.tsx，调用方只需把它放进自己的 JSX，
 * 不需要各自 import 该组件）。用户确认后带 approval_token 重放同一次请求，
 * `run` resolve 成真实结果，失败仍照常 reject；用户取消则 `run` resolve 成
 * `undefined`（不是 reject）——调用方按"没有产出"处理即可，不需要各自
 * import 一个取消错误类型再逐个 catch。真实 API 结果不会是 undefined
 * （删除类端点成功都回一个非空对象），拿它当取消哨兵是安全的。
 */
export function useDeleteConfirm() {
  const [pending, setPending] = useState<ApprovalPreflight | null>(null)
  const [busy, setBusy] = useState(false)
  const waiterRef = useRef<{
    retry: () => Promise<unknown>
    resolve: (value: unknown) => void
    reject: (error: unknown) => void
  } | null>(null)

  const run = useCallback(<T,>(action: () => Promise<T>): Promise<T | undefined> => {
    return action().catch((err: unknown) => {
      if (!(err instanceof ApprovalRequiredError)) throw err
      return new Promise<T | undefined>((resolve, reject) => {
        waiterRef.current = { retry: err.retry, resolve: resolve as (value: unknown) => void, reject }
        setPending(err.preflight)
      })
    })
  }, [])

  const confirm = useCallback(async () => {
    const waiter = waiterRef.current
    if (!waiter) return
    setBusy(true)
    try {
      waiter.resolve(await waiter.retry())
    } catch (e) {
      waiter.reject(e)
    } finally {
      setBusy(false)
      setPending(null)
      waiterRef.current = null
    }
  }, [])

  const cancel = useCallback(() => {
    waiterRef.current?.resolve(undefined)
    waiterRef.current = null
    setPending(null)
  }, [])

  const dialog = <DeleteConfirmDialog pending={pending} busy={busy} onCancel={cancel} onConfirm={() => { void confirm() }} />
  return { pending, busy, run, dialog }
}
