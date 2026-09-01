import { useState } from 'react'
import type {
  StoryboardPackResourceCharacter,
  StoryboardPackResourceProp,
  StoryboardPackResourceScene,
  StoryboardPackResources,
} from '../api'
import { characterPortraitDisplay, type ImageGenTaskLike } from '../lib/bibleAssets'
import ImageCompareModal from './ImageCompareModal'
import PortraitPlaceholder from './PortraitPlaceholder'
import SceneReferencePlaceholder from './SceneReferencePlaceholder'

/**
 * 段落资源清单的展示名：优先用后端按本集映射包现算的 display_name；没有就从内部
 * 键里取人能读的部分——``bible:孟浩`` 的冒号后半段本来就是名字，而 ``entity:``
 * 后面是 sha256 前 16 位（app.identity_authority），对人零信息，只能显示中性占位，
 * 不许把哈希当名字摆出来（用户实测 2026-09-01：生成台整排群演都是一串 id）。
 * 原始 id 不丢，调用方挂在 title 上供溯源。
 */
function resourceLabel(displayName: string | undefined, id: string | undefined, fallback: string): string {
  if (displayName) return displayName
  if (!id) return fallback
  if (id.startsWith('entity:')) return fallback
  const readable = id.includes(':') ? id.slice(id.indexOf(':') + 1) : id
  return readable || fallback
}

type Preview = { title: string; images: { src: string; label: string }[] }
type OnPreview = (label: string, src: string) => void

/**
 * 分镜台（BoardPage.tsx）/ 生成台（WallPage.tsx）共用（用户拍板，2026-08-31，
 * 「传入素材」展示重做）：一段视频生成提示词声明涉及哪些人物/场景/道具，按当前
 * 人物谱/场景库解析出的定妆照/参考图。原来两个页面各自拆成「头部一排 30px 小圆
 * 缩略图」+「折叠详情里的 40px 缩略图列表」两处展示同一份 resources，用户反馈
 * 看不清、也分不出两处的区别——本组件合并成一处、常驻展示（不再需要点开折叠）、
 * 缩略图放大到可辨认人脸的尺寸，点击可看大图（复用 components/ImageCompareModal.tsx）。
 *
 * 这里展示的是「本段脚本声明涉及哪些实体」，不是「这一次生成实际发给供应商的
 * 参考图」——那件事只在生成台由 components/GenerationReferenceGallery.tsx 按具体
 * 一次生成尝试的 image_inputs 展示，两者语义不同，不在本组件里重复。
 *
 * 人物/场景两类缩略图都直接取后端现算的 current_portrait_image_url /
 * current_scene_image_url，不再拿段落里固化的 portrait_id / scene_reference_id
 * 快照去人物谱/场景库查图：出图解耦到后台后，分镜落库那一刻两个快照恒为 null，
 * 查图必然落空，图后来出好了也永远显示"待生成"（真实事故 2026-09-01 EP1）。
 * 因此本组件不再需要 bible。
 *
 * 缺图四态（生成中/失败/待生成/群演无定妆照）不重新判定，直接复用
 * components/PortraitPlaceholder.tsx、SceneReferencePlaceholder.tsx——同一份判据
 * 见 lib/bibleAssets.ts::resolvePortraitPlaceholderKind 的文档字符串。
 *
 * CSS 放 index.css 而不是某页的 styles/*.css：本组件被两个页面共用，放进页面样式
 * 表会被 scripts/check_css_split.py 判成跨页选择器（见该脚本文档）。
 */
export default function SegmentResourcePanel({ resources, project }: {
  resources: StoryboardPackResources
  project: ImageGenTaskLike | null | undefined
}) {
  const [preview, setPreview] = useState<Preview | null>(null)
  const onPreview: OnPreview = (label, src) => setPreview({ title: label, images: [{ src, label }] })
  const characters = resources.characters ?? []
  const scenes = resources.scenes ?? []
  const props = resources.props ?? []
  const empty = !characters.length && !scenes.length && !props.length

  return (
    <section className="segres-panel" aria-label="本段涉及素材">
      <div className="segres-head">
        <b>本段涉及素材</b>
        <span>按当前人物谱/场景库解析，不是这一次生成实际发送的参考图</span>
      </div>
      {empty
        ? <p className="segres-empty-hint">暂无数据</p>
        : (
          <div className="segres-groups">
            <CharacterGroup characters={characters} project={project} onPreview={onPreview} />
            <SceneGroup scenes={scenes} project={project} onPreview={onPreview} />
            <PropGroup props={props} />
          </div>
        )}
      {preview && <ImageCompareModal title={preview.title} images={preview.images} onClose={() => setPreview(null)} />}
    </section>
  )
}

function CharacterGroup({ characters, project, onPreview }: {
  characters: StoryboardPackResourceCharacter[]
  project: ImageGenTaskLike | null | undefined
  onPreview: OnPreview
}) {
  return (
    <div className="segres-group">
      <b>人物 · {characters.length}</b>
      <div className="segres-list">
        {characters.map((character, index) => {
          const { imageUrl, updated } = characterPortraitDisplay(character)
          const label = resourceLabel(character.display_name, character.identity_id, '未具名群演')
          return (
            <div className="segres-item" key={`c-${index}`}>
              {imageUrl
                ? (
                  <button type="button" className="segres-thumb-btn" onClick={() => onPreview(label, imageUrl)} aria-label={`查看 ${label} 大图`}>
                    <img className="segres-thumb" src={imageUrl} alt={label} loading="lazy" decoding="async" />
                    {updated && <span className="segres-thumb-updated" title="定妆照已更新，与本段素材记录当时依据的那张不同">已更新</span>}
                  </button>
                )
                : <PortraitPlaceholder identityId={character.identity_id} project={project} className="segres-thumb-empty" />}
              <div className="segres-body">
                <span className="segres-name" title={character.identity_id}>{label}</span>
                <span className="segres-desc">{character.description || (imageUrl ? '' : '暂无文字描述')}</span>
              </div>
            </div>
          )
        })}
        {!characters.length && <p className="segres-empty-hint">本段无人物资源</p>}
      </div>
    </div>
  )
}

function SceneGroup({ scenes, project, onPreview }: {
  scenes: StoryboardPackResourceScene[]
  project: ImageGenTaskLike | null | undefined
  onPreview: OnPreview
}) {
  return (
    <div className="segres-group">
      <b>场景 · {scenes.length}</b>
      <div className="segres-list">
        {scenes.map((scene, index) => {
          const imageUrl = scene.current_scene_image_url ?? null
          const label = resourceLabel(scene.display_name, scene.scene_id, '未命名场景')
          return (
            <div className="segres-item" key={`s-${index}`}>
              {imageUrl
                ? (
                  <button type="button" className="segres-thumb-btn" onClick={() => onPreview(label, imageUrl)} aria-label={`查看 ${label} 大图`}>
                    <img className="segres-thumb" src={imageUrl} alt={label} loading="lazy" decoding="async" />
                  </button>
                )
                : <SceneReferencePlaceholder sceneId={scene.scene_id} label={label} project={project} className="segres-thumb-empty" />}
              <div className="segres-body">
                <span className="segres-name" title={scene.scene_id}>{label}</span>
                <span className="segres-desc">{scene.description || (imageUrl ? '' : '暂无文字描述')}</span>
              </div>
            </div>
          )
        })}
        {!scenes.length && <p className="segres-empty-hint">本段无场景资源</p>}
      </div>
    </div>
  )
}

function PropGroup({ props }: { props: StoryboardPackResourceProp[] }) {
  return (
    <div className="segres-group">
      <b>道具 · {props.length}</b>
      <div className="segres-list">
        {/* 道具没有世界书图像素材库（设计使然），一律只有文字描述，用统一占位图标。 */}
        {props.map((prop, index) => (
          <div className="segres-item" key={`p-${index}`}>
            <div className="segres-thumb-icon" aria-hidden="true">物</div>
            <div className="segres-body">
              <span className="segres-name">{prop.label || '未命名道具'}</span>
              <span className="segres-desc">{prop.description || '暂无文字描述'}</span>
            </div>
          </div>
        ))}
        {!props.length && <p className="segres-empty-hint">本段无道具</p>}
      </div>
    </div>
  )
}
