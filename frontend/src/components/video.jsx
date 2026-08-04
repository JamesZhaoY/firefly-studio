// video: slide-over panel for 一键成片.
// Owns form / progress / result views. Polling is lifted to App (single source of truth).

import { useEffect, useMemo, useRef, useState } from 'react'
import { motion } from 'motion/react'
import { keyOf } from './util.js'

const SHOT_OPTIONS = [3, 4, 5, 6]
const DUR_OPTIONS = [4, 6, 8]
const ASPECT_OPTIONS = ['auto']
const DEFAULT_VOICE = 'zh-CN-XiaoxiaoNeural'

// 备选用 voice，避免 /api/voices 第一次还在飞时面板是空的
const FALLBACK_VOICES = [
  { id: 'zh-CN-XiaoxiaoNeural', name: '晓晓', gender: 'Female', locale: 'zh-CN' },
  { id: 'zh-CN-YunxiNeural', name: '云希', gender: 'Male', locale: 'zh-CN' },
  { id: 'en-US-JennyNeural', name: 'Jenny', gender: 'Female', locale: 'en-US' },
  { id: 'en-US-GuyNeural', name: 'Guy', gender: 'Male', locale: 'en-US' },
]

function ChipRow({ label, options, value, onChange, suffix = '' }) {
  return (
    <div className="vp-field">
      <span className="vp-label">{label}</span>
      <div className="vp-chip-row" role="radiogroup" aria-label={label}>
        {options.map((opt) => {
          const v = String(opt)
          const active = v === String(value)
          return (
            <button
              key={v}
              type="button"
              role="radio"
              aria-checked={active}
              className={`vp-chip ${active ? 'on' : ''}`}
              onClick={() => onChange(opt)}
            >
              {v}{suffix}
            </button>
          )
        })}
      </div>
    </div>
  )
}

// 通用下拉：voice / image model / video model 共用（复用 vp-voice-* 样式）
// 键盘：Esc 关闭；上/下箭头移动活动项；Enter 选择；首字母聚焦由 panel 焦点管理承担。
function SelectField({ label, value, options, onChange, placeholder = '未选择' }) {
  const [open, setOpen] = useState(false)
  const [focusIdx, setFocusIdx] = useState(-1)
  const wrapRef = useRef(null)
  const triggerRef = useRef(null)
  const listRef = useRef(null)
  const selected = options.find((o) => o.value === value) || null
  useEffect(() => {
    if (!open) return undefined
    function onDown(e) {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])
  useEffect(() => {
    if (!open) return
    const idx = Math.max(0, options.findIndex((o) => o.value === value))
    setFocusIdx(idx === -1 ? 0 : idx)
  }, [open, value, options])
  useEffect(() => {
    if (!open || focusIdx < 0) return
    const el = listRef.current?.querySelectorAll('.vp-voice-opt')?.[focusIdx]
    el?.focus()
  }, [focusIdx, open])

  function onTriggerKey(e) {
    if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      setOpen(true)
    }
  }
  function onListKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
      triggerRef.current?.focus()
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setFocusIdx((i) => Math.min(options.length - 1, i + 1))
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      setFocusIdx((i) => Math.max(0, i - 1))
      return
    }
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      const opt = options[focusIdx]
      if (opt) {
        onChange(opt.value)
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
  }

  return (
    <div className="vp-field" ref={wrapRef}>
      <span className="vp-label">{label}</span>
      <button
        ref={triggerRef}
        type="button"
        className={`vp-voice-trigger ${open ? 'open' : ''}`}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={onTriggerKey}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <span className="vp-voice-id">{selected ? selected.primary : placeholder}</span>
        {selected?.meta ? <span className="vp-voice-meta">{selected.meta}</span> : null}
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2"
             strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {open && (
        <ul
          className="vp-voice-list"
          role="listbox"
          aria-label={label}
          ref={listRef}
          onKeyDown={onListKey}
        >
          {options.map((o, i) => (
            <li key={o.value}>
              <button
                type="button"
                role="option"
                aria-selected={o.value === value}
                className={`vp-voice-opt ${o.value === value ? 'on' : ''}`}
                onClick={() => { onChange(o.value); setOpen(false) }}
                tabIndex={i === focusIdx ? 0 : -1}
              >
                <span className="vp-voice-opt-id">{o.primary}</span>
                {o.meta ? <span className="vp-voice-opt-meta">{o.meta}</span> : null}
              </button>
            </li>
          ))}
          {!options.length && <li className="vp-voice-empty">暂无选项</li>}
        </ul>
      )}
    </div>
  )
}

function VoiceSelect({ voices, value, onChange }) {
  const list = voices.length ? voices : FALLBACK_VOICES
  return (
    <SelectField
      label="语音"
      value={value}
      onChange={onChange}
      placeholder="选择语音"
      options={list.map((v) => ({
        value: v.id,
        primary: v.id,
        meta: `${v.name || ''}${v.gender ? ` · ${v.gender === 'Female' ? '女' : '男'}` : ''}${v.locale ? ` · ${v.locale}` : ''}`,
      }))}
    />
  )
}

function ModelSelect({ label, models, value, onChange }) {
  return (
    <SelectField
      label={label}
      value={value}
      onChange={onChange}
      placeholder="选模型"
      options={(models || []).map((m) => ({
        value: keyOf(m),
        primary: `${m.id}${m.version ? ` v${m.version}` : ''}`,
        meta: [m.label, m.provider, m.family].filter(Boolean).join(' · ') || '',
      }))}
    />
  )
}

function LlmModelSelect({ models, value, onChange }) {
  return (
    <SelectField
      label="分镜模型"
      value={value}
      onChange={onChange}
      placeholder={models.length ? '使用后端默认模型' : '分镜模型加载中…'}
      options={[
        { value: '', primary: '使用后端默认模型', meta: '由 LLM_MODEL 配置决定' },
        ...models.map((id) => ({ value: id, primary: id, meta: '来自 /v1/models' })),
      ]}
    />
  )
}

function FormView({ voices, llmModels, videoModels, busy, onSubmit, errorMsg }) {
  const [prompt, setPrompt] = useState('')
  const [shotCount, setShotCount] = useState(4)
  const [duration, setDuration] = useState(6)
  const [voice, setVoice] = useState(DEFAULT_VOICE)
  const [aspect, setAspect] = useState('16:9')
  const [useLlm, setUseLlm] = useState(false)
  const [videoModelKey, setVideoModelKey] = useState('')
  const [llmModel, setLlmModel] = useState('')
  const [advancedOpen, setAdvancedOpen] = useState(false)
  // 模型切换后给用户一次简短提示
  const [modelChangeNote, setModelChangeNote] = useState('')
  // voice 列表回到之后，更新默认选中（仅当用户没主动改过）
  useEffect(() => {
    if (!voices.length) return
    if (voices.some((v) => v.id === voice)) return
    setVoice(voices[0].id)
  }, [voices, voice])
  useEffect(() => {
    if (!videoModels.length || videoModelKey) return
    const prefer = videoModels.find((m) => String(m.id).includes('seedance')) || videoModels[0]
    setVideoModelKey(keyOf(prefer))
  }, [videoModels, videoModelKey])

  // 按当前所选视频模型的能力动态算可选时长 / 比例
  const selectedVideoModel = useMemo(
    () => videoModels.find((m) => keyOf(m) === videoModelKey) || null,
    [videoModels, videoModelKey],
  )
  const durOptions = useMemo(() => {
    const durs = selectedVideoModel?.durations?.length
      ? selectedVideoModel.durations
      : DUR_OPTIONS
    return durs.filter((d) => Number.isFinite(Number(d))).map((d) => Number(d))
  }, [selectedVideoModel])
  const aspectOptions = useMemo(() => {
    const aspects = selectedVideoModel?.aspect_ratios?.length
      ? selectedVideoModel.aspect_ratios
      : ASPECT_OPTIONS
    return aspects.filter((a) => typeof a === 'string' && a)
  }, [selectedVideoModel])
  const videoSupportsAudio = selectedVideoModel?.audio !== false

  // 模型切换时若当前 duration/aspect 不在能力里，落到合法值并提示
  useEffect(() => {
    if (!selectedVideoModel) return
    let changed = []
    if (durOptions.length && !durOptions.includes(Number(duration))) {
      changed.push(`${durOptions[0]}s`)
      setDuration(durOptions[0])
    }
    if (aspectOptions.length && !aspectOptions.includes(aspect)) {
      changed.push(aspectOptions[0])
      setAspect(aspectOptions[0])
    }
    if (changed.length) {
      setModelChangeNote(`已更新为该模型支持的 ${changed.join(' / ')}`)
      const t = setTimeout(() => setModelChangeNote(''), 4000)
      return () => clearTimeout(t)
    }
    return undefined
  }, [selectedVideoModel]) // eslint-disable-line react-hooks/exhaustive-deps

  // 输出摘要
  const totalSec = shotCount * Number(duration || 0)
  const totalText = totalSec >= 60
    ? `${Math.floor(totalSec / 60)}分${totalSec % 60}秒`
    : `${totalSec}秒`
  // 估算耗时：每镜生成经验值 60s + 拼接 30s
  const estSec = Math.max(120, shotCount * 60 + 30)
  const estText = estSec >= 60
    ? `${Math.floor(estSec / 60)}-${Math.ceil(estSec / 60) + 2} 分钟`
    : `${estSec}秒`

  const canSubmit = !busy && prompt.trim().length > 0 && !!selectedVideoModel
  const noModels = videoModels.length === 0

  function modelParts(models, key) {
    const m = models.find((x) => keyOf(x) === key)
    return m ? { model: m.id, version: m.version } : { model: '', version: '' }
  }

  function submit(e) {
    e?.preventDefault?.()
    if (!canSubmit) return
    const vid = modelParts(videoModels, videoModelKey)
    const videoSize = selectedVideoModel?.sizes_by_aspect?.[aspect] || 'auto'
    onSubmit({
      prompt: prompt.trim(),
      options: {
        shot_count: shotCount,
        duration_sec: duration,
        voice,
        aspect_ratio: aspect,
        video_size: videoSize,
        generate_audio: videoSupportsAudio,
        use_llm: useLlm,
        video_model: vid.model,
        video_model_version: vid.version,
        llm_model: llmModel.trim(),
      },
    })
  }

  return (
    <form className="vp-form" onSubmit={submit}>
      <label className="vp-field vp-field-grow">
        <span className="vp-label">描述</span>
        <textarea
          className="vp-textarea"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={4}
          placeholder="例如：清晨森林里的小鹿走入薄雾，远处是鹿群奔过，最后太阳升起照亮山谷。"
          aria-label="成片描述"
        />
      </label>

      <div className="vp-summary" aria-label="输出摘要">
        <span className="vp-summary-item">
          <span className="vp-summary-num">{shotCount}</span>
          <span className="vp-summary-label">镜头</span>
        </span>
        <span className="vp-summary-sep">×</span>
        <span className="vp-summary-item">
          <span className="vp-summary-num">{duration}{duration ? 's' : ''}</span>
          <span className="vp-summary-label">每镜</span>
        </span>
        <span className="vp-summary-sep">=</span>
        <span className="vp-summary-item">
          <span className="vp-summary-num">{totalText}</span>
          <span className="vp-summary-label">总时长（估）</span>
        </span>
        <span className="vp-summary-meta">
          {aspect || '比例未选'} · 预计耗时 {estText}
        </span>
      </div>

      <ModelSelect
        label="生成视频模型"
        models={videoModels}
        value={videoModelKey}
        onChange={setVideoModelKey}
      />
      {noModels && (
        <div className="vp-toggle-hint">正在加载视频模型…若持续为空请刷新或重试。</div>
      )}
      {modelChangeNote && (
        <div className="vp-toggle-hint vp-model-note" role="status">{modelChangeNote}</div>
      )}
      {!videoSupportsAudio && (
        <div className="vp-toggle-hint">当前视频模型不支持自带音频，将仅使用 TTS 配音。</div>
      )}
      <VoiceSelect voices={voices} value={voice} onChange={setVoice} />

      <ChipRow
        label="镜头数"
        options={SHOT_OPTIONS}
        value={shotCount}
        onChange={setShotCount}
      />
      <ChipRow
        label="每镜时长"
        options={durOptions}
        value={duration}
        onChange={setDuration}
        suffix="s"
      />
      <ChipRow
        label="比例"
        options={aspectOptions}
        value={aspect}
        onChange={setAspect}
      />

      <button
        type="button"
        className={`vp-advanced-toggle ${advancedOpen ? 'open' : ''}`}
        onClick={() => setAdvancedOpen((v) => !v)}
        aria-expanded={advancedOpen}
      >
        <span>高级选项</span>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2"
             strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {advancedOpen && (
        <div className="vp-advanced-panel">
          <label className="vp-toggle">
            <input
              type="checkbox"
              checked={useLlm}
              onChange={(e) => setUseLlm(e.target.checked)}
            />
            <span className="vp-toggle-track" aria-hidden="true">
              <span className="vp-toggle-knob" />
            </span>
            <span className="vp-toggle-text">
              <span>用 AI 拆镜</span>
              <span className="vp-toggle-hint">开启后走 LLM 分镜；否则按提示词长度启发式。</span>
            </span>
          </label>
          <LlmModelSelect models={llmModels} value={llmModel} onChange={setLlmModel} />
          <span className="vp-toggle-hint">开启「用 AI 拆镜」后生效；模型列表来自 LLM 服务的 `/v1/models`。</span>
        </div>
      )}

      {errorMsg && <div className="vp-error">{errorMsg}</div>}

      <button type="submit" className="vp-submit" disabled={!canSubmit}>
        {busy ? '提交中…' : (noModels ? '无可用视频模型' : '开始生成')}
      </button>
      <p className="vp-foot-hint">
        总时长为估算值，最终时长由实际视频与配音决定；面板可关闭，任务在后台继续。
      </p>
    </form>
  )
}

function ShotRow({ shot }) {
  const status = shot.status || 'queued'
  const iconClass =
    status === 'succeeded' ? 'ok' :
    status === 'failed' ? 'err' :
    status === 'running' ? 'run' : 'queued'
  const label = shot.label || `第 ${shot.index} 镜`
  const narration = shot.narration || ''
  return (
    <div className={`vp-shot ${iconClass}`}>
      <div className="vp-shot-thumb">{shot.videoUrl ? <video src={shot.videoUrl} muted preload="metadata" aria-label={`${label}视频`} /> : <span aria-hidden="true">镜头</span>}</div>
      <span className={`vp-shot-mark ${iconClass}`} aria-hidden="true">
        {status === 'succeeded' ? '✓' :
         status === 'failed' ? '×' :
         status === 'running' ? '•' : '◦'}
      </span>
      <div className="vp-shot-body">
        <div className="vp-shot-head">
          <span className="vp-shot-label">{label}</span>
          <span className="vp-shot-status">{stageLabel(shot.stage, status)}</span>
        </div>
        {narration && <div className="vp-shot-narration">{narration}</div>}
        {shot.visual_prompt && <div className="vp-shot-narration">提示词：{shot.visual_prompt}</div>}
        {shot.error && <div className="vp-shot-err">{shot.error}</div>}
      </div>
    </div>
  )
}

function statusLabel(s) {
  return s === 'queued' ? '排队' :
         s === 'running' ? '生成中' :
         s === 'succeeded' ? '完成' :
         s === 'failed' ? '失败' : s
}

function stageLabel(stage, status) {
  if (status === 'succeeded') return '完成'
  if (status === 'failed') return '失败'
  switch (stage) {
    case 'planned': return '待开始'
    case 'video': return '生成视频'
    case 'tts': return '合成配音'
    case 'concatenated': return '已加入成片'
    case 'done': return '完成'
    case 'failed': return '失败'
    default: return statusLabel(status)
  }
}

function ProgressView({ job, onClose }) {
  const shots = job?.shots || []
  const done = job?.status === 'succeeded'
  const failed = job?.status === 'failed'
  const stale = !done && !failed && shots.length > 0 && shots.every((s) => s.status === 'queued')
  const message = job?.message || '准备中…'
  const pct = Number(job?.progress || 0)
  const running = shots.find((s) => s.status === 'running') ||
                  shots.find((s) => s.status === 'queued')
  const runningLabel = running
    ? `生成第 ${running.index}/${shots.length} 镜 · ${running.label || '分镜'}`
    : (done ? '已完成' : failed ? '已失败' : '准备中')
  return (
    <div className="vp-progress">
      <div className="vp-progress-head">
        <span className={`vp-status-pill ${done ? 'ok' : failed ? 'err' : 'run'}`}>
          <span className="vp-status-dot" aria-hidden="true" />
          {done ? '完成' : failed ? '失败' : '生成中'}
        </span>
        <span className="vp-progress-msg">{runningLabel}</span>
      </div>

      <div className="vp-bar" aria-label="生成进度">
        <motion.i
          initial={false}
          animate={{ width: `${Math.max(2, Math.min(100, pct))}%` }}
          transition={{ duration: 0.4 }}
        />
      </div>
      <div className="vp-progress-foot" aria-live="polite">{message}</div>
      {stale && <div className="vp-idle-hint">仍在等待上游返回，可关闭面板后继续。</div>}

      <div className="vp-shots">
        {shots.map((s) => <ShotRow key={s.index} shot={s} />)}
        {!shots.length && <div className="vp-shots-empty">分镜生成中…</div>}
      </div>

      <div className="vp-progress-actions">
        <button type="button" className="vp-ghost" onClick={onClose}>
          关闭面板
        </button>
      </div>
      <p className="vp-foot-hint">
        任务在后台运行，可以切换到对话页面继续工作。
      </p>
    </div>
  )
}

function ResultView({ job, onAgain }) {
  const shots = job?.shots || []
  const videoSrc = job?.status === 'succeeded' ? (job?.finalVideoUrl || '') : ''
  const partial = job?.status === 'failed' && shots.length > 0
  const manifestUrl = job?.manifestUrl || ''
  return (
    <div className="vp-result">
      <div className="vp-video-frame">
        {videoSrc ? (
          <video controls src={videoSrc} preload="metadata" />
        ) : (
          <div className={`vp-result-summary ${partial ? 'warn' : 'err'}`} role="status">{partial ? '部分完成：分镜已保留，但最终合成失败。' : '生成失败：没有可用的最终成片。'}</div>
        )}
      </div>

      <div className="vp-result-meta">
        <span className="vp-meta-chip">
          {shots.length} 个分镜
        </span>
        {job?.ffprobeDuration ? (
          <span className="vp-meta-chip">
            {job.ffprobeDuration.toFixed(1)}s
          </span>
        ) : null}
        {job?.usedFfmpeg === false && (
          <span className="vp-meta-chip warn">未使用视频合成工具</span>
        )}
      </div>

      {manifestUrl && (
        <a className="vp-link" href={manifestUrl} target="_blank" rel="noreferrer">
          查看分镜清单 →
        </a>
      )}

      <div className="vp-result-shots">
        <div className="vp-result-shots-head">分镜缩略图</div>
        <div className="vp-result-grid">
          {shots.map((s) => (
            <div key={s.index} className="vp-result-tile">
              {s.videoUrl ? (
                <video src={s.videoUrl} muted controls preload="metadata" aria-label={s.label || `分镜 ${s.index}`} />
              ) : (
                <div className="vp-result-tile-empty">无图</div>
              )}
              <div className="vp-result-tile-meta">
                <span className="vp-result-tile-idx">#{s.index}</span>
                <span className={`vp-result-tile-status ${s.status}`}>
                  {statusLabel(s.status)}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <button type="button" className="vp-submit" onClick={onAgain}>
        再生成一条
      </button>
    </div>
  )
}

export default function VideoPanel({
  open,
  onClose,
  phase,            // 'form' | 'progress' | 'result'
  job,              // 当前 video job (含 shots)
  voices,
  llmModels,
  videoModels,
  onSubmit,         // ({prompt, options}) => Promise<void>
  submitting,       // 表单提交中
  submitError,
}) {
  const panelRef = useRef(null)

  // ESC 关闭
  useEffect(() => {
    if (!open) return undefined
    function onKey(e) { if (e.key === 'Escape') onClose?.() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // 打开：把焦点移进 dialog
  useEffect(() => {
    if (!open) return undefined
    const t = window.setTimeout(() => {
      const panel = panelRef.current
      if (!panel) return
      // 优先定位到描述框（form 阶段），否则取 panel 内第一个可聚焦元素
      const preferred = panel.querySelector('.vp-textarea')
      const target = preferred || panel.querySelector(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )
      target?.focus()
    }, 30)
    return () => window.clearTimeout(t)
  }, [open, phase])

  // 最小 focus trap：Tab/Shift+Tab 在 panel 内循环
  function onPanelKeyDown(e) {
    if (!open) return
    if (e.key !== 'Tab') return
    const panel = panelRef.current
    if (!panel) return
    const focusables = Array.from(panel.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )).filter((el) => el.offsetParent !== null || el === document.activeElement)
    if (focusables.length === 0) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  return (
    <div className={`vp-shell ${open ? 'open' : ''}`} aria-hidden={!open}>
      <div className="vp-backdrop" onClick={onClose} />
      <aside
        className="vp-panel"
        role="dialog"
        aria-modal="true"
        aria-label="一键成片"
        ref={panelRef}
        onKeyDown={onPanelKeyDown}
      >
        <header className="vp-head">
          <div className="vp-head-text">
            <h3 id="vp-title" tabIndex="-1">一键成片</h3>
            <p>写一段描述，自动拆镜、出图、出镜、出配音、合成。</p>
          </div>
          <button type="button" className="vp-close" onClick={onClose} aria-label="关闭">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth="2"
                 strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </header>

        <div className="vp-body">
          {phase === 'form' && (
            <FormView
              voices={voices}
              llmModels={llmModels}
              videoModels={videoModels}
              busy={submitting}
              onSubmit={onSubmit}
              errorMsg={submitError}
            />
          )}
          {phase === 'progress' && (
            <ProgressView job={job} onClose={onClose} />
          )}
          {phase === 'result' && (
            <ResultView job={job} onAgain={onSubmit /* 触发 reset：App 会把 phase 拉回 form */} />
          )}
        </div>
      </aside>
    </div>
  )
}
