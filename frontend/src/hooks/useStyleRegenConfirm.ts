import { useState } from 'react'
import { api, ApiError } from '../api'
import type { StyleRegenQuote } from '../components/StyleRegenConfirmDialog'

export type StyleRegenOutcome =
  | { kind: 'unchanged' }
  | {
    kind: 'started'
    refsStarted: boolean
    refsError: string | null
    sceneRefsStarted: boolean
    sceneRefsError: string | null
    sceneBibleReady: boolean
  }
  | { kind: 'idempotent_replay' }

/**
 * 人物谱页与场景库页共用：风格切换后「人物定妆照 + 场景图」合并付费确认。
 *
 * 两阶段走同一个后端路由（POST /projects/{id}/bible/style）：第一次不带
 * confirm，画风未变化时直接拿到结果（changed=false，幂等短路，无需弹窗）；
 * 画风有变化时后端返回 409 + 合并报价，这里捕获后打开确认弹窗；用户确认后
 * 带着 quote_id 再调用一次，后端在**同一次请求内**发起人物与场景两条生成
 * 线——不是这个 hook 自己在前端排队调用两个不同端点，那样任何一步失败或
 * 页面被关掉都会让"两条线都要发起"变成"取决于用户接下来干了什么"。
 */
export function useStyleRegenConfirm(projectId: string) {
  const [dialogOpen, setDialogOpen] = useState(false)
  const [quoteLoading, setQuoteLoading] = useState(false)
  const [quoteError, setQuoteError] = useState<string | null>(null)
  const [quote, setQuote] = useState<StyleRegenQuote | null>(null)
  const [pendingStyleName, setPendingStyleName] = useState('')
  const [pendingExpectedVersion, setPendingExpectedVersion] = useState(0)

  /** 第一步：提交风格选择。画风未变化时直接返回 unchanged，不开弹窗、不产生任何费用。 */
  const requestStyleChange = async (
    styleName: string,
    expectedVersion: number,
  ): Promise<StyleRegenOutcome | null> => {
    setPendingStyleName(styleName)
    setPendingExpectedVersion(expectedVersion)
    try {
      const result = await api.setBibleStyle(projectId, {
        style_name: styleName, expected_version: expectedVersion,
      })
      if (!result.changed) return { kind: 'unchanged' }
      // 后端在「未变化」之外的任何 changed=true 无确认响应都不该出现——防御性兜底。
      return null
    } catch (e: unknown) {
      if (e instanceof ApiError && e.code === 'PAYMENT_CONFIRM_REQUIRED') {
        const precheck = (e.detail as { precheck?: StyleRegenQuote } | undefined)?.precheck
        if (precheck) {
          setQuote(precheck)
          setQuoteError(null)
          setQuoteLoading(false)
          setDialogOpen(true)
          return null
        }
      }
      setQuoteError(e instanceof Error ? e.message : String(e))
      setQuote(null)
      setDialogOpen(true)
      return null
    }
  }

  /** 第二步：用户在合并确认弹窗里点击「确认并开始」。 */
  const confirmStyleChange = async (): Promise<StyleRegenOutcome> => {
    if (!quote) throw new Error('报价缺失，请重新发起')
    const result = await api.setBibleStyle(projectId, {
      style_name: pendingStyleName,
      expected_version: pendingExpectedVersion,
      confirm: true,
      quote_id: quote.quote_id,
    })
    setDialogOpen(false)
    if (result.idempotent_replay) return { kind: 'idempotent_replay' }
    return {
      kind: 'started',
      refsStarted: !!result.refs_started,
      refsError: result.refs_error ?? null,
      sceneRefsStarted: !!result.scene_refs_started,
      sceneRefsError: result.scene_refs_error ?? null,
      sceneBibleReady: !!result.scene_bible_ready,
    }
  }

  const closeDialog = () => {
    setDialogOpen(false)
    setQuote(null)
    setQuoteError(null)
  }

  return {
    dialogOpen,
    quoteLoading,
    quoteError,
    quote,
    pendingStyleName,
    requestStyleChange,
    confirmStyleChange,
    closeDialog,
  }
}
