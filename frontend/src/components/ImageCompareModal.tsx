import { useState } from 'react'

export default function ImageCompareModal({
  title,
  images,
  onClose,
}: {
  title: string
  images: { src: string; label: string }[]
  onClose: () => void
}) {
  const [mode, setMode] = useState<'single' | 'compare'>('single')
  const [index, setIndex] = useState(0)
  const [zoom, setZoom] = useState(1)
  if (!images.length) return null
  const current = images[Math.min(index, images.length - 1)]

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={e => {
      if (e.currentTarget === e.target) onClose()
    }}>
      <section className="impact-dialog image-compare-modal" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        <div className="image-compare-toolbar">
          <button type="button" className="btn small" onClick={() => setMode('single')}>单图</button>
          <button type="button" className="btn small" disabled={images.length < 2} onClick={() => setMode('compare')}>并排对比</button>
          <button type="button" className="btn small" onClick={() => setZoom(1)}>1:1</button>
          <button type="button" className="btn small" onClick={() => setZoom(z => Math.min(3, z + 0.25))}>放大</button>
          <button type="button" className="btn small" onClick={() => setZoom(z => Math.max(0.5, z - 0.25))}>缩小</button>
          <button type="button" className="btn small" disabled={index <= 0} onClick={() => setIndex(i => i - 1)}>上一张</button>
          <button type="button" className="btn small" disabled={index >= images.length - 1} onClick={() => setIndex(i => i + 1)}>下一张</button>
          <span>{index + 1}/{images.length}</span>
        </div>
        {mode === 'single' ? (
          <div className="image-compare-stage">
            <img src={current.src} alt={current.label} style={{ transform: `scale(${zoom})` }} />
            <p>{current.label}</p>
          </div>
        ) : (
          <div className="image-compare-grid">
            {images.slice(0, 3).map(image => (
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
