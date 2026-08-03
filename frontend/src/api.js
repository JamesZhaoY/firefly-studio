const BASE = import.meta.env.VITE_API_BASE || ''
const ADMIN_API_KEY = import.meta.env.VITE_ADMIN_API_KEY || ''

function authHeaders(headers = {}) {
  return ADMIN_API_KEY ? { 'X-Admin-Key': ADMIN_API_KEY, ...headers } : headers
}

async function request(path, opts = {}) {
  const { headers = {}, ...rest } = opts
  const url = BASE
    ? `${BASE.replace(/\/$/, '')}${path}`
    : path
  const res = await fetch(url, {
    headers: authHeaders({ 'Content-Type': 'application/json', ...headers }),
    ...rest,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const err = new Error(data.error || res.statusText || 'request failed')
    err.data = data
    err.status = res.status
    throw err
  }
  return data
}

export const api = {
  health: () => request('/api/health'),
  models: (params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/api/models${qs ? `?${qs}` : ''}`)
  },
  generate: (body) =>
    request('/api/generate', { method: 'POST', body: JSON.stringify(body) }),
  jobs: (limit = 40) => request(`/api/jobs?limit=${limit}`),
  job: (id) => request(`/api/jobs/${id}`),
  deleteJob: (id) => request(`/api/jobs/${id}`, { method: 'DELETE' }),
  logs: (params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request(`/api/logs${qs ? `?${qs}` : ''}`)
  },
  clearLogs: () => request('/api/logs', { method: 'DELETE' }),
  clearChat: () => request('/api/chat/clear', { method: 'POST' }),
  videoGenerate: (body) =>
    request('/api/video/generate', { method: 'POST', body: JSON.stringify(body) }),
  videoJob: (id) => request(`/api/video/${id}`),
  voices: () => request('/api/voices'),
  llmModels: () => request('/api/llm-models'),

  accounts: () => request('/api/accounts'),
  uploadAccount: ({ label, tokenFile, cookieFile }) => {
    const fd = new FormData()
    fd.append('label', label)
    if (tokenFile) fd.append('token_file', tokenFile)
    if (cookieFile) fd.append('cookie_file', cookieFile)
    return fetch(
      (BASE ? BASE.replace(/\/$/, '') : '') + '/api/accounts/upload',
      { method: 'POST', headers: authHeaders(), body: fd },
    ).then(async (r) => {
      const data = await r.json().catch(() => ({}))
      if (!r.ok) {
        const e = new Error(data.error || r.statusText)
        e.data = data
        throw e
      }
      return data
    })
  },
  deleteAccount: (id) => request(`/api/accounts/${id}`, { method: 'DELETE' }),
  patchAccount: (id, body) =>
    request(`/api/accounts/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  refreshAccount: (id) =>
    request(`/api/accounts/${id}/refresh`, { method: 'POST' }),
}
