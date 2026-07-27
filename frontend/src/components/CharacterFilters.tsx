import { useMemo, useState } from 'react'

export type CharacterFilterState = {
  role: string
  portrait: string
  qa: string
  missing: string
}

const DEFAULT: CharacterFilterState = { role: '', portrait: '', qa: '', missing: '' }

/** 简易拼音首字母：仅覆盖常见汉字，失败时回落包含匹配。 */
const PINYIN_HEAD: Record<string, string> = {
  萧: 'x', 炎: 'y', 药: 'y', 老: 'l', 美: 'm', 杜: 'd', 莎: 's',
  纳: 'n', 兰: 'l', 嫣: 'y', 然: 'r', 云: 'y', 韵: 'y', 海: 'h', 波: 'b',
}

function fuzzyMatch(name: string, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  if (name.includes(query.trim())) return true
  const heads = Array.from(name).map(ch => PINYIN_HEAD[ch] || ch.toLowerCase()).join('')
  return heads.includes(q.replace(/\s+/g, ''))
}

export function matchCharacterFilters(
  character: { name: string; role?: string },
  query: string,
  filters: CharacterFilterState,
  meta: { availability: string; hasPortrait: boolean; hasCandidate?: boolean },
): boolean {
  if (!fuzzyMatch(character.name, query) && !(character.role || '').includes(query.trim())) return false
  if (filters.role && !(character.role || '').includes(filters.role)) return false
  if (filters.portrait === 'yes' && !meta.hasPortrait) return false
  if (filters.portrait === 'no' && meta.hasPortrait) return false
  if (filters.qa && meta.availability !== filters.qa) return false
  if (filters.missing === 'yes' && meta.availability !== 'missing' && meta.availability !== 'failed') return false
  if (filters.missing === 'no' && (meta.availability === 'missing' || meta.availability === 'failed')) return false
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
  const activeCount = useMemo(
    () => Object.values(value).filter(Boolean).length,
    [value],
  )
  return (
    <div className="character-filters">
      <button type="button" className="btn small" onClick={() => setOpen(v => !v)}>
        筛选{activeCount ? ` · ${activeCount}` : ''}
      </button>
      {activeCount > 0 && (
        <button type="button" className="btn small ghost" onClick={() => onChange({ ...DEFAULT })}>清除筛选</button>
      )}
      {open && (
        <div className="character-filter-panel">
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
            QA
            <select value={value.qa} onChange={e => onChange({ ...value, qa: e.target.value })}>
              <option value="">全部</option>
              <option value="passed">已采用且通过</option>
              <option value="warning">有警告</option>
              <option value="failed">硬失败</option>
              <option value="unverified">待复核</option>
              <option value="missing">未出图</option>
              <option value="generating">生成中</option>
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
        </div>
      )}
    </div>
  )
}

export { DEFAULT as EMPTY_CHARACTER_FILTERS }
