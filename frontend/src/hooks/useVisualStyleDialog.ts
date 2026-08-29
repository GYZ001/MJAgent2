import { useState } from 'react'
import { api } from '../api'
import type { VisualStyleOption } from '../components/VisualStyleDialog'

/**
 * 人物谱与场景库共用的统一画风弹窗状态。两个页面都从同一个后端来源
 * （GET /projects/{id}/bible/visual-styles + 当前项目的 bible_style_name）
 * 读取当前画风，保证无论从哪个页面打开，看到的都是同一份项目级设置，而不是
 * 各自维护的一份本地默认值。
 *
 * 下游动作（人物谱页触发定妆照 / 场景库页触发场景图）由调用方在 onConfirm
 * 里各自处理，这个 hook 只负责弹窗本身的状态机。
 */
export function useVisualStyleDialog(projectId: string) {
  const [styleOpen, setStyleOpen] = useState(false)
  const [styleLoading, setStyleLoading] = useState(false)
  const [styleError, setStyleError] = useState<string | null>(null)
  const [styleOptions, setStyleOptions] = useState<VisualStyleOption[]>([])
  const [selectedStyle, setSelectedStyle] = useState('')

  const openStyleDialog = async (currentStyleName?: string | null) => {
    setStyleOpen(true)
    setStyleLoading(true)
    setStyleError(null)
    try {
      const result = await api.bibleVisualStyles(projectId)
      const names = result.items.map(item => item.name).filter(Boolean)
      setStyleOptions(result.items)
      setSelectedStyle(
        currentStyleName && names.includes(currentStyleName)
          ? currentStyleName
          : result.default || names[0] || '',
      )
    } catch (e: unknown) {
      setStyleError((e as Error).message)
      setStyleOptions([])
      setSelectedStyle('')
    } finally {
      setStyleLoading(false)
    }
  }

  const closeStyleDialog = () => {
    setStyleOpen(false)
    setStyleError(null)
  }

  return {
    styleOpen,
    styleLoading,
    styleError,
    styleOptions,
    selectedStyle,
    setSelectedStyle,
    setStyleError,
    openStyleDialog,
    closeStyleDialog,
  }
}
