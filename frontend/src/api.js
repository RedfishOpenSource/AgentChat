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
  agents: () => req('/agents'),
  agentProfiles: () => req('/agent-profiles'),
  saveAgentProfile: (body) => req('/agent-profiles', { method: 'POST', body: JSON.stringify(body) }),
  debugAgent: (id, sessionId, content) => req(`/agent-profiles/${encodeURIComponent(id)}/chat`, {
    method: 'POST', body: JSON.stringify({ session_id: sessionId, content }),
  }),
  deleteAgentProfile: (id) => req(`/agent-profiles/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  openMcpConfig: (workspace) => req('/agent-mcp/open', { method: 'POST', body: JSON.stringify({ workspace }) }),
  resetSession: (platform, chatId) => req('/agent-sessions/reset', {
    method: 'POST', body: JSON.stringify({ platform, chat_id: chatId }),
  }),
  contactChat: (platform, chatId, content) => req('/agent-sessions/chat', {
    method: 'POST', body: JSON.stringify({ platform, chat_id: chatId, content }),
  }),
  regenerate: (messageId, extra) => req(`/messages/${encodeURIComponent(messageId)}/regenerate`, {
    method: 'POST', body: JSON.stringify({ extra }),
  }),
  assignAgent: (platform, chatId, agentId) => req('/agent-assignments', {
    method: 'PUT', body: JSON.stringify({ platform, chat_id: chatId, agent_id: agentId || '' }),
  }),
  contactConfigs: (platform) => req(`/contact-configs${platform ? `?platform=${platform}` : ''}`),
  saveContactConfig: (body) => req('/contact-configs', { method: 'PUT', body: JSON.stringify(body) }),
  deleteContactConfig: (platform, chatId) => req(`/contact-configs?platform=${encodeURIComponent(platform)}&chat_id=${encodeURIComponent(chatId)}`, { method: 'DELETE' }),
  config: () => req('/config'),
  updateConfig: (body) => req('/config', { method: 'PUT', body: JSON.stringify(body) }),
  messages: (params = {}) => req(`/messages?${new URLSearchParams(params)}`),
  audit: (limit = 100) => req(`/audit?limit=${limit}`),
  replies: (params = {}) => req(`/replies?${new URLSearchParams(params)}`),
  approve: (id, content, confirmUnknown = false) => req(`/replies/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify({ ...(content !== undefined && content !== null ? { content } : {}), confirm_unknown: confirmUnknown }),
  }),
  selectCandidate: (id, index) => req(`/replies/${id}/select`, {
    method: 'POST', body: JSON.stringify({ index }),
  }),
  reject: (id) => req(`/replies/${id}/reject`, { method: 'POST' }),
  simulate: (body) => req('/simulate', { method: 'POST', body: JSON.stringify(body) }),
  chat: (body) => req('/chat', { method: 'POST', body: JSON.stringify(body) }),
  connector: (platform, action) => req(`/connectors/${platform}/${action}`, { method: 'POST' }),
  setContacts: (platform, list) => req(`/contacts?platform=${platform}`, { method: 'PUT', body: JSON.stringify({ allowed_contacts: list }) }),
  contactSuggestions: (platform) => req(`/contacts/suggestions?platform=${platform}`),
  knowledge: (platform) => req(`/knowledge${platform ? `?platform=${platform}` : ''}`),
  rescanKnowledge: (platform) => req(`/knowledge/scan${platform ? `?platform=${platform}` : ''}`, { method: 'POST' }),
  pickDirectory: (path = '') => req('/fs/pick-dir', {
    method: 'POST', body: JSON.stringify({ path }),
  }),
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
