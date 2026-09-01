import { locationFor } from '../App'
import { resolveSceneRefPlaceholderKind, sceneRefPlaceholderText, type SceneRefsTaskLike } from '../lib/bibleAssets'

/**
 * 映射台/分镜台/生成台共用的场景图占位（用户拍板，2026-08-31，跟 PortraitPlaceholder
 * 同一次改造）：具卡场景当前无图时，按 scene_refs_status/scene_refs_target 区分
 * "生成中/生成失败/待生成"三态，不再一律显示"无图"——场景侧没有群演等价的
 * "永远无图"第四态（见 lib/bibleAssets.ts::resolveSceneRefPlaceholderKind 注释）。
 * 失败态额外给一个可点击入口跳转场景库手动补图（CLAUDE.md「拦住用户时必须给
 * 出路」，不许把人晾在原地）。className 由调用方传入，不引入第四份 CSS。
 *
 * 用 <a href> 而不是 useNav().go()：本组件会被 ScriptPage/BoardPage/WallPage.test.ts
 * 用 renderToStaticMarkup 直接单测（不经过 NavCtx.Provider、无 window/jsdom），
 * useNav() 的 fallback 分支无条件调用 window.location（App.tsx::readLocation），
 * 在这种环境下会直接抛错——同 PortraitPlaceholder.tsx 的修法。
 */
export default function SceneReferencePlaceholder({ sceneId, label, project, className }: {
  sceneId: string | null | undefined
  label: string
  project: (SceneRefsTaskLike & { id?: string }) | null | undefined
  className: string
}) {
  const kind = resolveSceneRefPlaceholderKind(sceneId, label, project)
  const text = sceneRefPlaceholderText(kind)
  if (kind !== 'failed') {
    return <div className={className} aria-hidden="true">{text}</div>
  }
  return (
    <a
      href={locationFor('scenes', project?.id ?? null, null, null)}
      className={className}
      style={{ border: 'none', padding: 0, font: 'inherit', cursor: 'pointer', textDecoration: 'none' }}
      title="场景图生成失败，点击前往场景库手动补图"
    >
      {text}
    </a>
  )
}
