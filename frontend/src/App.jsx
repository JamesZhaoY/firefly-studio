import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { api } from './api'
import './App.css'

const SIZE_FALLBACK = {
  '16:9': '854x480',
  '9:16': '480x854',
  '1:1': '720x720',
  '4:3': '640x480',
  '3:4': '480x640',
}

function keyOf(m) {
  return `${m.id}@@${m.version}`
}

function fmtTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const today = new Date()
  const sameDay =
    d.getFullYear() === today.getFullYear() &&
    d.getMonth() === today.getMonth() &&
    d.getDate() === today.getDate()
  const time = d.toTimeString().slice(0, 5)
  return sameDay ? time : `${d.getMonth() + 1}/${d.getDate()} ${time}`
}

export default function App() {
  const [page, setPage] = useState('chat') // chat | explore | logs
  const [kind, setKind] = useState('image')
  const [presets, setPresets] = useState({ image: [], video: [], audio: [] })
  const [modelSource, setModelSource] = useState('')
  const [filter, setFilter] = useState('')
  const [selectedKey, setSelectedKey] = useState('')
  const [prompt, setPrompt] = useState('')
  const [n, setN] = useState(1)
  const [size, setSize] = useState('auto')
  const [detail, setDetail] = useState(3)
  const [duration, setDuration] = useState(6)
  const [aspect, setAspect] = useState('16:9')
  const [audio, setAudio] = useState(true)
  const [seeds, setSeeds] = useState('')
  const [msg, setMsg] = useState({ text: '', type: '' })
  const [busy, setBusy] = useState(false)
  const [auth, setAuth] = useState(null)
  const [jobs, setJobs] = useState([])
  const [logs, setLogs] = useState([])

  // collapsed popover state
  const [paramsOpen, setParamsOpen] = useState(false)

  const promptRef = useRef(null)
  const modelSearchRef = useRef(null)
  const threadRef = useRef(null)

  const allKindModels = presets[kind] || []
  const models = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return allKindModels
    return allKindModels.filter((m) => {
      const blob = [m.id, m.version, m.label, m.family, m.provider, m.release]
        .map((x) => String(x || '').toLowerCase())
        .join(' ')
      return blob.includes(q)
    })
  }, [allKindModels, filter])

  const selected = useMemo(
    () => allKindModels.find((m) => keyOf(m) === selectedKey) || null,
    [allKindModels, selectedKey],
  )

  const showMsg = (text, type = '') => setMsg({ text, type })

  const loadHealth = useCallback(async () => {
    try {
      setAuth((await api.health()).auth || null)
    } catch {
      setAuth(null)
    }
  }, [])

  const loadModels = useCallback(async (force = false) => {
    showMsg(force ? '正在从 Adobe 刷新…' : '加载模型…')
    try {
      const data = await api.models(force ? { refresh: '1' } : {})
      const next = {
        image: data.presets?.image || [],
        video: data.presets?.video || [],
        audio: data.presets?.audio || [],
      }
      setPresets(next)
      setModelSource(data.source || '')
      const c = data.counts || {}
      const total = data.total || 0
      const hasSeedance = (next.video || []).some((m) =>
        String(m.id).includes('seedance'),
      )
      showMsg(
        `模型 ${data.source || '?'} · ${total}（图 ${c.image || 0} / 视频 ${c.video || 0}）${hasSeedance ? ' · 含 seedance' : ''}`,
        total > 20 ? 'ok' : '',
      )
    } catch (e) {
      showMsg(`加载失败：${e.message}`, 'err')
    }
  }, [])

  const loadJobs = useCallback(async () => {
    try {
      setJobs((await api.jobs(50)).jobs || [])
    } catch (e) {
      showMsg(e.message, 'err')
    }
  }, [])

  const loadLogs = useCallback(async () => {
    try {
      setLogs((await api.logs({ limit: 80 })).logs || [])
    } catch (e) {
      showMsg(e.message, 'err')
    }
  }, [])

  async function onClearChat() {
    if (!jobs.length) return
    if (!window.confirm(`清空对话？将删除 ${jobs.length} 个任务及其日志。`)) return
    try {
      await api.clearChat()
      setJobs([])
      showMsg('对话已清空', 'ok')
    } catch (e) {
      showMsg(`清空失败：${e.message}`, 'err')
    }
  }

  async function onClearLogs() {
    if (!logs.length) return
    if (!window.confirm(`清空日志？将删除 ${logs.length} 条调用日志。`)) return
    try {
      await api.clearLogs()
      setLogs([])
      showMsg('日志已清空', 'ok')
    } catch (e) {
      showMsg(`清空失败：${e.message}`, 'err')
    }
  }

  useEffect(() => {
    loadHealth()
    loadModels(false)
    loadJobs()
    const t = setInterval(loadHealth, 30000)
    return () => clearInterval(t)
  }, [loadHealth, loadModels, loadJobs])

  useEffect(() => {
    const busyJobs = jobs.some((j) => ['queued', 'running'].includes(j.status))
    if (!busyJobs) return undefined
    const t = setInterval(loadJobs, 2500)
    return () => clearInterval(t)
  }, [jobs, loadJobs])

  useEffect(() => {
    if (page === 'logs') loadLogs()
  }, [page, loadLogs])

  useEffect(() => {
    if (!threadRef.current) return
    threadRef.current.scrollTop = threadRef.current.scrollHeight
  }, [jobs.length, busy])

  useEffect(() => {
    if (!allKindModels.length) return
    if (allKindModels.some((m) => keyOf(m) === selectedKey)) return
    const prefer =
      allKindModels.find((m) => String(m.id).includes('seedance')) ||
      allKindModels.find((m) => m.id === 'gpt-image' && String(m.version) === '2') ||
      allKindModels.find((m) => m.id === 'veo') ||
      allKindModels[0]
    setSelectedKey(keyOf(prefer))
  }, [allKindModels, selectedKey])

  useEffect(() => {
    if (!selected) return
    if (kind === 'image') {
      setSize(selected.default_size || (selected.sizes?.[0] || 'auto'))
      setDetail(selected.default_detail || 3)
    } else {
      const durs = selected.durations?.length ? selected.durations : [4, 5, 6, 8, 12]
      setDuration(selected.default_duration || durs[0])
      const aspects = selected.aspect_ratios?.length
        ? selected.aspect_ratios
        : ['16:9', '9:16']
      setAspect(selected.default_aspect_ratio || aspects[0])
      setAudio(selected.audio !== false)
    }
    setN(1)
  }, [selected, kind])

  useEffect(() => {
    function onKey(e) {
      const meta = e.metaKey || e.ctrlKey
      if (meta && e.key === 'Enter' && !busy) {
        e.preventDefault()
        onGenerate()
      } else if (meta && e.key === '/') {
        e.preventDefault()
        setParamsOpen((v) => !v)
      } else if (meta && e.key === '.') {
        e.preventDefault()
        setKind((k) => (k === 'image' ? 'video' : 'image'))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    busy, selected, prompt, kind, n, size, detail, duration, aspect, audio, seeds,
  ])

  async function onGenerate() {
    if (!selected) return showMsg('请选择模型', 'err')
    if (!prompt.trim()) return showMsg('请输入提示词', 'err')
    const body = {
      kind,
      prompt: prompt.trim(),
      model: selected.id,
      model_version: selected.version,
      n: Number(n) || 1,
      seeds: seeds.trim(),
    }
    if (kind === 'image') {
      body.size = size
      body.detail_level = Number(detail) || 3
    } else {
      body.duration = Number(duration) || 6
      body.aspect_ratio = aspect || '16:9'
      body.generate_audio = !!audio
      const map = selected.sizes_by_aspect || {}
      let sizeStr = map[body.aspect_ratio] || ''
      if (!sizeStr && Array.isArray(selected.sizes)) {
        const hit = selected.sizes.find(
          (s) => typeof s === 'string' && /\d+x\d+/i.test(s),
        )
        sizeStr = hit && hit !== 'auto' ? hit : ''
      }
      body.size = sizeStr || SIZE_FALLBACK[body.aspect_ratio] || '854x480'
    }
    setBusy(true)
    showMsg('提交中…')
    try {
      const data = await api.generate(body)
      showMsg(`已创建任务 ${data.job_id}`, 'ok')
      await loadJobs()
    } catch (e) {
      showMsg(e.message || String(e), 'err')
    } finally {
      setBusy(false)
    }
  }

  const authCls = !auth
    ? 'err'
    : auth.token_ok || auth.can_ims_refresh
      ? 'ok'
      : 'err'
  const authText = !auth
    ? '离线'
    : auth.token_ok || auth.can_ims_refresh
      ? auth.client_id || '已就绪'
      : '未登录'

  const total = allKindModels.length
  const filtered = models

  return (
    <div className="app">
      {/* ── sidebar ───────────────────────────────────── */}
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13 2 L3 14 h7 l-1 8 10-12 h-7 z" />
            </svg>
          </div>
          <div className="brand-text">
            <span className="brand-name">Firefly</span>
            <span className="brand-sub">studio</span>
          </div>
        </div>

        <div className="nav-section-label">工作区</div>
        <nav className="nav">
          <NavItem
            page={page}
            id="chat"
            setPage={setPage}
            icon={
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
              </svg>
            }
            label="对话"
            hint="⌘K"
          />
          <NavItem
            page={page}
            id="explore"
            setPage={setPage}
            icon={
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="9"/>
                <path d="m21 21-4.3-4.3"/>
                <path d="M11 11a2 2 0 1 0 4 0 2 2 0 0 0-4 0z" opacity=".4"/>
              </svg>
            }
            label="探索"
            hint="⌘E"
          />
          <NavItem
            page={page}
            id="logs"
            setPage={setPage}
            icon={
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 12h6m-3-3h6m-3 6h6m4 0h3M3 6h18M3 18h18"/>
              </svg>
            }
            label="日志"
            hint={`${logs.length || ''}`}
          />
        </nav>

        <div className="nav-section-label">最近</div>
        <div className="recent">
          {jobs.slice(0, 6).map((j) => {
            const p = j.params || {}
            return (
              <div className="recent-item" key={j.id} title={p.prompt || j.prompt}>
                <span className={`dot ${j.status}`} />
                <span className="recent-text">
                  {(p.prompt || j.prompt || '').slice(0, 22)}
                  {(p.prompt || '').length > 22 ? '…' : ''}
                </span>
              </div>
            )
          })}
          {!jobs.length && <div className="recent-empty">还没有任务</div>}
        </div>

        <div className="sidebar-foot">
          <div className={`auth ${authCls}`} title={authText}>
            <span className="dot" />
            <div className="auth-text">
              <span className="auth-label">{authText}</span>
              {auth?.expires_in_sec != null && auth.expires_in_sec > 0 && (
                <span className="auth-time">
                  {Math.max(0, Math.floor(auth.expires_in_sec / 60))}m
                </span>
              )}
            </div>
          </div>
          <button
            type="button"
            className="ghost-btn"
            onClick={() => loadModels(true)}
          >
            刷新模型
          </button>
        </div>
      </aside>

      {/* ── main ─────────────────────────────────────── */}
      {page === 'chat' ? (
        <main className="main">
          <header className="topbar">
            <div className="crumb">
              <span className="crumb-k">对话</span>
              <span className="crumb-d">{jobs.length} 个任务</span>
            </div>
            <div className="spacer" />
            <div className="src-chip">{modelSource || '—'}</div>
            <button
              type="button"
              className="ghost-btn danger"
              onClick={onClearChat}
              disabled={!jobs.length}
              title="清空对话"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
              清空
            </button>
          </header>

          <section className="thread" ref={threadRef}>
            <div className="thread-inner">
              {!jobs.length && !busy && (
                <div className="welcome">
                  <div className="welcome-mark">
                    <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M13 2 L3 14 h7 l-1 8 10-12 h-7 z" />
                    </svg>
                  </div>
                  <h2>告诉我你想生成什么</h2>
                  <p>描述画面或动作，Firefly 帮你生成。</p>
                  <div className="suggestions">
                    {[
                      '赛博朋克风格的东京夜景，雨后，霓虹反光',
                      '一只橘猫在咖啡馆窗边打盹，胶片质感',
                      '火星上的宇航员第一人称视角，远处是日落',
                      '极简风的玻璃器皿产品摄影，工作室灯光',
                    ].map((s) => (
                      <button
                        key={s}
                        type="button"
                        className="suggestion"
                        onClick={() => setPrompt(s)}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {jobs.map((j) => (
                <MessageBubble key={j.id} job={j} />
              ))}

              {busy && (
                <div className="bubble assistant">
                  <div className="avatar">ff</div>
                  <div className="bubble-body">
                    <div className="thinking">
                      <span /><span /><span />
                    </div>
                    <div className="thinking-text">正在生成…</div>
                  </div>
                </div>
              )}
            </div>
          </section>

          <footer className="composer-wrap">
            <div className="composer">
              <AnimatePresence>
                {paramsOpen && (
                  <motion.div
                    key="params"
                    className="params"
                    initial={{ opacity: 0, y: 8, scale: 0.98 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: 8, scale: 0.98 }}
                    transition={{ duration: 0.16, ease: [0.2, 0.7, 0.2, 1] }}
                  >
                    <div className="params-section">
                      <div className="params-title">模型</div>
                      <div className="picker">
                        <div className="picker-toolbar">
                          <input
                            ref={modelSearchRef}
                            type="search"
                            value={filter}
                            onChange={(e) => setFilter(e.target.value)}
                            placeholder="过滤 · seedance / veo / kling"
                          />
                          <span className="picker-meta">
                            {filtered.length}/{total}
                          </span>
                        </div>
                        <div className="picker-list">
                          {!filtered.length && (
                            <div className="picker-empty">
                              {filter ? `无匹配「${filter}」` : '暂无模型'}
                            </div>
                          )}
                          {filtered.map((m) => {
                            const k = keyOf(m)
                            const active = k === selectedKey
                            return (
                              <div
                                key={k}
                                role="option"
                                aria-selected={active}
                                className={`picker-row ${active ? 'active' : ''}`}
                                onClick={() => setSelectedKey(k)}
                              >
                                <span className="ver">
                                  {m.id}:{m.version}
                                </span>
                                {m.provider && (
                                  <span className="provider">{m.provider}</span>
                                )}
                                {m.release && (
                                  <span
                                    className={`rel ${
                                      m.release === 'alpha' ? 'alpha' : ''
                                    }`}
                                  >
                                    {m.release}
                                  </span>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      </div>
                    </div>

                    <div className="params-row">
                      <div className="params-section">
                        <div className="params-title">参数</div>
                        {kind === 'image' ? (
                          <div className="chip-row">
                            <ChipSelect
                              label="尺寸"
                              value={size}
                              options={selected?.sizes || ['auto']}
                              onChange={setSize}
                            />
                            <ChipSelect
                              label="细节"
                              value={String(detail)}
                              options={['1', '2', '3', '4', '5']}
                              onChange={(v) => setDetail(Number(v))}
                            />
                            <ChipNumber
                              label="n"
                              value={n}
                              min={1}
                              max={selected?.max_n || 4}
                              onChange={setN}
                            />
                          </div>
                        ) : (
                          <div className="chip-row">
                            <ChipSelect
                              label="时长"
                              value={String(duration)}
                              options={(
                                selected?.durations?.length
                                  ? selected.durations
                                  : [4, 5, 6, 8, 12]
                              ).map((d) => String(d))}
                              suffix="s"
                              onChange={(v) => setDuration(Number(v))}
                            />
                            <ChipSelect
                              label="比例"
                              value={aspect}
                              options={
                                selected?.aspect_ratios?.length
                                  ? selected.aspect_ratios
                                  : ['16:9', '9:16']
                              }
                              onChange={setAspect}
                            />
                            <ChipToggle
                              label="音频"
                              on={audio}
                              onChange={setAudio}
                            />
                            <ChipNumber
                              label="n"
                              value={n}
                              min={1}
                              max={selected?.max_n || 1}
                              onChange={setN}
                            />
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
                  </motion.div>
                )}
              </AnimatePresence>

              <div className="chips">
                <button
                  type="button"
                  className={`chip kind ${kind === 'image' ? 'on' : ''}`}
                  onClick={() => setKind('image')}
                  title="图片生成 (⌘.)"
                >
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/></svg>
                  图片
                </button>
                <button
                  type="button"
                  className={`chip kind ${kind === 'video' ? 'on' : ''}`}
                  onClick={() => setKind('video')}
                  title="视频生成 (⌘.)"
                >
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                  视频
                </button>
                <button
                  type="button"
                  className="chip"
                  onClick={() => setParamsOpen((v) => !v)}
                  title="参数 (⌘/)"
                >
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
                  {selected ? `${selected.id}:${selected.version}` : '选择模型'}
                </button>
                {paramsOpen && (
                  <>
                    {kind === 'image' && size !== 'auto' && (
                      <span className="chip ghost">{size}</span>
                    )}
                    {kind === 'image' && (
                      <span className="chip ghost">细节 {detail}</span>
                    )}
                    {kind === 'video' && (
                      <>
                        <span className="chip ghost">{duration}s</span>
                        <span className="chip ghost">{aspect}</span>
                        {!audio && <span className="chip ghost">无声</span>}
                      </>
                    )}
                    {seeds && <span className="chip ghost">seed {seeds}</span>}
                  </>
                )}
              </div>

              <div className="textarea-wrap">
                <textarea
                  ref={promptRef}
                  className="composer-input"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  onKeyDown={(e) => {
                    if (
                      e.key === 'Enter' &&
                      !e.shiftKey &&
                      (e.metaKey || e.ctrlKey)
                    ) {
                      e.preventDefault()
                      onGenerate()
                    }
                  }}
                  placeholder={
                    kind === 'video'
                      ? '描述一个动作或场景…（Shift+Enter 换行，⌘+Enter 提交）'
                      : '描述画面内容、风格、构图…（Shift+Enter 换行，⌘+Enter 提交）'
                  }
                  rows={1}
                />
                <button
                  type="button"
                  className="send"
                  onClick={onGenerate}
                  disabled={busy || !prompt.trim() || !selected}
                  aria-label="提交"
                >
                  {busy ? (
                    <svg className="spin" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
                  ) : (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
                  )}
                </button>
              </div>

              <div className="composer-foot">
                <AnimatePresence mode="wait">
                  {msg.text && (
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
                  )}
                </AnimatePresence>
              </div>
            </div>
          </footer>
        </main>
      ) : page === 'explore' ? (
        <main className="main explore-page">
          <header className="topbar">
            <div className="crumb">
              <span className="crumb-k">探索</span>
              <span className="crumb-d">{allKindModels.length} 个模型</span>
            </div>
            <div className="spacer" />
            <div className="src-chip">{modelSource || '—'}</div>
          </header>
          <section className="explore-content">
            <div className="explore-hero">
              <h1>模型库</h1>
              <p>浏览上游全部可用模型，108 个版本 · 涵盖图、视频、音频。</p>
            </div>
            <div className="explore-filter">
              <input
                type="search"
                placeholder="按名字 / 提供商过滤"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            </div>
            <div className="explore-grid">
              {(filter ? models : allKindModels.slice(0, 36)).map((m) => (
                <div
                  key={keyOf(m)}
                  className={`explore-card ${
                    selected && keyOf(selected) === keyOf(m) ? 'selected' : ''
                  }`}
                  onClick={() => {
                    setSelectedKey(keyOf(m))
                    setKind(m.kind === 'audio' ? 'image' : m.kind || 'image')
                    setPage('chat')
                  }}
                >
                  <div className="explore-card-kind">{m.kind}</div>
                  <div className="explore-card-title">{m.id}</div>
                  <div className="explore-card-ver">{m.version}</div>
                  <div className="explore-card-foot">
                    {m.provider && (
                      <span className="explore-card-provider">{m.provider}</span>
                    )}
                    {m.release && (
                      <span
                        className={`explore-card-rel ${
                          m.release === 'alpha' ? 'alpha' : ''
                        }`}
                      >
                        {m.release}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>
        </main>
      ) : (
        <main className="main logs-page">
          <header className="topbar">
            <div className="crumb">
              <span className="crumb-k">日志</span>
              <span className="crumb-d">{logs.length} 条 · SQLite</span>
            </div>
            <div className="spacer" />
            <button type="button" className="ghost-btn" onClick={loadLogs}>
              刷新
            </button>
            <button
              type="button"
              className="ghost-btn danger"
              onClick={onClearLogs}
              disabled={!logs.length}
              title="清空全部日志"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
              清空
            </button>
          </header>
          <section className="logs-content">
            <div className="logs">
              {!logs.length && <div className="empty">暂无日志</div>}
              {logs.map((log) => (
                <article className="log" key={log.id}>
                  <div className="log-head">
                    <span className="log-id">#{log.id}</span>
                    <span className="log-phase">{log.phase || '-'}</span>
                    <span className="log-method">{log.method}</span>
                    <span className="log-url">{log.url}</span>
                    <span className="log-ts">
                      {log.created_at
                        ? new Date(log.created_at * 1000).toLocaleString()
                        : ''}
                    </span>
                    <span
                      className={`log-status ${
                        log.status_code >= 400 ? 'err' : ''
                      }`}
                    >
                      {log.status_code ?? '-'}
                    </span>
                  </div>
                  {(log.request_body || log.response_body) && (
                    <div className="log-body">
                      {log.request_body
                        ? `REQ ${String(log.request_body).slice(0, 600)}\n`
                        : ''}
                      {log.response_body
                        ? `RES ${String(log.response_body).slice(0, 600)}`
                        : ''}
                    </div>
                  )}
                </article>
              ))}
            </div>
          </section>
        </main>
      )}
    </div>
  )
}

function NavItem({ page, id, setPage, icon, label, hint }) {
  const active = page === id
  return (
    <button
      type="button"
      className={`nav-item ${active ? 'active' : ''}`}
      onClick={() => setPage(id)}
    >
      <span className="nav-icon">{icon}</span>
      <span className="nav-label">{label}</span>
      {hint && <span className="nav-hint">{hint}</span>}
    </button>
  )
}

function MessageBubble({ job }) {
  const p = job.params || {}
  const outs = (job.outputs || job.files || []).filter((o) => o.url)
  const isActive = ['queued', 'running'].includes(job.status)

  const statusLabel = {
    queued: '排队中',
    running: '生成中',
    succeeded: '完成',
    failed: '失败',
  }[job.status] || job.status

  return (
    <>
      <motion.div
        className="bubble user"
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.18, ease: [0.2, 0.7, 0.2, 1] }}
      >
        <div className="bubble-body">
          <div className="bubble-prompt">{p.prompt || job.prompt}</div>
          <div className="bubble-meta">
            <span className="meta-chip">
              {p.model || job.model}:{p.model_version || job.model_version}
            </span>
            {p.size && <span className="meta-chip">{p.size}</span>}
            {p.duration && <span className="meta-chip">{p.duration}s</span>}
            {p.aspect_ratio && (
              <span className="meta-chip">{p.aspect_ratio}</span>
            )}
            <span className="meta-time">{fmtTime(job.created_at)}</span>
          </div>
        </div>
      </motion.div>

      <motion.div
        className="bubble assistant"
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.18, delay: 0.04, ease: [0.2, 0.7, 0.2, 1] }}
      >
        <div className="avatar">ff</div>
        <div className="bubble-body">
          <div className="bubble-status">
            <span className={`status-dot ${job.status || 'queued'}`} />
            <span>{statusLabel}</span>
            {job.message && (
              <span className="status-msg">· {job.message}</span>
            )}
          </div>

          {isActive && (
            <div className="bar">
              <motion.i
                initial={{ width: 0 }}
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

function OutputCard({ url, type }) {
  const isVideo = type === 'video' || /\.(mp4|webm|mov)(\?|$)/i.test(url)
  const isImage = type === 'image' || /\.(png|jpe?g|webp|gif)(\?|$)/i.test(url)

  async function copy() {
    try {
      await navigator.clipboard.writeText(url)
    } catch {
      /* ignore */
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
          <span>FILE</span>
        )}
      </a>
      <div className="output-actions">
        <a className="action" href={url} target="_blank" rel="noreferrer">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3"/></svg>
          打开
        </a>
        <button type="button" className="action" onClick={copy}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          复制链接
        </button>
      </div>
    </div>
  )
}

function ChipSelect({ label, value, options, onChange, suffix }) {
  return (
    <div className="chip-control">
      <span className="chip-control-label">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
            {suffix || ''}
          </option>
        ))}
      </select>
    </div>
  )
}

function ChipNumber({ label, value, min, max, onChange }) {
  return (
    <div className="chip-control">
      <span className="chip-control-label">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  )
}

function ChipToggle({ label, on, onChange }) {
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