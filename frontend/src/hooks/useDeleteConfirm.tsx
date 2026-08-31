import { useCallback, useRef, useState } from 'react'
import { ApprovalRequiredError } from '../api/client'
import type { ApprovalPreflight } from '../api/client'
import DeleteConfirmDialog from '../components/DeleteConfirmDialog'

/** run()/runBackground() 共用的最终结果形状：真正的成功/失败都在这里，不吞异常。 */
export type BackgroundOutcome<T> = { ok: true; value: T } | { ok: false; error: unknown }

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

  /**
   * 后台变体：批准（若需要）后不等真正的删除请求跑完就放行调用方——批准后的
   * 那次重放是后端一次同步执行到底的 HTTP 请求（没有队列/后台任务），一个
   * 大项目的级联删除能让它跑到一分钟以上，`run()` 的语义会把调用方一路 await
   * 到那时候，UI 只能干等。这里改成 `retry` 一发出去就让 confirm() 立即
   * resolve，真正的成功/失败通过 `onSettled` 异步交付，调用方自己决定何时
   * 用 toast 呈现——不能吞掉失败（用户 2026-08-31 原话：级联删除可能部分
   * 失败，必须让用户看见)。与 run() 共用同一个 waiterRef/对话框，只是把
   * “确认后做什么”换成 fire-and-forget。
   */
  const runBackground = useCallback(<T,>(
    action: () => Promise<T>,
    onSettled: (outcome: BackgroundOutcome<T>) => void,
  ): Promise<'submitted' | undefined> => {
    return action().then(
      (value): 'submitted' | undefined => {
        onSettled({ ok: true, value })
        return 'submitted'
      },
      (err: unknown): Promise<'submitted' | undefined> => {
        if (!(err instanceof ApprovalRequiredError)) {
          onSettled({ ok: false, error: err })
          return Promise.resolve(undefined)
        }
        return new Promise<'submitted' | undefined>(resolve => {
          waiterRef.current = {
            retry: () => {
              (err.retry() as Promise<T>).then(
                value => onSettled({ ok: true, value }),
                error => onSettled({ ok: false, error }),
              )
              return Promise.resolve('submitted')
            },
            resolve: resolve as (value: unknown) => void,
            reject: resolve as (value: unknown) => void,
          }
          setPending(err.preflight)
        })
      },
    )
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
  return { pending, busy, run, runBackground, dialog }
}
