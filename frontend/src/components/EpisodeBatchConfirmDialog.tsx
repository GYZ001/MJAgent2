import { useFocusTrap } from '../hooks/useFocusTrap'

/** 从 EpisodesPage.tsx 拆出（EP-01 第二阶段腾行数用）：纯 props 驱动的批量
 *  操作二次确认弹窗，不碰页面自身状态，天然适合独立成文件。 */
export type BatchAction = 'replan' | 'screenplay' | 'storyboard'

export default function EpisodeBatchConfirmDialog({
  action,
  projectName,
  totalEpisodes,
  screenplayTodoCount,
  storyboardTodoCount,
  busy,
  onClose,
  onConfirm,
}: {
  action: BatchAction
  projectName: string
  totalEpisodes: number
  screenplayTodoCount: number
  storyboardTodoCount: number
  busy: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  const trapRef = useFocusTrap(true, onClose)
  const content = action === 'replan'
    ? {
      title: totalEpisodes ? '重新规划全部分集？' : '开始规划分集？',
      count: `${totalEpisodes || '全部'} 集`,
      impact: totalEpisodes
        ? '将清空当前全部分集及其映射包、分镜、视频和交付记录，再按原著重新建立分集。'
        : '将依据原著章节创建分集，不会启动映射包、分镜或视频生成。',
      // 分集规划是纯规则拆章（按章节标题切分），不调用任何模型；重新规划真正
      // 的代价是清空旧数据——若其中含已生成视频，那部分视频额度不会退回
      // （不产出新视频不退款，见 app/quota.py 的额度记账口径）。
      cost: totalEpisodes
        ? '不调用任何模型，不产生费用；但会清空已生成的视频，其消耗的视频额度不会退回。'
        : '按章节规则直接生成分集，不调用任何模型，不产生费用。',
      confirm: totalEpisodes ? '确认清空并重新分集' : '确认开始分集',
      danger: totalEpisodes > 0,
    }
    : action === 'screenplay'
      ? {
        title: '批量生成待办映射包？',
        count: `${screenplayTodoCount} 集`,
        impact: '只处理待生成、失败或需要修订的映射包；已完成且无需重建的映射包不会重复生成。',
        cost: '每集会调用文本模型；文本调用不产生费用，也不消耗视频额度。失败不会覆盖已完成映射包。',
        confirm: '确认生成待办映射包',
        danger: false,
      }
      : {
        title: '批量生成待办分镜？',
        count: `${storyboardTodoCount} 集`,
        impact: '只处理映射包已就绪或可从恢复点继续的分集；不会自动确认分镜，也不会启动付费视频。',
        cost: '逐集调用文本模型；文本调用不产生费用，也不消耗视频额度。',
        confirm: '确认生成待办分镜',
        danger: false,
      }
  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target && !busy) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog episode-batch-dialog" role="dialog" aria-modal="true"
        aria-labelledby="episode-batch-title">
        <h3 id="episode-batch-title">{content.title}</h3>
        <dl>
          <div><dt>项目</dt><dd>{projectName}</dd></div>
          <div><dt>本次范围</dt><dd>{content.count}</dd></div>
          <div><dt>执行影响</dt><dd>{content.impact}</dd></div>
          <div><dt>费用与额度</dt><dd>{content.cost}</dd></div>
        </dl>
        <div className="dialog-actions">
          <button className="btn" type="button" disabled={busy}
            aria-label={busy ? '取消批量操作，暂不可用：正在提交任务' : '取消，不执行批量操作'}
            onClick={onClose}>取消（不执行）</button>
          <button className={`btn ${content.danger ? 'danger' : 'primary'}`} type="button" disabled={busy}
            aria-label={busy ? `${content.confirm}，暂不可用：正在提交任务` : content.confirm}
            onClick={onConfirm}>
            {busy ? '提交中…' : content.confirm}
          </button>
        </div>
      </section>
    </div>
  )
}
