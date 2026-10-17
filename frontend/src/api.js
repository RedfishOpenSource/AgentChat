const BASE = '/api'

async function req(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = await res.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  status: () => req('/status'),
  config: () => req('/config'),
  updateConfig: (body) => req('/config', { method: 'PUT', body: JSON.stringify(body) }),
  messages: (params = {}) => req(`/messages?${new URLSearchParams(params)}`),
  replies: (params = {}) => req(`/replies?${new URLSearchParams(params)}`),
  approve: (id, content) => req(`/replies/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify(content !== undefined && content !== null ? { content } : {}),
  }),
  reject: (id) => req(`/replies/${id}/reject`, { method: 'POST' }),
  simulate: (body) => req('/simulate', { method: 'POST', body: JSON.stringify(body) }),
  chat: (body) => req('/chat', { method: 'POST', body: JSON.stringify(body) }),
  connector: (platform, action) => req(`/connectors/${platform}/${action}`, { method: 'POST' }),
  setContacts: (list) => req('/contacts', { method: 'PUT', body: JSON.stringify({ allowed_contacts: list }) }),
  knowledge: () => req('/knowledge'),
  rescanKnowledge: () => req('/knowledge/scan', { method: 'POST' }),
  listDirs: (path = '') => req(`/fs/dirs?path=${encodeURIComponent(path)}`),
}

export function connectWS(onEvent, onState) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${proto}://${location.host}/ws`)
  ws.onopen = () => onState && onState(true)
  ws.onclose = () => onState && onState(false)
  ws.onerror = () => onState && onState(false)
  ws.onmessage = (e) => {
    try { onEvent(JSON.parse(e.data)) } catch { /* ignore */ }
  }
  return ws
}
