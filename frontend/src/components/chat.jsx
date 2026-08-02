// chat: welcome / thread / message bubble / output card / composer / param panel / model picker.
// All Motion usage isolated here (Section 3.A: motion lives in client leaves).
// State lives in App.jsx and is passed in via props.

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { ChipSelect, ChipNumber, ChipToggle, ThinkingDots, Suggestion } from './primitives.jsx'
import { fmtTime, keyOf, statusLabel } from './util.js'

const SUGGESTIONS = [
  '赛博朋克风格的东京夜景，雨后，霓虹反光',
  '一只橘猫在咖啡馆窗边打盹，胶片质感',
  '火星上的宇航员第一人称视角，远处是日落',
  '极简风的玻璃器皿产品摄影，工作室灯光',
]

export function Welcome({ onPick }) {
  return (
    <div className="welcome">
      <div className="welcome-orbit" aria-hidden="true">
        <div className="welcome-mark">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="1.5"
               strokeLinecap="round" strokeLinejoin="round">
            <path d="M13 2 L3 14 h7 l-1 8 10-12 h-7 z" />
          </svg>
        </div>
      </div>
      <span className="welcome-kicker">Firefly Studio</span>
      <h2>把你的想象变成画面</h2>
      <p>描述镜头、动作或风格，选择模型后即可开始生成。</p>
      <div className="suggestions">
        {SUGGESTIONS.map((s) => (
          <Suggestion key={s} prompt={s} onPick={onPick} />
        ))}
      </div>
    </div>
  )
}

function OutputCard({ url, type }) {
  const isVideo = type === 'video' || /\.(mp4|webm|mov)(\?|$)/i.test(url)
  const isImage = type === 'image' || /\.(png|jpe?g|webp|gif)(\?|$)/i.test(url)

  async function copy() {
    try {
      await navigator.clipboard.writeText(url)
    } catch {
      /* ponytail: clipboard can fail on insecure context / denied permission */
    }
  }

  return (
    <div className="output-card">
      <a className="output-thumb" href={url} target="_blank" rel="noreferrer">
        {isVideo ? (
          <video src={url} muted preload="metadata" />
        ) : isImage ? (
          <img src={url} alt="" loading="lazy" />
        ) : (
          <span>文件</span>
        )}
      </a>
      <div className="output-actions">
        <a className="action" href={url} target="_blank" rel="noreferrer">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3" />
          </svg>
          打开
        </a>
        <button type="button" className="action" onClick={copy}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" />
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
          </svg>
          复制链接
        </button>
      </div>
    </div>
  )
}

export function MessageBubble({ job }) {
  const reduce = useReducedMotion()
  const p = job.params || {}
  const outs = (job.outputs || job.files || []).filter((o) => o.url)
  const isVideoPipeline = job.kind === 'video_pipeline'
  const isActive = ['queued', 'running'].includes(job.status)
  const enter = reduce ? false : { opacity: 0, y: 6 }

  return (
    <>
      <motion.div
        className="bubble user"
        initial={enter}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.18, ease: [0.2, 0.7, 0.2, 1] }}
      >
        <div className="bubble-body">
          <div className="bubble-prompt">{p.prompt || job.prompt}</div>
          <div className="bubble-meta">
            <span className="meta-chip">
              {(p.model || job.model || p.model_version || job.model_version) ? [p.model || job.model, p.model_version || job.model_version].filter(Boolean).join(':') : '未指定模型'}
            </span>
            {p.size && <span className="meta-chip">{p.size}</span>}
            {p.duration && <span className="meta-chip">{p.duration}s</span>}
            {p.aspect_ratio && <span className="meta-chip">{p.aspect_ratio}</span>}
            <span className="meta-time">{fmtTime(job.created_at)}</span>
          </div>
        </div>
      </motion.div>

      <motion.div
        className="bubble assistant"
        initial={enter}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.18, delay: 0.04, ease: [0.2, 0.7, 0.2, 1] }}
      >
        <div className="avatar">ff</div>
        <div className="bubble-body">
          <div className="bubble-status">
            <span className={`status-dot ${job.status || 'queued'}`} />
            <span>{statusLabel(job.status)}</span>
            {job.message && <span className="status-msg">{job.message}</span>}
          </div>

          {isVideoPipeline ? (
            <div className="project-card">
              <strong>短片生成</strong><span>{p.shot_count || job.shot_count || 0} 个镜头</span><span>{p.aspect_ratio || job.aspect_ratio || '比例未知'}</span>
              <button type="button" className="action" onClick={() => window.dispatchEvent(new CustomEvent('open-video-job', { detail: job.id }))}>查看成片</button>
            </div>
          ) : isActive && (
            <div className="bar">
              <motion.i
                initial={reduce ? false : { width: 0 }}
                animate={{ width: `${Number(job.progress || 0)}%` }}
                transition={{ duration: 0.4 }}
              />
            </div>
          )}

          {outs.length > 0 && (
            <div className="outputs">
              {outs.map((o, i) => (
                <OutputCard key={i} url={o.url} type={o.type} />
              ))}
            </div>
          )}
        </div>
      </motion.div>
    </>
  )
}

export function Thread({ jobs, busy, onPickSuggestion, focusJobId, onFocused }) {
  const ref = useRef(null)
  const pinnedRef = useRef(true) // 是否贴在底部, ref 避免触发渲染
  const [showJump, setShowJump] = useState(false)

  // 监听滚动, 仅在容器接近底部时标记为跟随状态
  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    function onScroll() {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight
      const pinned = distance < 48
      pinnedRef.current = pinned
      setShowJump(!pinned && el.scrollHeight > el.clientHeight + 80)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  // 新消息或任务进度更新: 仅在用户已经贴在底部时, 跟随最新消息。
  useEffect(() => {
    const el = ref.current
    if (!el || !pinnedRef.current) return
    el.scrollTop = el.scrollHeight
  }, [jobs, busy])

  useLayoutEffect(() => {
    if (!focusJobId) return
    let attempts = 0
    let timer
    function locate() {
      const el = ref.current
      const target = document.getElementById(`job-${focusJobId}`)
      if (!el || !target) {
        if (++attempts < 8) timer = window.setTimeout(locate, 50)
        return
      }
      el.scrollTop = Math.max(0, target.offsetTop - 32)
      pinnedRef.current = false
      setShowJump(true)
      onFocused?.()
    }
    locate()
    return () => window.clearTimeout(timer)
  }, [focusJobId, onFocused])

  function scrollToBottom() {
    const el = ref.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    pinnedRef.current = true
    setShowJump(false)
  }

  return (
    <section className="thread" ref={ref}>
      <div className="thread-inner">
        {!jobs.length && !busy && <Welcome onPick={onPickSuggestion} />}
        {[...jobs].reverse().map((j) => (
          <div key={j.id} id={`job-${j.id}`} className="thread-job" data-job-id={j.id}>
            <MessageBubble job={j} />
          </div>
        ))}
        {busy && (
          <div className="bubble assistant">
            <div className="avatar">ff</div>
            <div className="bubble-body">
              <ThinkingDots />
              <div className="thinking-text">正在生成...</div>
            </div>
          </div>
        )}
      </div>
      <div className="thread-jump" aria-hidden={!showJump}>
        <button
          type="button"
          className="thread-jump-btn"
          onClick={scrollToBottom}
          aria-label="跳到最新消息"
          title="跳到最新消息"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      </div>
    </section>
  )
}

function ModelPicker({ filtered, filter, setFilter, total, selectedKey, onPick }) {
  return (
    <div className="picker">
      <div className="picker-toolbar">
        <span className="picker-search">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="过滤 seedance / veo / kling"
            aria-label="过滤模型"
          />
        </span>
        <span className="picker-meta">{filtered.length}/{total}</span>
      </div>
      <div className="picker-list" role="listbox" aria-label="可用模型">
        {!filtered.length && (
          <div className="picker-empty">
            {filter ? `无匹配「${filter}」` : '暂无模型'}
          </div>
        )}
        {filtered.map((m) => {
          const k = keyOf(m)
          const active = k === selectedKey
          return (
            <button
              type="button"
              key={k}
              role="option"
              aria-selected={active}
              className={`picker-row ${active ? 'active' : ''}`}
              onPointerDown={(e) => e.preventDefault()}
              onClick={() => onPick(k)}
            >
              <span className="picker-row-mark" aria-hidden="true" />
              <span className="picker-model">
                <span className="ver">{m.id}</span>
                <span className="picker-version">{m.version}</span>
              </span>
              <span className="picker-row-meta">
                {m.provider && <span className="provider">{m.provider}</span>}
                {m.release && (
                  <span className={`rel ${m.release === 'alpha' ? 'alpha' : ''}`}>
                    {m.release}
                  </span>
                )}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

function ParamPanel({
  selected, kind,
  filtered, total, filter, setFilter, onPickModel, selectedKey,
  size, setSize, detail, setDetail, n, setN,
  duration, setDuration, aspect, setAspect, audio, setAudio,
  seeds, setSeeds,
}) {
  if (!selected) return null
  return (
    <div className="params">
      <div className="params-section">
        <div className="params-title">模型</div>
        <ModelPicker
          filtered={filtered}
          filter={filter}
          setFilter={setFilter}
          total={total}
          selectedKey={selectedKey}
          onPick={onPickModel}
        />
      </div>

      <div className="params-row">
        <div className="params-section">
          <div className="params-title">参数</div>
          {kind === 'image' ? (
            <div className="chip-row">
              <ChipSelect label="尺寸" value={size}
                          options={selected.sizes || ['auto']}
                          onChange={setSize} />
              <ChipSelect label="细节" value={String(detail)}
                          options={['1', '2', '3', '4', '5']}
                          onChange={(v) => setDetail(Number(v))} />
              <ChipNumber label="生成数量" value={n}
                          min={1} max={selected.max_n || 4}
                          onChange={setN} />
            </div>
          ) : (
            <div className="chip-row">
              <ChipSelect label="时长" value={String(duration)}
                          options={(
                            selected.durations?.length
                              ? selected.durations
                              : [4, 5, 6, 8, 12]
                          ).map((d) => String(d))}
                          suffix="s"
                          onChange={(v) => setDuration(Number(v))} />
              <ChipSelect label="比例" value={aspect}
                          options={
                            selected.aspect_ratios?.length
                              ? selected.aspect_ratios
                              : ['16:9', '9:16']
                          }
                          onChange={setAspect} />
              <ChipToggle label="音频" on={audio} onChange={setAudio} />
              <ChipNumber label="生成数量" value={n}
                          min={1} max={selected.max_n || 1}
                          onChange={setN} />
            </div>
          )}
        </div>
      </div>

      <div className="params-row">
        <div className="params-section">
          <div className="params-title">种子（可选）</div>
          <input
            className="seed-input"
            value={seeds}
            onChange={(e) => setSeeds(e.target.value)}
            placeholder="留空随机"
          />
        </div>
      </div>
    </div>
  )
}

export function Composer(props) {
  const {
    kind, setKind, selected, paramsOpen, setParamsOpen,
    size, detail, duration, aspect, audio, seeds,
    n,
    prompt, setPrompt, busy, onGenerate, msg,
    filtered, total, filter, setFilter,
    selectedKey, onPickModel,
    setSize, setDetail, setN, setDuration, setAspect, setAudio, setSeeds,
  } = props

  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      onGenerate()
    }
  }

  return (
    <footer className="composer-wrap">
      <div className="composer">
        <AnimatePresence initial={false}>
          {paramsOpen && (
            <motion.div
              className="params-popover"
              key="params"
              initial={{ opacity: 0, y: 6, scale: 0.99 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.99 }}
              transition={{ duration: 0.16, ease: [0.2, 0.7, 0.2, 1] }}
            >
              <ParamPanel
                selected={selected}
                kind={kind}
                filtered={filtered}
                total={total}
                filter={filter}
                setFilter={setFilter}
                onPickModel={onPickModel}
                selectedKey={selectedKey}
                size={size} setSize={setSize}
                detail={detail} setDetail={setDetail}
                n={n} setN={setN}
                duration={duration} setDuration={setDuration}
                aspect={aspect} setAspect={setAspect}
                audio={audio} setAudio={setAudio}
                seeds={seeds} setSeeds={setSeeds}
              />
            </motion.div>
          )}
        </AnimatePresence>

        <div className="chips">
          <button
            type="button"
            className={`chip kind ${kind === 'image' ? 'on' : ''}`}
            onClick={() => setKind('image')}
            aria-pressed={kind === 'image'}
            title="图片生成 (Cmd .)"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <circle cx="9" cy="9" r="2" />
              <path d="M21 15l-5-5L5 21" />
            </svg>
            图片
          </button>
          <button
            type="button"
            className={`chip kind ${kind === 'video' ? 'on' : ''}`}
            onClick={() => setKind('video')}
            aria-pressed={kind === 'video'}
            title="视频生成 (Cmd .)"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polygon points="5 3 19 12 5 21 5 3" />
            </svg>
            视频
          </button>
          <button
            type="button"
            className={`chip model-chip ${paramsOpen ? 'on' : ''}`}
            onClick={() => setParamsOpen((v) => !v)}
            aria-expanded={paramsOpen}
            title="参数 (Cmd /)"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
            <span className="chip-model-label">
              {selected ? `${selected.id}:${selected.version}` : '选择模型'}
            </span>
          </button>
          {paramsOpen && (
            <>
              {kind === 'image' && size !== 'auto' && <span className="chip ghost">{size}</span>}
              {kind === 'image' && <span className="chip ghost">细节 {detail}</span>}
              {kind === 'video' && (
                <>
                  <span className="chip ghost">{duration}s</span>
                  <span className="chip ghost">{aspect}</span>
                  {!audio && <span className="chip ghost">无声</span>}
                </>
              )}
              {seeds && <span className="chip ghost">种子 {seeds}</span>}
            </>
          )}
        </div>

        <div className="textarea-wrap">
          <textarea
            className="composer-input"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={onKey}
            placeholder={
              kind === 'video'
                ? '描述一个动作或场景...（Shift+Enter 换行，Cmd+Enter 提交）'
                : '描述画面内容、风格、构图...（Shift+Enter 换行，Cmd+Enter 提交）'
            }
            rows={1}
            aria-label={kind === 'video' ? '视频生成描述' : '图片生成描述'}
          />
          <button
            type="button"
            className="send"
            onClick={onGenerate}
            disabled={busy || !prompt.trim() || !selected}
            aria-label="提交"
          >
            {busy ? (
              <svg className="spin" width="16" height="16" viewBox="0 0 24 24"
                   fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M21 12a9 9 0 1 1-6.219-8.56" />
              </svg>
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" strokeWidth="2.5"
                   strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 19V5M5 12l7-7 7 7" />
              </svg>
            )}
          </button>
        </div>

        <div className="composer-foot">
          <AnimatePresence mode="wait">
            {msg.text ? (
              <motion.span
                key={msg.text + msg.type}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.12 }}
                className={`hint ${msg.type}`}
              >
                {msg.text}
              </motion.span>
            ) : (
              <span className="hint shortcut">⌘ / Ctrl + Enter 提交 · Shift + Enter 换行</span>
            )}
          </AnimatePresence>
        </div>
      </div>
    </footer>
  )
}
