import type { Shot } from '../api'
import type { ImageGenTaskLike } from '../lib/bibleAssets'
import { compressSegmentIndexes } from '../lib/segmentIndexes'
import { storyboardPackTargetModelLabel } from '../lib/storyboardTargetModel'
import SegmentResourcePanel from './SegmentResourcePanel'
import SegmentIdentityReview from './SegmentIdentityReview'

/**
 * 分镜台唯一的段落展示（docs/STORYBOARD_PROMPT_IR_DESIGN.md 冻结契约）。
 * shot_size/camera_move/first_frame_desc 等经典逐镜字段在这一行没有意义
 * （见 api.ts 的 StoryboardPackSegment 注释）。身份与发声修订使用独立复核入口。
 *
 * 信息分层（用户拍板，不得自由发挥）：
 * 1. 永远可见——段号 + 时长 + 一句话梗概；右侧素材缩略图行。
 * 2. 主体——prompt_text 整块，唯一动作是整段复制；这是本页主交付物，视觉权重最高。
 * 3. 次要——shot_count/目标模型/原文段号回指/台词条数/节拍/降级角标，小字与角标，
 *    不占正文层级，用 <details> 收起可展开的长内容（台词全文、节拍摘要、素材详情）。
 */
export default function StoryboardPackSegmentView({ shot, notify, project, onSaved }: {
  onSaved: () => void
  shot: Shot
  project: ImageGenTaskLike | null | undefined
  notify: (message: string, error?: boolean) => void
}) {
  const segment = shot.storyboard_pack_segment
  if (!segment) {
    return (
      <article className="shot-strip storyboard-pack-segment">
        <p className="storyboard-pack-empty-hint">本段暂无数据</p>
      </article>
    )
  }
  const rangeText = compressSegmentIndexes(segment.source_segment_indexes ?? [])
  const beats = segment.beats ?? []

  const copyPromptText = async () => {
    if (!navigator.clipboard) {
      notify('当前浏览器无法访问剪贴板，请检查浏览器权限后重试', true)
      return
    }
    try {
      await navigator.clipboard.writeText(segment.prompt_text)
      notify('提示词已整块复制')
    } catch {
      notify('复制失败，请允许浏览器访问剪贴板后重试', true)
    }
  }

  return (
    <article className="shot-strip storyboard-pack-segment">
      <header className="storyboard-pack-segment-head">
        <div className="storyboard-pack-segment-head-copy">
          <div className="storyboard-pack-segment-head-top">
            <b>第 {segment.segment_no} 段</b>
            <span>{segment.duration_s}s</span>
          </div>
          <p className="storyboard-pack-synopsis">{segment.synopsis || '（本段无梗概）'}</p>
        </div>
      </header>

      <SegmentResourcePanel resources={segment.resources} project={project} />
      <SegmentIdentityReview shotId={shot.id} notify={notify} onSaved={onSaved} />

      <section className="storyboard-pack-prompt-block">
        <div className="storyboard-pack-prompt-head">
          <b>视频生成提示词</b>
          <button type="button" className="text-action" onClick={() => void copyPromptText()}>复制整段提示词</button>
        </div>
        {segment.prompt_text
          ? <pre className="storyboard-pack-prompt-text">{segment.prompt_text}</pre>
          : <p className="storyboard-pack-empty-hint">暂无数据</p>}
      </section>

      <section className="storyboard-pack-meta-strip" aria-label="次要信息">
        <span className="pack-meta-chip">{segment.shot_count} 镜切换</span>
        <span className="pack-meta-chip">{storyboardPackTargetModelLabel(segment.target_model)}</span>
        <span className="pack-meta-chip">对应原文{rangeText ? ` 第 ${rangeText} 段` : '暂无数据'}</span>
        {segment.dialogue.length ? (
          <details className="pack-meta-details">
            <summary className="pack-meta-chip">台词 {segment.dialogue.length} 条</summary>
            <ul className="storyboard-pack-dialogue-list">
              {segment.dialogue.map((line, index) => (
                <li key={index}>
                  <span className="storyboard-pack-dialogue-speaker">{line.speaker_identity_id || '未知说话人'}</span>
                  <span className="storyboard-pack-dialogue-line">{line.line}</span>
                  <span className="storyboard-pack-dialogue-source">原文第 {line.source_segment_index} 段</span>
                </li>
              ))}
            </ul>
          </details>
        ) : <span className="pack-meta-chip muted">无台词</span>}
        {!!beats.length && (
          <details className="pack-meta-details">
            <summary className="pack-meta-chip">节拍 {beats.length} 个</summary>
            <ul className="storyboard-pack-beat-detail-list">
              {beats.map(beat => (
                <li key={beat.beat_id}><b>{beat.beat_id}</b>{beat.summary ? `：${beat.summary}` : '（暂无摘要）'}</li>
              ))}
            </ul>
          </details>
        )}
        {segment.degraded_capabilities.map((item, index) => (
          <span key={index} className="pack-meta-chip degraded">{item}</span>
        ))}
      </section>
    </article>
  )
}

