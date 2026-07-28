import { useEffect, useId, useState } from 'react'
import { useFocusTrap } from '../hooks/useFocusTrap'

export function imageCompareDisabledReason(
  action: 'compare' | 'reset' | 'zoomIn' | 'zoomOut' | 'previous' | 'next',
  imageCount: number,
  index: number,
  zoom: number,
): string {
  if (action === 'compare' && imageCount < 2) return '只有一张图片，无法并排对比'
  if (action === 'reset' && zoom === 1) return '当前已是 100% 缩放'
  if (action === 'zoomIn' && zoom >= 3) return '当前已放大到 300% 上限'
  if (action === 'zoomOut' && zoom <= 0.5) return '当前已缩小到 50% 下限'
  if (action === 'previous' && index <= 0) return '当前已是第一张'
  if (action === 'next' && index >= imageCount - 1) return '当前已是最后一张'
  return ''
}

export default function ImageCompareModal({
  title,
  images,
  onClose,
}: {
  title: string
  images: { src: string; label: string }[]
  onClose: () => void
}) {
  const trapRef = useFocusTrap(true, onClose)
  const titleId = useId()
  const [mode, setMode] = useState<'single' | 'compare'>('single')
  const [index, setIndex] = useState(0)
  const [zoom, setZoom] = useState(1)

  useEffect(() => {
    setMode('single')
    setIndex(0)
    setZoom(1)
  }, [title, images.length])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (mode !== 'single') return
      if (event.key === 'ArrowLeft') {
        event.preventDefault()
        setIndex(current => Math.max(0, current - 1))
      } else if (event.key === 'ArrowRight') {
        event.preventDefault()
        setIndex(current => Math.min(images.length - 1, current + 1))
      } else if (event.key === '+' || event.key === '=') {
        event.preventDefault()
        setZoom(current => Math.min(3, current + 0.25))
      } else if (event.key === '-') {
        event.preventDefault()
        setZoom(current => Math.max(0.5, current - 0.25))
      } else if (event.key === '0') {
        event.preventDefault()
        setZoom(1)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [images.length, mode])
  if (!images.length) return null
  const safeIndex = Math.min(index, images.length - 1)
  const current = images[safeIndex]
  const compareDisabledReason = imageCompareDisabledReason('compare', images.length, safeIndex, zoom)
  const resetDisabledReason = imageCompareDisabledReason('reset', images.length, safeIndex, zoom)
  const zoomInDisabledReason = imageCompareDisabledReason('zoomIn', images.length, safeIndex, zoom)
  const zoomOutDisabledReason = imageCompareDisabledReason('zoomOut', images.length, safeIndex, zoom)
  const previousDisabledReason = imageCompareDisabledReason('previous', images.length, safeIndex, zoom)
  const nextDisabledReason = imageCompareDisabledReason('next', images.length, safeIndex, zoom)

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={e => {
      if (e.currentTarget === e.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog image-compare-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h3 id={titleId}>{title}</h3>
        <div className="image-compare-toolbar">
          <button type="button" className={`btn small${mode === 'single' ? ' active' : ''}`}
            aria-pressed={mode === 'single'} onClick={() => setMode('single')}>单图查看</button>
          <button type="button" className={`btn small${mode === 'compare' ? ' active' : ''}`}
            aria-pressed={mode === 'compare'} disabled={Boolean(compareDisabledReason)}
            aria-label={compareDisabledReason ? `并排对比，暂不可用：${compareDisabledReason}` : `并排对比全部 ${images.length} 张图片`}
            onClick={() => setMode('compare')}>并排对比</button>
          {mode === 'single' && <>
            <button type="button" className="btn small" disabled={Boolean(resetDisabledReason)}
              aria-label={resetDisabledReason ? `重置缩放，暂不可用：${resetDisabledReason}` : '重置为 100% 缩放'}
              onClick={() => setZoom(1)}>重置缩放</button>
            <button type="button" className="btn small" disabled={Boolean(zoomInDisabledReason)}
              aria-label={zoomInDisabledReason ? `放大，暂不可用：${zoomInDisabledReason}` : '放大 25%'}
              onClick={() => setZoom(z => Math.min(3, z + 0.25))}>放大</button>
            <button type="button" className="btn small" disabled={Boolean(zoomOutDisabledReason)}
              aria-label={zoomOutDisabledReason ? `缩小，暂不可用：${zoomOutDisabledReason}` : '缩小 25%'}
              onClick={() => setZoom(z => Math.max(0.5, z - 0.25))}>缩小</button>
            <button type="button" className="btn small" disabled={Boolean(previousDisabledReason)}
              aria-label={previousDisabledReason ? `上一张，暂不可用：${previousDisabledReason}` : '上一张'}
              onClick={() => setIndex(i => i - 1)}>上一张</button>
            <button type="button" className="btn small" disabled={Boolean(nextDisabledReason)}
              aria-label={nextDisabledReason ? `下一张，暂不可用：${nextDisabledReason}` : '下一张'}
              onClick={() => setIndex(i => i + 1)}>下一张</button>
          </>}
          <span role="status">{mode === 'single'
            ? `第 ${safeIndex + 1} / ${images.length} 张 · ${Math.round(zoom * 100)}%`
            : `并排显示全部 ${images.length} 张`}</span>
        </div>
        {mode === 'single' ? (
          <figure className="image-compare-stage">
            <img src={current.src} alt={current.label}
              style={{ width: `${zoom * 100}%`, maxWidth: 'none' }} />
            <figcaption>{current.label}</figcaption>
          </figure>
        ) : (
          <div className="image-compare-grid">
            {images.map(image => (
              <figure key={image.src}>
                <img src={image.src} alt={image.label} />
                <figcaption>{image.label}</figcaption>
              </figure>
            ))}
          </div>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn primary" onClick={onClose}>关闭</button>
        </div>
      </section>
    </div>
  )
}
