// App: state container + routing. All UI lives in components/.
// Motion is isolated in chat.jsx (Section 3.A).

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import { Sidebar, Topbar } from './components/chrome.jsx'
import { Thread, Composer } from './components/chat.jsx'
import { ExplorePage } from './components/explore.jsx'
import { LogsPage } from './components/logs.jsx'
import { AccountsPage } from './components/accounts.jsx'
import { GhostButton } from './components/primitives.jsx'
import VideoPanel from './components/video.jsx'
import { keyOf } from './components/util.js'
import './App.css'

export default function App() {
  const [page, setPage] = useState('chat')
  const [kind, setKind] = useState('image')
  const [presets, setPresets] = useState({ image: [], video: [], audio: [] })
  const [modelInfo, setModelInfo] = useState('')
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
  const [credits, setCredits] = useState(null)
  const [jobs, setJobs] = useState([])
  const [logs, setLogs] = useState([])
  const [paramsOpen, setParamsOpen] = useState(false)

  // ── accounts pool ──
  const [accounts, setAccounts] = useState([])
  const [poolSummary, setPoolSummary] = useState(null)
  const [accountBusy, setAccountBusy] = useState(false)

  // ── 一键成片 slide-over state ──
  const [videoOpen, setVideoOpen] = useState(false)
  const [videoPhase, setVideoPhase] = useState('form') // form | progress | result
  const [videoJob, setVideoJob] = useState(null)        // {id, status, progress, message, shots, finalVideoUrl, manifestUrl, ...}
  const [videoSubmitting, setVideoSubmitting] = useState(false)
  const [videoSubmitError, setVideoSubmitError] = useState('')
  const [voices, setVoices] = useState([])
  const [llmModels, setLlmModels] = useState([])
  const [focusJobId, setFocusJobId] = useState('')
  const videoTriggerRef = useRef(null)
  const pollRef = useRef(null)

  // 最近一条 video_pipeline 任务（按 created_at 倒序取第一条）
  const latestVideoJobId = useMemo(() => {
    const vp = jobs.filter((j) => j.kind === 'video_pipeline')
    return vp[0]?.id || ''
  }, [jobs])

  const allKindModels = presets[kind] || []
  const catalogModels = useMemo(
    () => Object.entries(presets).flatMap(([modelKind, models]) =>
      models.map((model) => ({ ...model, kind: modelKind })),
    ),
    [presets],
  )
  const filtered = useMemo(() => {
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

  const showMsg = useCallback((text, type = '') => setMsg({ text, type }), [])

  const loadHealth = useCallback(async () => {
    try {
      const data = await api.health()
      setAuth(data.auth || null)
      setCredits(data.credits || null)
    } catch {
      setAuth(null)
      setCredits(null)
    }
  }, [])

  const loadModels = useCallback(async (force = false) => {
    try {
      const data = await api.models(force ? { refresh: '1' } : {})
      const next = {
        image: data.presets?.image || [],
        video: data.presets?.video || [],
        audio: data.presets?.audio || [],
      }
      setPresets(next)
      const c = data.counts || {}
      const total = data.total || 0
      setModelInfo(`模型 ${total}（图 ${c.image || 0} / 视频 ${c.video || 0}）`)
    } catch (e) {
      showMsg(`加载失败：${e.message}`, 'err')
    }
  }, [showMsg])

  const loadJobs = useCallback(async () => {
    try { setJobs((await api.jobs(50)).jobs || []) }
    catch (e) { showMsg(e.message, 'err') }
  }, [showMsg])

  const loadLogs = useCallback(async () => {
    try { setLogs((await api.logs({ limit: 80 })).logs || []) }
    catch (e) { showMsg(e.message, 'err') }
  }, [showMsg])

  const loadAccounts = useCallback(async () => {
    try {
      const data = await api.accounts()
      setAccounts(data.accounts || [])
      setPoolSummary(data.pool || null)
    } catch {
      // 没启动 pool 也允许 UI 退化, 不刷错误消息
      setAccounts([])
      setPoolSummary(null)
    }
  }, [])

  const loadVoices = useCallback(async () => {
    try {
      const data = await api.voices()
      const list = Array.isArray(data.voices) ? data.voices : []
      // 归一化字段名给 panel 用
      setVoices(list.map((v) => ({
        id: v.id || v.ShortName || '',
        name: v.name || v.FriendlyName || '',
        gender: v.gender || v.Gender || '',
        locale: v.locale || v.Locale || '',
      })).filter((v) => v.id))
    } catch { /* keep empty -> panel uses fallback */ }
  }, [])

  const loadLlmModels = useCallback(async () => {
    try {
      const data = await api.llmModels()
      setLlmModels(Array.isArray(data.models) ? data.models : [])
    } catch {
      setLlmModels([])
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

  async function onUploadAccount({ label, tokenFile, cookieFile }) {
    setAccountBusy(true)
    try {
      await api.uploadAccount({ label, tokenFile, cookieFile })
      await loadAccounts()
    } finally {
      setAccountBusy(false)
    }
  }

  async function onToggleAccount(id, disabled) {
    setAccountBusy(true)
    try {
      await api.patchAccount(id, { disabled })
      await loadAccounts()
      showMsg(disabled ? '已停用' : '已启用', 'ok')
    } catch (e) {
      showMsg(e.message, 'err')
    } finally {
      setAccountBusy(false)
    }
  }

  async function onRenameAccount(id, newLabel) {
    setAccountBusy(true)
    try {
      await api.patchAccount(id, { label: newLabel })
      await loadAccounts()
      showMsg('已重命名', 'ok')
    } catch (e) {
      showMsg(e.message, 'err')
    } finally {
      setAccountBusy(false)
    }
  }

async function onDeleteAccount(id) {
    if (!window.confirm('删除该账号? 该账号的文件会从 data/accounts/ 删除.')) return
    setAccountBusy(true)
    try {
      await api.deleteAccount(id)
      await loadAccounts()
      showMsg('已删除', 'ok')
    } catch (e) {
      showMsg(e.message, 'err')
    } finally {
      setAccountBusy(false)
    }
  }

  async function onRefreshAccount(id) {
    setAccountBusy(true)
    try {
      const data = await api.refreshAccount(id)
      if (data.ok) {
        showMsg('IMS 刷新成功', 'ok')
      } else {
        showMsg(data.error || '刷新失败', 'err')
      }
      await loadAccounts()
    } catch (e) {
      showMsg(e.message, 'err')
      await loadAccounts()
    } finally {
      setAccountBusy(false)
    }
  }

  // ── 一键成片：open / submit / poll / reset ──
  async function openVideoPanel(jobId) {
    setVideoSubmitError('')
    setVideoOpen(true)
    const targetId = jobId || latestVideoJobId
    // 已有当前 videoJob 且匹配目标 ID → 沿用旧逻辑
    if (videoJob && videoJob.id === targetId) {
      setVideoPhase(
        videoJob.status !== 'succeeded' && videoJob.status !== 'failed'
          ? 'progress'
          : 'result',
      )
      return
    }
    // 没指定 ID 且 jobs 列表里也没有 → 显示空表单
    if (!targetId) {
      setVideoPhase('form')
      return
    }
    try {
      const data = await api.videoJob(targetId)
      const j = data?.job || {}
      const finalPath = j.final_video_path || ''
      const finalVideoUrl = finalPath
        ? (finalPath.includes('/outputs/') ? '/outputs/' + finalPath.split('/outputs/', 2)[1] : '')
        : ''
      const manifestPath = j.manifest_path || ''
      const manifestUrl = manifestPath
        ? (manifestPath.includes('/outputs/') ? '/outputs/' + manifestPath.split('/outputs/', 2)[1] : '')
        : ''
      const shots = (j.shots || []).map((s) => ({
        index: s.index,
        label: s.label || `第 ${s.index} 镜`,
        visual_prompt: s.visual_prompt || '',
        narration: s.narration || '',
        imageUrl: s.image_url || '',
        videoUrl: s.video_url || '',
        status: s.status || 'queued',
        stage: s.stage || '',
        error: s.error || '',
      }))
      const restored = {
        id: targetId,
        status: j.status || 'running',
        progress: j.progress || 0,
        message: j.message || '',
        shots,
        finalVideoUrl,
        manifestUrl,
        usedFfmpeg: j.used_ffmpeg,
        ffprobeDuration: j.ffprobe_duration_total || 0,
      }
      setVideoJob(restored)
      const isTerminal = j.status === 'succeeded' || j.status === 'failed'
      setVideoPhase(isTerminal ? 'result' : 'progress')
      if (!isTerminal) startVideoPoll(targetId)
    } catch {
      setVideoPhase('form')
    }
  }

  function closeVideoPanel() {
    setVideoOpen(false)
    window.setTimeout(() => videoTriggerRef.current?.focus(), 0)
  }

  function resetVideoPanel() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    setVideoJob(null)
    setVideoPhase('form')
    setVideoSubmitError('')
    setVideoSubmitting(false)
  }

  async function onVideoSubmit(payload) {
    // 「再生成一条」会进这里：先重置到 form 再开 panel
    if (videoPhase === 'result') {
      resetVideoPanel()
      setVideoOpen(true)
      return
    }
    setVideoSubmitting(true)
    setVideoSubmitError('')
    try {
      const data = await api.videoGenerate(payload)
      const jobId = data?.job_id
      if (!jobId) throw new Error('后端未返回 job_id')
      // 立即切到 progress，给一个乐观的初始 job
      setVideoJob({
        id: jobId,
        status: 'queued',
        progress: 0,
        message: '排队中',
        shots: [],
        finalVideoUrl: '',
        manifestUrl: '',
      })
      setVideoPhase('progress')
      startVideoPoll(jobId)
      await loadJobs() // 同步到主对话列表
    } catch (e) {
      setVideoSubmitError(e?.message || String(e))
    } finally {
      setVideoSubmitting(false)
    }
  }

  function startVideoPoll(jobId) {
    if (pollRef.current) clearInterval(pollRef.current)
    const tick = async () => {
      try {
        const data = await api.videoJob(jobId)
        const j = data?.job || {}
        const finalPath = j.final_video_path || ''
        const finalVideoUrl = finalPath
          ? (finalPath.includes('/outputs/') ? '/outputs/' + finalPath.split('/outputs/', 2)[1] : '')
          : ''
        const manifestPath = j.manifest_path || ''
        const manifestUrl = manifestPath
          ? (manifestPath.includes('/outputs/') ? '/outputs/' + manifestPath.split('/outputs/', 2)[1] : '')
          : ''
        const shots = (j.shots || []).map((s) => ({
          index: s.index,
          label: s.label || `第 ${s.index} 镜`,
          visual_prompt: s.visual_prompt || '',
          narration: s.narration || '',
          imageUrl: s.image_url || '',
          videoUrl: s.video_url || '',
          status: s.status || 'queued',
          stage: s.stage || '',
          error: s.error || '',
        }))
        setVideoJob({
          id: jobId,
          status: j.status || 'running',
          progress: j.progress || 0,
          message: j.message || '',
          shots,
          finalVideoUrl,
          manifestUrl,
          usedFfmpeg: j.used_ffmpeg,
          ffprobeDuration: j.ffprobe_duration_total || 0,
        })
        if (j.status === 'succeeded' || j.status === 'failed') {
          if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
          setVideoPhase('result')
          // 同步主列表 job 状态
          loadJobs()
        }
      } catch { /* swallow; next tick retries */ }
    }
    tick()
    pollRef.current = setInterval(tick, 5000)
  }

  // 卸载时清理 poll
  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current)
  }, [])

  useEffect(() => {
    const onOpen = (e) => { openVideoPanel(e.detail || '') }
    window.addEventListener('open-video-job', onOpen)
    return () => window.removeEventListener('open-video-job', onOpen)
  }, [latestVideoJobId, videoJob])

  // bootstrap
  useEffect(() => {
    loadHealth()
    loadModels(false)
    loadJobs()
    loadVoices()
    loadLlmModels()
    loadAccounts()
    const t = setInterval(loadHealth, 30000)
    return () => clearInterval(t)
  }, [loadHealth, loadModels, loadJobs, loadVoices, loadLlmModels, loadAccounts])

  // refresh accounts page when opened
  useEffect(() => {
    if (page === 'accounts') loadAccounts()
  }, [page, loadAccounts])

  // poll active jobs
  useEffect(() => {
    const active = jobs.some((j) => ['queued', 'running'].includes(j.status))
    if (!active) return undefined
    const t = setInterval(loadJobs, 2500)
    return () => clearInterval(t)
  }, [jobs, loadJobs])

  // refresh logs when navigating in
  useEffect(() => {
    if (page === 'logs') loadLogs()
  }, [page, loadLogs])

  // auto-pick a sensible default model
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

  // sync per-kind defaults when selection / kind changes
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
        : ['auto']
      setAspect(selected.default_aspect_ratio || aspects[0] || 'auto')
      setAudio(selected.audio !== false)
    }
    setN(1)
  }, [selected, kind])

  // hotkeys
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
  }, [busy, selected, prompt, kind, n, size, detail, duration, aspect, audio, seeds])

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
      // 后端会基于同一份 discovery 能力再次校验并补全真实尺寸。
      body.size = sizeStr || 'auto'
    }
    setBusy(true)
    showMsg('提交中...')
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

  function onPickFromExplore(m) {
    setSelectedKey(keyOf(m))
    setKind(m.kind === 'audio' ? 'image' : m.kind || 'image')
    setPage('chat')
    showMsg(`已选择 ${m.id}:${m.version}`, 'ok')
  }

  function onPickFromPicker(k) {
    setSelectedKey(k)
  }

  function onSelectJob(jobId) {
    const job = jobs.find((j) => j.id === jobId)
    if (job?.kind === 'video_pipeline') { openVideoPanel(jobId); return }
    setPage('chat')
    setFocusJobId(jobId)
  }

  return (
    <div className="app">
      <Sidebar
        page={page}
        setPage={setPage}
        jobs={jobs}
        logs={logs}
        accounts={accounts}
        auth={auth}
        credits={credits}
        modelInfo={modelInfo}
        onRefreshModels={() => loadModels(true)}
        onSelectJob={onSelectJob}
      />

      {page === 'chat' && (
        <main className="main">
          <Topbar
            title="对话"
            count={`${jobs.length} 个任务`}
            right={
              <>
                <GhostButton ref={videoTriggerRef} onClick={openVideoPanel} title="一键成片（视频生成流程）">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="2"
                       strokeLinecap="round" strokeLinejoin="round">
                    <rect x="2" y="6" width="14" height="12" rx="2" />
                    <path d="M16 10l6-3v10l-6-3z" />
                  </svg>
                  一键成片
                </GhostButton>
                <GhostButton danger disabled={!jobs.length} onClick={onClearChat} title="清空对话">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="2"
                       strokeLinecap="round" strokeLinejoin="round">
                    <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                  </svg>
                  清空
                </GhostButton>
              </>
            }
          />

          <Thread
            jobs={jobs}
            busy={busy}
            onPickSuggestion={setPrompt}
            focusJobId={focusJobId}
            onFocused={() => setFocusJobId('')}
          />

          <Composer
            kind={kind} setKind={setKind}
            selected={selected}
            paramsOpen={paramsOpen} setParamsOpen={setParamsOpen}
            size={size} detail={detail}
            duration={duration} aspect={aspect} audio={audio} seeds={seeds}
            prompt={prompt} setPrompt={setPrompt}
            busy={busy} onGenerate={onGenerate} msg={msg}
            allKindModels={allKindModels}
            filtered={filtered}
            total={allKindModels.length}
            filter={filter} setFilter={setFilter}
            selectedKey={selectedKey} onPickModel={onPickFromPicker}
            n={n}
            setSize={setSize} setDetail={setDetail} setN={setN}
            setDuration={setDuration} setAspect={setAspect} setAudio={setAudio} setSeeds={setSeeds}
          />
        </main>
      )}

      {page === 'explore' && (
        <main className="main">
          <Topbar
            title="探索"
            count={`${catalogModels.length} 个模型`}
          />
          <ExplorePage
            models={catalogModels}
            totalCount={catalogModels.length}
            filter={filter}
            setFilter={setFilter}
            selectedKey={selectedKey}
            onPick={onPickFromExplore}
            jobs={jobs}
          />
        </main>
      )}

      {page === 'logs' && (
        <main className="main">
          <Topbar
            title="日志"
            count={`${logs.length} 条 · SQLite`}
            right={
              <>
                <GhostButton onClick={loadLogs}>刷新</GhostButton>
                <GhostButton danger disabled={!logs.length} onClick={onClearLogs} title="清空全部日志">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="2"
                       strokeLinecap="round" strokeLinejoin="round">
                    <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                  </svg>
                  清空
                </GhostButton>
              </>
            }
          />
          <LogsPage logs={logs} />
        </main>
      )}

      {page === 'accounts' && (
        <main className="main">
          <Topbar
            title="账号池"
            count={poolSummary
              ? `${poolSummary.available} / ${poolSummary.size} 可用`
              : `${accounts.length} 个账号`}
            right={
              <GhostButton onClick={loadAccounts} title="刷新">刷新</GhostButton>
            }
          />
          <AccountsPage
            accounts={accounts}
            pool={poolSummary}
            busy={accountBusy}
            onUpload={onUploadAccount}
            onToggle={onToggleAccount}
            onDelete={onDeleteAccount}
            onRefresh={onRefreshAccount}
            onRename={onRenameAccount}
            showMsg={showMsg}
          />
        </main>
      )}

      <VideoPanel
        open={videoOpen}
        onClose={closeVideoPanel}
        phase={videoPhase}
        job={videoJob}
        voices={voices}
        llmModels={llmModels}
        videoModels={presets.video}
        onSubmit={onVideoSubmit}
        submitting={videoSubmitting}
        submitError={videoSubmitError}
      />
    </div>
  )
}
