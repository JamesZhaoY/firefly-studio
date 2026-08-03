// accounts: 多账号连接池管理页.
// 上传 / 列表 / 启停 / 删除 / 强制 IMS 刷新.

import { useState, useRef } from 'react'
import { GhostButton } from './primitives.jsx'

function fmtTs(unix, seconds = false) {
  if (!unix) return '-'
  return new Date(unix * 1000).toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
    ...(seconds ? { second: '2-digit' } : {}),
    hour12: false,
  })
}

function fmtLeft(sec) {
  if (!sec) return ''
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.round(sec / 60)}m`
  return `${Math.round(sec / 3600)}h`
}

function fmtCredits(credits) {
  if (!credits) return '读取中…'
  if (credits.error) return credits.error
  if (credits.available == null) return '额度暂不可读取'
  return `${credits.available} / ${credits.total ?? '?'} 积分`
}

function AccountRow({ acct, busy, onToggle, onDelete, onRefresh, onRename }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(acct.label)

  const tone = acct.disabled
    ? 'disabled'
    : !acct.healthy
      ? 'cooling'
      : 'ok'

  return (
    <article className={`account-row tone-${tone}`}>
      <div className="account-row-head">
        <span className={`account-dot ${tone}`} aria-hidden="true" />
        {editing ? (
          <form
            className="account-rename"
            onSubmit={async (e) => {
              e.preventDefault()
              const v = draft.trim()
              if (!v || v === acct.label) { setEditing(false); return }
              await onRename(acct.id, v)
              setEditing(false)
            }}
          >
            <input
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Escape') setEditing(false) }}
              maxLength={64}
            />
            <button type="submit" className="ghost-btn small">保存</button>
            <button type="button" className="ghost-btn small" onClick={() => setEditing(false)}>取消</button>
          </form>
        ) : (
          <span className="account-label" title={acct.token_preview}>
            {acct.label}
            <button
              type="button"
              className="account-edit"
              onClick={() => { setDraft(acct.label); setEditing(true) }}
              title="重命名"
              aria-label="重命名"
            >✎</button>
          </span>
        )}
        <span className={`account-state tone-${tone}`}>
          {acct.disabled
            ? '已停用'
            : !acct.healthy && acct.cooldown_left_sec > 0
              ? `冷却 ${fmtLeft(acct.cooldown_left_sec)}`
              : acct.expired
                ? '已过期'
                : '就绪'}
        </span>
      </div>
      <div className="account-meta">
        <span>client: <code>{acct.client_id || '-'}</code></span>
        <span>来源: {acct.source}</span>
        <span>添加: {fmtTs(acct.added_at)}</span>
        {acct.expires_at > 0 && <span>过期: {fmtTs(acct.expires_at)}</span>}
        <span>成功/失败: {acct.stats.succeeded} / {acct.stats.failed}</span>
        <span className={`account-credits ${acct.credits?.error ? 'err' : ''}`} title={acct.credits?.error || ''}>
          额度: {fmtCredits(acct.credits)}
        </span>
        {acct.credits?.updated_at && <span>额度查询: {fmtTs(acct.credits.updated_at, true)}</span>}
        {acct.credits?.available_until && <span>额度过期: {new Date(acct.credits.available_until).toLocaleString('zh-CN', {
          month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
        })}</span>}
        {acct.last_error && (
          <span className="account-err" title={acct.last_error}>
            错误: {acct.last_error.slice(0, 40)}
          </span>
        )}
      </div>
      <div className="account-actions">
        <GhostButton
          disabled={busy || acct.disabled}
          onClick={() => onRefresh(acct.id)}
          title="用本账号 cookie 强制刷 IMS token"
        >刷新 token</GhostButton>
        <GhostButton onClick={() => onToggle(acct.id, !acct.disabled)}>
          {acct.disabled ? '启用' : '停用'}
        </GhostButton>
        <GhostButton danger onClick={() => onDelete(acct.id)}>删除</GhostButton>
      </div>
    </article>
  )
}

export function AccountsPage({
  accounts,
  pool,
  busy,
  onUpload,
  onToggle,
  onDelete,
  onRefresh,
  onRename,
  showMsg,
}) {
  const [label, setLabel] = useState('')
  const [tokenFile, setTokenFile] = useState(null)
  const [cookieFile, setCookieFile] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const tokenRef = useRef(null)
  const cookieRef = useRef(null)

  async function submit(e) {
    e.preventDefault()
    if (!label.trim()) return showMsg('请输入账号名', 'err')
    if (!tokenFile && !cookieFile) return showMsg('请至少选择 token 或 cookie 文件', 'err')
    setSubmitting(true)
    try {
      await onUpload({ label: label.trim(), tokenFile, cookieFile })
      setLabel('')
      setTokenFile(null)
      setCookieFile(null)
      if (tokenRef.current) tokenRef.current.value = ''
      if (cookieRef.current) cookieRef.current.value = ''
      showMsg('账号已添加', 'ok')
    } catch (err) {
      showMsg(err.message || String(err), 'err')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="accounts-content">
      <form className="accounts-upload" onSubmit={submit}>
        <div className="accounts-upload-head">添加账号</div>
        <div className="accounts-upload-row">
          <label className="accounts-field">
            <span>账号名</span>
            <input
              type="text"
              placeholder="例如 alice"
              value={label}
              maxLength={64}
              onChange={(e) => setLabel(e.target.value)}
              required
            />
          </label>
          <label className="accounts-field">
            <span>token 文件 (可选)</span>
            <input
              ref={tokenRef}
              type="file"
              accept=".json,application/json"
              onChange={(e) => setTokenFile(e.target.files?.[0] || null)}
            />
          </label>
          <label className="accounts-field">
            <span>cookie 文件 (可选，可单独上传)</span>
            <input
              ref={cookieRef}
              type="file"
              accept=".json,application/json"
              onChange={(e) => setCookieFile(e.target.files?.[0] || null)}
            />
          </label>
          <GhostButton type="submit" disabled={submitting}>
            {submitting ? '上传中…' : '添加'}
          </GhostButton>
        </div>
        <div className="accounts-upload-hint">
          可上传 <code>current_token.json</code>、<code>storage.json</code> 或两者一起。
          只传 cookie 时服务端会立即换取 IMS token；全池失效时会自动用已上传 cookie 恢复后再切换。
        </div>
      </form>

      {pool && (
        <div className="accounts-summary">
          <span>共 <b>{pool.size}</b> 个</span>
          <span>· 可用 <b>{pool.available}</b></span>
          {pool.cooling_down > 0 && <span className="warn">· 冷却 <b>{pool.cooling_down}</b></span>}
          {pool.disabled > 0 && <span className="warn">· 停用 <b>{pool.disabled}</b></span>}
          {pool.expired > 0 && <span className="warn">· 过期 <b>{pool.expired}</b></span>}
          <span className="muted">· 策略: {pool.strategy}</span>
        </div>
      )}

      <div className="accounts-list">
        {!accounts.length && (
          <div className="empty">
            还没有账号. 上传 <code>current_token.json</code> 或 <code>storage.json</code> 后, 任务会在多个账号间自动轮询.
            凭证不再从本地 JSON 文件读取, 全部由本页面统一管理.
          </div>
        )}
        {accounts.map((a) => (
          <AccountRow
            key={a.id}
            acct={a}
            busy={busy}
            onToggle={onToggle}
            onDelete={onDelete}
            onRefresh={onRefresh}
            onRename={onRename}
          />
        ))}
      </div>
    </section>
  )
}
