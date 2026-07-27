import type { UiIntent } from './types'
import type { View } from '../App'
import { api } from '../api'

type GoFn = (v: View, projectId?: string | null, episodeId?: string | null, chapterIdx?: number | null) => void

function triggerBlobDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noreferrer'
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/** 白名单 UI Bridge：禁止任意 URL / DOM selector。 */
export function applyUiIntent(
  intent: UiIntent,
  go: GoFn,
  extras?: {
    toast?: (msg: string, isErr?: boolean) => void
    onOpenEvidence?: (artifactId: string) => void
    onSelectShot?: (episodeId: string, shotId: string) => void
    onOpenCredentials?: (modelId?: string) => void
  },
): { ok: boolean; message?: string } {
  switch (intent.type) {
    case 'navigate': {
      const view = intent.view as View
      go(view, intent.project_id ?? undefined, intent.episode_id ?? undefined, intent.chapter_idx ?? undefined)
      return { ok: true }
    }
    case 'select_shot': {
      go('wall', undefined, intent.episode_id)
      extras?.onSelectShot?.(intent.episode_id, intent.shot_id)
      extras?.toast?.(`已定位镜头 ${intent.shot_id}`)
      return { ok: true }
    }
    case 'select_version': {
      // 版本选择必须在评审墙人工完成；意图只做导航提示，避免假闭环。
      go('wall')
      extras?.toast?.(`请在评审墙人工选择版本 ${intent.version_id}`)
      return { ok: true }
    }
    case 'open_evidence': {
      extras?.onOpenEvidence?.(intent.artifact_id)
      return { ok: true }
    }
    case 'open_delivery': {
      go('cinema', undefined, intent.episode_id)
      extras?.toast?.(`已打开成片台 · ${intent.tab}`)
      return { ok: true }
    }
    case 'open_download': {
      const apiPath = intent.artifact === 'archive'
        ? `/delivery/packages/${encodeURIComponent(intent.package_id)}/archive`
        : `/delivery/packages/${encodeURIComponent(intent.package_id)}/report`
      if (!apiPath.startsWith('/delivery/packages/')) {
        return { ok: false, message: '非法下载路径' }
      }
      const filename = intent.artifact === 'archive'
        ? `${intent.package_id}.zip`
        : `${intent.package_id}-report.html`
      void api.download(apiPath)
        .then((blob) => triggerBlobDownload(blob, filename))
        .catch((err: Error) => extras?.toast?.(err.message || String(err), true))
      return { ok: true }
    }
    case 'open_credentials': {
      go('monitor')
      extras?.onOpenCredentials?.(intent.model_id)
      extras?.toast?.('请在监制房专用表单中填写 API Key，勿在对话中发送密钥')
      return { ok: true }
    }
    case 'preview': {
      // 未接线深链：降级为明确提示，避免假按钮感。
      extras?.toast?.('请在当前页面预览对应媒体（助手暂不支持内嵌预览）')
      return { ok: true }
    }
    case 'request_directory_grant': {
      extras?.toast?.('请在人物谱页亲自选择导出目录')
      go('bible')
      return { ok: true }
    }
    default:
      return { ok: false, message: '未知 UI 意图' }
  }
}
