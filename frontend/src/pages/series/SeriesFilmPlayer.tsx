import { useRef } from 'react'
import type { Film } from '../../api'
import { formatFileSize } from '../studioImport'

export function formatFilmDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const mm = String(m).padStart(2, '0')
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`
}

/** 点击章节应该把播放头跳到哪——按 episode_no 匹配对应章节的 start_s；
 *  找不到（数据缺失）返回 null，调用方据此忽略这次点击，不强行跳到 0 秒
 *  制造"点了没反应但其实跳错地方"的假象。 */
export function seriesChapterSeekTime(chapters: Film['chapters'], episodeNo: number): number | null {
  const found = chapters.find(chapter => chapter.episode_no === episodeNo)
  return found ? found.start_s : null
}

export default function SeriesFilmPlayer({ film }: { film: Film }) {
  const videoRef = useRef<HTMLVideoElement | null>(null)

  const seekToEpisode = (episodeNo: number) => {
    const target = seriesChapterSeekTime(film.chapters, episodeNo)
    if (target == null || !videoRef.current) return
    videoRef.current.currentTime = target
  }

  return (
    <section className="series-film-player card">
      <h3>连播成片</h3>
      <video ref={videoRef} controls src={film.url} className="series-film-video" />
      <div className="series-film-meta">
        <span>时长 {formatFilmDuration(film.duration_s)}</span>
        <span>大小 {formatFileSize(film.size_bytes)}</span>
        <a className="btn" href={film.url} download>下载连播成片</a>
      </div>
      <ol className="series-film-chapters">
        {film.chapters.map(chapter => (
          <li key={chapter.episode_no}>
            <button
              type="button"
              className="series-film-chapter-btn"
              onClick={() => seekToEpisode(chapter.episode_no)}
            >
              第 {chapter.episode_no} 集 · {formatFilmDuration(chapter.start_s)}
            </button>
          </li>
        ))}
      </ol>
    </section>
  )
}
