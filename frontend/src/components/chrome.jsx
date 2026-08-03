// chrome: sidebar (brand + nav + recent + auth) and topbar.
// All components are pure presentation. State owned by App.

function BrandIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M13 2 L3 14 h7 l-1 8 10-12 h-7 z" />
    </svg>
  )
}

function NavIcon({ id }) {
  switch (id) {
    case 'chat':
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      )
    case 'explore':
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="9" />
          <path d="m21 21-4.3-4.3" />
          <path d="M11 11a2 2 0 1 0 4 0 2 2 0 0 0-4 0z" opacity=".4" />
        </svg>
      )
    case 'accounts':
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="9" cy="8" r="3" />
          <path d="M3 20a6 6 0 0 1 12 0" />
          <circle cx="17" cy="9" r="2.5" />
          <path d="M14 20a5 5 0 0 1 7-4.5" />
        </svg>
      )
    case 'logs':
    default:
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 12h6m-3-3h6m-3 6h6m4 0h3M3 6h18M3 18h18" />
        </svg>
      )
  }
}

export function NavItem({ page, id, setPage, label, hint }) {
  const active = page === id
  return (
    <button
      type="button"
      className={`nav-item ${active ? 'active' : ''}`}
      onClick={() => setPage(id)}
      aria-current={active ? 'page' : undefined}
    >
      <span className="nav-icon"><NavIcon id={id} /></span>
      <span className="nav-label">{label}</span>
      {hint != null && hint !== '' && <span className="nav-hint">{hint}</span>}
    </button>
  )
}

export function Brand() {
  return (
    <div className="brand">
      <div className="brand-mark"><BrandIcon /></div>
      <div className="brand-text">
        <span className="brand-name">Firefly Studio</span>
        <span className="brand-sub">本地创作台</span>
      </div>
    </div>
  )
}

export function RecentItem({ job, onSelect }) {
  const p = job.params || {}
  const text = (p.prompt || job.prompt || '').slice(0, 22)
  const truncated = (p.prompt || '').length > 22 || (job.prompt || '').length > 22
  return (
    <button type="button" className="recent-item" title={p.prompt || job.prompt} onClick={() => onSelect(job.id)}>
      <span className={`dot ${job.status}`} />
      <span className="recent-text">{text}{truncated ? '...' : ''}</span>
    </button>
  )
}

export function AuthBadge({ auth, modelInfo, onRefresh }) {
  const cls = !auth
    ? 'err'
    : auth.token_ok || auth.can_ims_refresh ? 'ok' : 'err'
  const poolMode = auth?.mode === 'pool'
  const text = !auth
    ? '离线'
    : poolMode
      ? `账号池 ${auth.pool?.available ?? 0}/${auth.pool?.size ?? 0}`
      : auth.token_ok || auth.can_ims_refresh
        ? auth.client_id || '已就绪'
        : '未登录'

  return (
    <div className="sidebar-foot">
      <div className={`auth ${cls}`} title={text}>
        <span className="dot" />
        <div className="auth-text">
          <span className="auth-label">{text}</span>
        </div>
      </div>
      {modelInfo && <span className="model-info">{modelInfo}</span>}
      <button type="button" className="ghost-btn" onClick={onRefresh}>
        刷新模型
      </button>
    </div>
  )
}

export function Sidebar({ page, setPage, jobs, logs, accounts, auth, modelInfo, onRefreshModels, onSelectJob }) {
  const accountHint = !accounts
    ? ''
    : accounts.length
      ? `${accounts.filter((a) => a.healthy && !a.disabled).length}/${accounts.length}`
      : ''
  return (
    <aside className="sidebar">
      <Brand />
      <nav className="nav" aria-label="主导航">
        <NavItem page={page} id="chat" setPage={setPage} label="对话" hint="Cmd K" />
        <NavItem page={page} id="explore" setPage={setPage} label="探索" hint="Cmd E" />
        <NavItem page={page} id="accounts" setPage={setPage} label="账号池" hint={accountHint} />
        <NavItem
          page={page}
          id="logs"
          setPage={setPage}
          label="日志"
          hint={logs.length ? String(logs.length) : ''}
        />
      </nav>

      <div className="recent">
        {jobs.slice(0, 6).map((j) => <RecentItem key={j.id} job={j} onSelect={onSelectJob} />)}
        {!jobs.length && <div className="recent-empty">还没有任务</div>}
      </div>

      <AuthBadge auth={auth} modelInfo={modelInfo} onRefresh={onRefreshModels} />
    </aside>
  )
}

export function Topbar({ title, count, modelSource, right }) {
  return (
    <header className="topbar">
      <div className="crumb">
        <span className="crumb-k">{title}</span>
        {count != null && <span className="crumb-d">{count}</span>}
      </div>
      <div className="spacer" />
      {modelSource != null && (
        <span className="src-chip">{modelSource || '未加载'}</span>
      )}
      {right}
    </header>
  )
}
