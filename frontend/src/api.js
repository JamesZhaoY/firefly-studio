const BASE = import.meta.env.VITE_API_BASE || ''

async function request(path, opts = {}) {
  const { headers = {}, ...rest } = opts
  const url = BASE
    ? `${BASE.replace(/\/$/, '')}${path}`
    : path
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...headers },
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
}
