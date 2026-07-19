interface SearchFieldProps {
  value: string
  onChange: (value: string) => void
  placeholder: string
  ariaLabel?: string
  className?: string
}

export default function SearchField({ value, onChange, placeholder, ariaLabel = '搜索', className = '' }: SearchFieldProps) {
  return (
    <div className={`search-field${className ? ` ${className}` : ''}`}>
      <span className="search-field-icon" aria-hidden="true" />
      <input
        type="search"
        value={value}
        aria-label={ariaLabel}
        placeholder={placeholder}
        onChange={event => onChange(event.target.value)}
      />
      {value && (
        <button type="button" aria-label="清空搜索" title="清空搜索" onClick={() => onChange('')}>×</button>
      )}
    </div>
  )
}
