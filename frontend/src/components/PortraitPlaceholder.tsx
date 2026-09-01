import { locationFor } from '../App'
import { portraitPlaceholderText, resolvePortraitPlaceholderKind, type RefsTaskLike } from '../lib/bibleAssets'

/**
 * 映射台/分镜台/生成台共用的定妆照占位（用户拍板，2026-08-31）：具名角色当前无
 * 图时，按 refs_status/refs_target 区分"生成中/生成失败/待生成"三态，不再一律
 * 显示"无定妆照"——那个措辞只保留给群演/一次性人物/未收录称谓
 * （identityId 没有 bible: 前缀，见 lib/bibleAssets.ts::resolvePortraitPlaceholderKind）。
 * 失败态额外给一个可点击入口跳转人物谱手动补图（CLAUDE.md「拦住用户时必须给
 * 出路」，不许把人晾在原地）。className 由调用方传入——三个页面各自的样式表
 * 已经定义了尺寸/背景一致的空占位样式，这里不引入第四份 CSS。
 *
 * 用 <a href> 而不是 useNav().go()：本组件会被 ScriptPage/BoardPage/WallPage.test.ts
 * 用 renderToStaticMarkup 直接单测（不经过 NavCtx.Provider、无 window/jsdom），
 * useNav() 的 fallback 分支无条件调用 window.location（App.tsx::readLocation），
 * 在这种环境下会直接抛错。locationFor 是纯函数、不碰 window，projectId 从已经
 * 在传的 project.id 里取，不需要调用方再多传一个 prop。
 */
export default function PortraitPlaceholder({ identityId, project, className }: {
  identityId: string | null | undefined
  project: (RefsTaskLike & { id?: string }) | null | undefined
  className: string
}) {
  const kind = resolvePortraitPlaceholderKind(identityId, project)
  const text = portraitPlaceholderText(kind)
  if (kind !== 'failed') {
    return <div className={className} aria-hidden="true">{text}</div>
  }
  return (
    <a
      href={locationFor('bible', project?.id ?? null, null, null)}
      className={className}
      style={{ border: 'none', padding: 0, font: 'inherit', cursor: 'pointer', textDecoration: 'none' }}
      title="定妆照生成失败，点击前往人物谱手动补图"
    >
      {text}
    </a>
  )
}
