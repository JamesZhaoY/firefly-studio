// util: formatters and shared constants. No JSX, no state.

export function keyOf(m) {
  return `${m.id}@@${m.version}`
}

export function fmtTime(ts) {
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

export function fmtTimestamp(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleString()
}

export const STATUS_LABEL = {
  queued: '排队中',
  running: '生成中',
  succeeded: '完成',
  failed: '失败',
}

export function statusLabel(status) {
  return STATUS_LABEL[status] || status
}
