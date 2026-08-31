import { useState } from 'react'
import { api } from '../api'
import type { VisualStyleOption } from '../components/VisualStyleDialog'

/**
 * 导入项目页与（历史上）人物谱/场景库共用的统一画风弹窗状态。项目已存在时
 * 从 GET /projects/{id}/bible/visual-styles 读取，保证看到的是同一份项目级
 * 设置；`projectId` 为 null 时（导入面板：项目尚未创建，没有 id 可传）改读
 * 项目无关的 GET /bible/visual-styles——两者返回同一份
 * VISUAL_STYLE_PRESETS，只是要不要求 project_id 已存在的区别。
 *
 * 下游动作（当前只剩导入面板：选定后随创建请求一起提交）由调用方在
 * onConfirm 里处理，这个 hook 只负责弹窗本身的状态机。
 */
export function useVisualStyleDialog(projectId: string | null) {
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
      const result = projectId
        ? await api.bibleVisualStyles(projectId)
        : await api.bibleVisualStylesUnscoped()
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
