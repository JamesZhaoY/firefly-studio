// logs: call log list. Pure presentation, state from App.

import { fmtTimestamp } from './util.js'

export function LogRow({ log }) {
  const isError = log.status_code >= 400
  const statusTone = isError ? 'is-error' : log.status_code >= 200 ? 'is-ok' : 'is-neutral'
  return (
    <article className={`log ${statusTone}`}>
      <div className="log-head">
        <span className="log-status-mark" aria-hidden="true" />
        <span className="log-id">#{log.id}</span>
        <span className="log-phase">{log.phase || '-'}</span>
        <span className="log-method">{log.method}</span>
        <span className="log-url">{log.url}</span>
        <span className="log-ts">{fmtTimestamp(log.created_at)}</span>
        <span className={`log-status ${isError ? 'err' : ''}`}>
          {log.status_code ?? '-'}
        </span>
      </div>
      {(log.request_body || log.response_body) && (
        <details className="log-details">
          <summary>查看载荷</summary>
          <pre className="log-body">
{log.request_body
  ? `请求 ${String(log.request_body).slice(0, 600)}\n`
  : ''}{log.response_body
  ? `响应 ${String(log.response_body).slice(0, 600)}`
  : ''}
          </pre>
        </details>
      )}
    </article>
  )
}

export function LogsPage({ logs }) {
  return (
    <section className="logs-content">
      <div className="logs">
        {!logs.length && <div className="empty">暂无日志</div>}
        {logs.map((log) => <LogRow key={log.id} log={log} />)}
      </div>
    </section>
  )
}
