// primitives: shared controls and small visual elements.
// No state, props-only. Used by chat / explore / logs.

export function ChipSelect({ label, value, options, onChange, suffix }) {
  return (
    <label className="chip-control param-select">
      <span className="chip-control-label">{label}</span>
      <span className="param-select-value">
        <select value={value} onChange={(e) => onChange(e.target.value)}>
          {options.map((o) => (
            <option key={o} value={o}>
              {o}
              {suffix || ''}
            </option>
          ))}
        </select>
      </span>
    </label>
  )
}

export function ChipNumber({ label, value, min, max, onChange }) {
  return (
    <label className="chip-control">
      <span className="chip-control-label">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  )
}

export function ChipToggle({ label, on, onChange }) {
  return (
    <button
      type="button"
      className={`chip-control toggle ${on ? 'on' : ''}`}
      onClick={() => onChange(!on)}
    >
      <span className="chip-control-label">{label}</span>
      <span className="chip-control-value">{on ? '开' : '关'}</span>
    </button>
  )
}

export function ThinkingDots() {
  return (
    <div className="thinking" aria-hidden="true">
      <span /><span /><span />
    </div>
  )
}

export function Suggestion({ prompt, onPick }) {
  return (
    <button type="button" className="suggestion" onClick={() => onPick(prompt)}>
      {prompt}
    </button>
  )
}

export function GhostButton({ children, danger, disabled, onClick, title, ariaLabel }) {
  return (
    <button
      type="button"
      className={`ghost-btn ${danger ? 'danger' : ''}`}
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={ariaLabel}
    >
      {children}
    </button>
  )
}
