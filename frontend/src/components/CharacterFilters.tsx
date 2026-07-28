import { useEffect, useId, useMemo, useRef, useState } from 'react'

export type CharacterFilterState = {
  role: string
  portrait: string
  qa: string
  missing: string
  firstAppearance: string
  version: string
  candidate: string
  sort: string
}

const DEFAULT: CharacterFilterState = {
  role: '', portrait: '', qa: '', missing: '', firstAppearance: '',
  version: '', candidate: '', sort: 'name',
}

export function characterFilterActiveCount(value: CharacterFilterState): number {
  return Object.entries(value).filter(([key, item]) => (
    key === 'sort' ? item !== DEFAULT.sort : Boolean(item)
  )).length
}

/** 利用 zh-CN 排序边界推导汉字拼音首字母，覆盖通用汉字而非少量角色白名单。 */
const PINYIN_BOUNDARIES = Array.from('啊芭擦搭蛾发噶哈击喀垃妈拿哦啪期然撒塌挖昔压匝')
const PINYIN_INITIALS = Array.from('ABCDEFGHJKLMNOPQRSTWXYZ')
const zhCollator = new Intl.Collator('zh-CN')

function pinyinHead(character: string): string {
  if (!/[\u3400-\u9fff]/u.test(character)) return character.toLowerCase()
  let initial = ''
  for (let index = 0; index < PINYIN_BOUNDARIES.length; index += 1) {
    if (zhCollator.compare(character, PINYIN_BOUNDARIES[index]) >= 0) initial = PINYIN_INITIALS[index] || ''
    else break
  }
  return initial.toLowerCase()
}

function fuzzyMatch(name: string, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  if (name.includes(query.trim())) return true
  const heads = Array.from(name).map(pinyinHead).join('')
  return heads.includes(q.replace(/\s+/g, ''))
}

export function matchCharacterFilters(
  character: { name: string; role?: string },
  query: string,
  filters: CharacterFilterState,
  meta: {
    availability: string
    hasPortrait: boolean
    hasCandidate?: boolean
    hasHistory?: boolean
    firstAppearance?: number | null
  },
): boolean {
  if (!fuzzyMatch(character.name, query) && !(character.role || '').includes(query.trim())) return false
  if (filters.role && !(character.role || '').includes(filters.role)) return false
  if (filters.portrait === 'yes' && !meta.hasPortrait) return false
  if (filters.portrait === 'no' && meta.hasPortrait) return false
  if (filters.qa && meta.availability !== filters.qa) return false
  if (filters.missing === 'yes' && meta.availability !== 'missing' && meta.availability !== 'failed') return false
  if (filters.missing === 'no' && (meta.availability === 'missing' || meta.availability === 'failed')) return false
  if (filters.firstAppearance === 'initial' && (meta.firstAppearance ?? 1) > 1) return false
  if (filters.firstAppearance === 'later' && (meta.firstAppearance ?? 1) <= 1) return false
  if (filters.version === 'current' && meta.hasHistory) return false
  if (filters.version === 'history' && !meta.hasHistory) return false
  if (filters.candidate === 'yes' && !meta.hasCandidate) return false
  if (filters.candidate === 'no' && meta.hasCandidate) return false
  return true
}

export default function CharacterFilters({
  value,
  onChange,
  roles,
}: {
  value: CharacterFilterState
  onChange: (next: CharacterFilterState) => void
  roles: string[]
}) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const activeCount = useMemo(() => characterFilterActiveCount(value), [value])

  const closeAndRestoreFocus = () => {
    setOpen(false)
    window.requestAnimationFrame(() => triggerRef.current?.focus())
  }

  const clearFilters = () => {
    onChange({ ...DEFAULT })
    window.requestAnimationFrame(() => triggerRef.current?.focus())
  }

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      closeAndRestoreFocus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div ref={rootRef} className="character-filters">
      <button
        ref={triggerRef}
        type="button"
        className="btn small"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={activeCount ? `角色筛选，已启用 ${activeCount} 项` : '角色筛选'}
        onClick={() => setOpen(v => !v)}
      >
        角色筛选{activeCount ? ` · ${activeCount}` : ''}
      </button>
      {activeCount > 0 && (
        <button type="button" className="btn small ghost" onClick={clearFilters}>清除筛选</button>
      )}
      {open && (
        <div id={panelId} className="character-filter-panel" role="group" aria-label="角色筛选条件">
          <label>
            身份
            <select value={value.role} onChange={e => onChange({ ...value, role: e.target.value })}>
              <option value="">全部</option>
              {roles.map(role => <option key={role} value={role}>{role}</option>)}
            </select>
          </label>
          <label>
            定妆
            <select value={value.portrait} onChange={e => onChange({ ...value, portrait: e.target.value })}>
              <option value="">全部</option>
              <option value="yes">已有图</option>
              <option value="no">未出图</option>
            </select>
          </label>
          <label>
            质检结果
            <select value={value.qa} onChange={e => onChange({ ...value, qa: e.target.value })}>
              <option value="">全部</option>
              <option value="passed">已采用且通过</option>
              <option value="warning">质量需复核</option>
              <option value="failed">暂不可用</option>
              <option value="unverified">待质检</option>
              <option value="missing">未出图</option>
              <option value="generating">生成或质检中</option>
            </select>
          </label>
          <label>
            缺口
            <select value={value.missing} onChange={e => onChange({ ...value, missing: e.target.value })}>
              <option value="">全部</option>
              <option value="yes">仅缺口</option>
              <option value="no">无缺口</option>
            </select>
          </label>
          <label>
            首次适用集
            <select value={value.firstAppearance} onChange={e => onChange({ ...value, firstAppearance: e.target.value })}>
              <option value="">全部</option>
              <option value="initial">第 1 集起</option>
              <option value="later">后续集数加入</option>
            </select>
          </label>
          <label>
            版本
            <select value={value.version} onChange={e => onChange({ ...value, version: e.target.value })}>
              <option value="">全部</option>
              <option value="current">仅当前版</option>
              <option value="history">有历史版本</option>
            </select>
          </label>
          <label>
            候选包
            <select value={value.candidate} onChange={e => onChange({ ...value, candidate: e.target.value })}>
              <option value="">全部</option>
              <option value="yes">有候选/历史</option>
              <option value="no">无候选</option>
            </select>
          </label>
          <label>
            排序
            <select value={value.sort} onChange={e => onChange({ ...value, sort: e.target.value })}>
              <option value="name">按名称</option>
              <option value="role">按身份</option>
              <option value="first">按首次适用集</option>
              <option value="qa">按质检状态</option>
            </select>
          </label>
          <div className="character-filter-actions">
            {activeCount > 0 && <button type="button" className="btn small ghost" onClick={clearFilters}>清除筛选</button>}
            <button type="button" className="btn small primary" onClick={closeAndRestoreFocus}>完成筛选</button>
          </div>
        </div>
      )}
    </div>
  )
}

export { DEFAULT as EMPTY_CHARACTER_FILTERS }
