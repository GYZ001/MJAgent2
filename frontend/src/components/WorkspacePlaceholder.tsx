import { useId } from 'react'
import { useNav } from '../App'
import type { View } from '../App'
import EpisodeCrumb from './EpisodeCrumb'

interface WorkspacePlaceholderProps {
  label: string
  view: View
  /** 分集 id 仍在异步解析中（进项目/切分区触发的二次校验请求在途）。为真时正文
   *  必须是诚实的加载态，不能说“尚未进入具体分集”——那与同一屏 EpisodeCrumb
   *  此刻显示的“正在加载分集…”自相矛盾，也不能给终态引导按钮（解析还没结束，
   *  按钮此刻毫无意义）。 */
  resolving?: boolean
}

/** script/board/wall/cinema 四个 needEpisode 分区在 episodeId 为空时渲染的占位页。 */
export default function WorkspacePlaceholder({ label, view, resolving }: WorkspacePlaceholderProps) {
  const { projectId, go } = useNav()
  const titleId = useId()

  if (resolving) {
    return (
      <>
        <header className="desk-head">
          <EpisodeCrumb label={label} view={view} />
          <h1>
            {label} <span className="sub">正在打开，请稍候</span>
          </h1>
          <hr className="rule" />
        </header>
        <section className="empty workspace-empty" aria-labelledby={titleId} role="status">
          <div className="big" aria-hidden="true">集</div>
          <h2 id={titleId}>正在确认要进入的分集</h2>
          <p>分集信息回来后会自动进入本集；若本项目还没有分集，稍后会给出创建入口。</p>
        </section>
      </>
    )
  }

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label={label} view={view} />
        <h1>
          {label} <span className="sub">请选择或创建分集后进入</span>
        </h1>
        <hr className="rule" />
      </header>
      <section className="empty workspace-empty" aria-labelledby={titleId}>
        <div className="big" aria-hidden="true">集</div>
        <h2 id={titleId}>尚未进入具体分集</h2>
        <p>前往分集规划检查并选择已有分集；若项目尚无分集，可在那里创建。</p>
        <div className="workspace-empty-actions">
          {projectId && (
            <button
              type="button"
              className="btn primary"
              onClick={() => go('episodes', projectId, null)}
            >
              查看分集并选择
            </button>
          )}
          <button
            type="button"
            className="btn"
            onClick={() => go('studio', null, null)}
          >
            返回项目空间
          </button>
        </div>
      </section>
    </>
  )
}
