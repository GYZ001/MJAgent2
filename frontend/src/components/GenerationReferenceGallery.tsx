import { useState } from 'react'
import { needsCharacterImage } from '../lib/segmentIdentity'
import type { ReferenceAudioInput, ReferenceAudioSkip, ReferenceImage, StoryboardPackResources } from '../api'
import { referenceImageLabel } from '../lib/bibleAssets'
import ImageCompareModal from './ImageCompareModal'
import VoicePlayButton from './voice/VoicePlayButton'

/**
 * 生成台专用（WallPage.tsx，用户拍板 2026-08-31，「传入素材」展示重做）：这一次
 * 生成尝试实际发给供应商的参考图（GET /shots/{id}/review 的
 * image_inputs.reference_images，按选中版本摊平）。与
 * components/SegmentResourcePanel.tsx 展示的「本段脚本声明涉及哪些实体」是两件
 * 不同的事——那个跨分镜台/生成台展示当前解析结果，不受具体某次生成尝试影响；
 * 这个只在生成台、只反映选中这次尝试真正用了什么，两者标题刻意写成不同措辞，
 * 让职责一眼可辨，不合并成一处假装是同一件事。
 *
 * 「该有却没有」的显眼提示：已经提交过生成（hasAttempt）、详情加载成功
 * （不是 loading/失败）、但这次实际参考图一张都没有，而本段又声明了人物/场景
 * 资源时，说明这次生成很可能没有正确挂上参考图——这正是人物/场景在镜头间漂移
 * 的来源，用红色告警条显眼提示，不静默展示一个空列表。
 *
 * `loading` 同时承担「还不知道」这一档，由调用方（WallPage.tsx）在参考图快照没
 * 覆盖到当前选中版本时置真：分集轮询 4 秒一次、单镜详情只在切段/刷新时取一次，
 * 轮询必然先于详情看到新提交的尝试，那一段时间里「快照里查不到这条版本」不等于
 * 「这条版本没带参考图」。2026-09-01 实测：整轮生成都误报「参考图缺失」，而
 * image_inputs 里三张参考图一张不少（lib/wallReferences.ts 有完整来龙去脉）。
 *
 * 逐个角色/场景的精确比对（哪个具体实体缺失）没有做：resources 里的 identity_id/
 * scene_id 与 ReferenceImage.entity_name 之间的对应关系没有一份权威判据可以直接
 * 复用（entity_name 是否恒为去前缀后的裸名字未经后端确认），强行按字符串猜测匹配
 * 属于自造一份可能出错的第二判据（CLAUDE.md 禁止黑白名单式猜测匹配）。这里只做
 * 「一张参考图都没有」这种不需要任何猜测、100% 从数据能推出的粗粒度告警。
 *
 * CSS 放 styles/WallPage.css：本组件只有生成台一个消费方，已登记进
 * scripts/check_css_split.py 的 PAGES['WallPage']，可以直接复用 WallPage 既有的
 * .wall-attempt-issue / .wall-empty-hint 告警与提示样式，不再造第二套。
 *
 * 参考音频（U4b，2026-09-24）：另一个代理正在做「生成视频时把本段说话角色的
 * 参考音频一起传给视频模型」（系统设置 video_reference_audio_enabled 控制，
 * 默认关闭），这里只做展示，不等后端——image_inputs.reference_audios/
 * reference_audio_skips 字段名逐字照用后端约定（见 api/storyboard/versions.ts
 * 的 ReferenceAudioInput/ReferenceAudioSkip）。老版本、开关关闭、或字段本身
 * 缺失（旧数据）时 audios/audioSkips 都按空数组处理，两者都空时不渲染任何
 * 东西（不冒出空标题）。versionId 用于给 VoicePlayButton 拼出跨卡片唯一的
 * id——同一角色的声音可能同时出现在多张分镜卡片上，仅 voice_id 区分不出
 * 「这是哪张卡片这次生成实际用的那条」；调用方（WallPage.tsx）目前还没有把
 * 选中版本 id 接进来，留空时退化为只用 voice_id，是已知限制而非缺陷。
 */
export default function GenerationReferenceGallery({
  refs, loading, hasAttempt, declaredResources, audios = [], audioSkips = [], versionId = '',
}: {
  refs: ReferenceImage[]
  loading: boolean
  hasAttempt: boolean
  declaredResources?: StoryboardPackResources
  audios?: ReferenceAudioInput[]
  audioSkips?: ReferenceAudioSkip[]
  versionId?: string
}) {
  const [preview, setPreview] = useState<{ title: string; images: { src: string; label: string }[] } | null>(null)
  const hasDeclaredResources = Boolean(declaredResources?.characters.some(needsCharacterImage) || declaredResources?.scenes.length)
  const showMissingWarning = hasAttempt && !loading && !refs.length && hasDeclaredResources

  return (
    <section className="genref-gallery" aria-label="本次生成实际参考图">
      <div className="genref-head">
        <b>本次生成实际参考图</b>
        <span>这次提交给供应商的真实素材，不是本段应涉及的完整清单</span>
      </div>
      {loading && <p className="wall-empty-hint">正在加载参考图…</p>}
      {showMissingWarning && (
        <p className="wall-attempt-issue" role="alert">
          <b>参考图缺失</b>
          <span>本次生成没有携带任何参考图，人物/场景可能与预期不一致，建议核对后重新生成</span>
        </p>
      )}
      {!loading && !refs.length && !showMissingWarning && hasAttempt && (
        <p className="wall-empty-hint">本次生成未使用参考图</p>
      )}
      {!!refs.length && (
        <div className="genref-list">
          {refs.map(ref => {
            const label = referenceImageLabel(ref)
            const imageUrl = ref.image_url
            return (
              <figure className="genref-card" key={ref.id}>
                {imageUrl
                  ? (
                    <button type="button" className="genref-thumb-btn" onClick={() => setPreview({ title: label, images: [{ src: imageUrl, label }] })} aria-label={`查看 ${label} 大图`}>
                      <img src={imageUrl} alt={label} loading="lazy" decoding="async" />
                    </button>
                  )
                  : <div className="genref-thumb-empty">无图</div>}
                <figcaption title={label}>{label}</figcaption>
              </figure>
            )
          })}
        </div>
      )}
      <ReferenceAudioBlock audios={audios} skips={audioSkips} versionId={versionId} />
      {preview && <ImageCompareModal title={preview.title} images={preview.images} onClose={() => setPreview(null)} />}
    </section>
  )
}

/** 参考音频小节——拆出来只是为了让上面的主函数别再变长，不是独立文件（不涉及
 * scripts/check_css_split.py 的页面归属登记）。audios 为空且 skips 也为空时不
 * 渲染任何东西。 */
function ReferenceAudioBlock({ audios, skips, versionId }: {
  audios: ReferenceAudioInput[]
  skips: ReferenceAudioSkip[]
  versionId: string
}) {
  if (!audios.length && !skips.length) return null
  return (
    <div className="genref-audio-block">
      <div className="genref-audio-head"><b>参考音频</b></div>
      {!!audios.length && (
        <ul className="genref-audio-list">
          {audios.map(item => (
            <li className="genref-audio-item" key={`${item.voice_id}-${item.index}`}>
              <span className="genref-audio-label">{`音频${item.index} · ${item.character_name}`}</span>
              <VoicePlayButton
                voice={{ id: `${item.voice_id}:${versionId}`, audio_url: '', clip_url: item.clip_url, clip_duration_s: item.clip_duration_s }}
                label={`${item.character_name}的参考声音`}
              />
            </li>
          ))}
        </ul>
      )}
      {!!skips.length && (
        <p className="genref-audio-skip">
          未传声音：{skips.map(skip => `${skip.character_name}（${skip.reason}）`).join('、')}
        </p>
      )}
    </div>
  )
}
