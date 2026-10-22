<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, connectWS } from './api'
import ContactPanel from './components/ContactPanel.vue'
import AgentStudio from './components/AgentStudio.vue'
import ChatStream from './components/ChatStream.vue'

const status = ref(null)
const messages = ref([])
const processing = ref({})
const debugMessages = ref([])
const messageOffset = ref(0)
const messagePageSize = 500
const loadingOlder = ref(false)
const hasOlderMessages = ref(true)
let disposed = false
const wsConnected = ref(false)
const toastMsg = ref('')
// 顶部告警横幅（来自 /api/status 的 alerts 字段；后端未返回时为空数组）
const alerts = computed(() => status.value?.alerts || [])
const engineLabel = computed(() => {
  const s = status.value
  if (!s) return '...'
  if (s.engine === 'acp') return `Agent: ${s.agent_command || 'ACP'}`
  if (s.engine === 'llm') return 'LLM（OpenAI 兼容）'
  return 'Mock 规则引擎'
})
let ws = null
let toastTimer = null
let statusTimer = null
let reconnectTimer = null
let reconnectDelay = 1000

function toast(text) {
  toastMsg.value = text
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => (toastMsg.value = ''), 3000)
}

function upsert(msg) {
  if (!msg) return
  if (msg.platform === 'web' && !msg.meta?.debug_profile_id) return
  const target = msg.platform === 'web' ? debugMessages.value : messages.value
  const i = target.findIndex((m) => m.id === msg.id)
  if (i >= 0) target[i] = msg
  else target.push(msg)
  target.sort((a, b) => a.ts - b.ts)
  if (msg.meta?.processing?.finished_at) delete processing.value[msg.id]
}

function handleEvent(ev) {
  if (ev.type === 'message' || ev.type === 'reply' || ev.type === 'reply_updated' || ev.type === 'message_updated') {
    upsert(ev.message)
  } else if (ev.type === 'hello') {
    processing.value = ev.processing || {}
  } else if (ev.type === 'processing') {
    if (ev.state?.message_id) processing.value[ev.state.message_id] = ev.state
  } else if (ev.type === 'status') {
    if (status.value) status.value.connectors = ev.connectors
  } else if (ev.type === 'chat_cleared') {
    clearChat(ev)
  } else if (ev.type === 'error') {
    toast(ev.detail || '发生错误')
  }
}

function clearChat({ platform, chat_id }) {
  messages.value = messages.value.filter((m) => !(m.platform === platform && m.chat_id === chat_id))
}

async function refreshStatus() {
  try { status.value = await api.status() } catch { /* 后端未启动时忽略 */ }
}

async function refreshMessages() {
  try {
    const data = await api.messages({ limit: messagePageSize })
    messages.value = data.messages.filter((m) => m.platform !== 'web')
    messageOffset.value = data.messages.length
    hasOlderMessages.value = data.messages.length === messagePageSize
  } catch { /* ignore */ }
}

async function loadOlderMessages() {
  if (loadingOlder.value) return
  loadingOlder.value = true
  try {
    const data = await api.messages({ limit: messagePageSize, offset: messageOffset.value })
    const older = data.messages.filter((m) => m.platform !== 'web')
    messageOffset.value += data.messages.length
    hasOlderMessages.value = data.messages.length === messagePageSize
    const byId = new Map([...older, ...messages.value].map((m) => [m.id, m]))
    messages.value = [...byId.values()].sort((a, b) => a.ts - b.ts)
  } catch (e) { toast(`历史消息加载失败: ${e.message}`) }
  finally { loadingOlder.value = false }
}

async function onAction({ id, action, content, confirmUnknown = false }) {
  try {
    const data = action === 'approve' ? await api.approve(id, content, confirmUnknown) : await api.reject(id)
    upsert(data.message)
  } catch (e) {
    toast(`操作失败: ${e.message}`)
  }
}

async function onSelectCandidate({ id, index }) {
  try {
    const data = await api.selectCandidate(id, index)
    upsert(data.message)
  } catch (e) { toast(`切换候选失败: ${e.message}`) }
}

async function onRegenerate({ id, extra }) {
  try {
    const data = await api.regenerate(id, extra)
    upsert(data.reply)
    toast('已根据补充重新生成回复')
  } catch (e) { toast(`重新生成失败: ${e.message}`) }
}

async function onToggleAutoSend() {
  try {
    const target = !(status.value?.auto_send)
    if (target && !window.confirm('开启后，符合策略的普通回复会直接发送到真实会话。确认开启？')) return
    const data = await api.updateConfig({ auto_send: target, persist: true })
    if (status.value) status.value.auto_send = data.config.auto_send
    toast(target ? '⚠️ 已开启自动发送：新消息将直接回复' : '已切换为人工确认模式')
  } catch (e) {
    toast(`切换失败: ${e.message}`)
  }
}

function onContactsSaved() {
  toast('监听联系人已保存')
  refreshStatus()
}

onMounted(async () => {
  await Promise.all([refreshStatus(), refreshMessages()])
  connectRealtime()
  statusTimer = setInterval(refreshStatus, 10000)
})

function connectRealtime() {
  if (disposed) return
  ws = connectWS(handleEvent, async (ok) => {
    if (disposed) return
    wsConnected.value = ok
    if (ok) {
      reconnectDelay = 1000
      await refreshMessages()
    } else if (!reconnectTimer) {
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        connectRealtime()
      }, reconnectDelay)
      reconnectDelay = Math.min(reconnectDelay * 2, 30000)
    }
  })
}

onUnmounted(() => {
  disposed = true
  ws?.close()
  clearInterval(statusTimer)
  clearTimeout(reconnectTimer)
  clearTimeout(toastTimer)
})
</script>

<template>
  <div class="layout" :class="{ 'has-alert': alerts.length }">
    <div v-if="alerts.length" class="alert-banner">
      <span v-for="a in alerts" :key="a.key">⚠️ {{ a.message }}　</span>
    </div>

    <header class="topbar">
      <div class="brand">
        <span class="logo" aria-hidden="true">C<span>·</span></span>
        <div>
          <div class="title">ChatAgent</div>
          <div class="subtitle">微信 / 钉钉智能回复助手</div>
        </div>
      </div>
      <div class="chips">
        <a class="guide-link" href="/guide.html" target="_blank" rel="noopener">使用说明与案例 ↗</a>
        <span class="chip">引擎: {{ engineLabel }}</span>
        <span class="chip">发送: {{ status?.auto_send ? '自动' : '人工确认' }}</span>
        <button
          class="autosend-toggle"
          :class="{ on: status?.auto_send }"
          :title="status?.auto_send ? '点击切换为人工确认' : '点击开启自动发送（谨慎）'"
          @click="onToggleAutoSend"
        >
          {{ status?.auto_send ? '⏸ 停用自动发送' : '▶ 开启自动发送' }}
        </button>
        <span class="chip ws" :class="{ on: wsConnected }">{{ wsConnected ? '● 实时连接' : '○ 未连接' }}</span>
      </div>
    </header>

    <aside class="sidebar" aria-label="工作台控制">
      <div class="sidebar-label">工作台控制 <span>WORKSPACE</span></div>
      <details class="sidebar-group">
        <summary>微信 <span>监听联系人</span></summary>
        <ContactPanel
          platform="wechat"
          :contacts="status?.allowed_contacts?.wechat || []"
          :connector="status?.connectors?.wechat"
          @saved="onContactsSaved"
          @connector-changed="refreshStatus"
          @error="toast"
        />
      </details>
      <details class="sidebar-group">
        <summary>钉钉 <span>监听联系人</span></summary>
        <ContactPanel
          platform="dingtalk"
          :contacts="status?.allowed_contacts?.dingtalk || []"
          :connector="status?.connectors?.dingtalk"
          @saved="onContactsSaved"
          @connector-changed="refreshStatus"
          @error="toast"
        />
      </details>
      <AgentStudio :debug-messages="debugMessages" :processing="processing" :connected="wsConnected" @message="upsert" @toast="toast" />
      <p class="sidebar-note">回复由各监听联系人绑定的 Agent 生成，未绑定时回退第一个已创建的 Agent（工作目录以其工作空间为准）；默认人工审核，发送前请核对目标会话</p>
    </aside>

    <main class="main">
      <ChatStream :messages="messages" :processing="processing" :connected="wsConnected" :contacts="status?.allowed_contacts || {}" @toast="toast" @message="upsert" :loading-older="loadingOlder" :has-older="hasOlderMessages" @load-older="loadOlderMessages" @action="onAction" @select-candidate="onSelectCandidate" @regenerate="onRegenerate" @cleared-chat="clearChat" />
    </main>

    <transition name="fade">
      <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>
    </transition>
  </div>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: 384px minmax(0, 1fr);
  grid-template-rows: 66px minmax(0, 1fr);
  height: 100vh;
  background: radial-gradient(circle at 80% -20%, rgba(47, 111, 122, .12), transparent 34%), var(--bg);
}
.alert-banner {
  grid-column: 1 / 3;
  grid-row: 1;
  padding: 8px 22px;
  border-bottom: 1px solid rgba(233, 121, 112, .38);
  background: rgba(100, 36, 33, .42);
  color: #f2aaa2;
  font-size: 12px;
}
.topbar {
  grid-column: 1 / 3;
  grid-row: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 0 22px;
  border-bottom: 1px solid var(--border-soft);
  background: rgba(16, 21, 27, .92);
  backdrop-filter: blur(18px);
}
.brand { display: flex; align-items: center; gap: 11px; min-width: 210px; }
.logo {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 1px solid rgba(85, 184, 200, .35);
  border-radius: 8px;
  background: rgba(85, 184, 200, .1);
  font-size: 18px;
}
.title { color: #f5fbfc; font-size: 16px; font-weight: 700; letter-spacing: .2px; }
.subtitle { margin-top: 2px; color: var(--text-dim); font-size: 11px; }
.guide-link { color: var(--accent-strong); font-size: 11px; white-space: nowrap; text-decoration: none; }
.guide-link:hover { text-decoration: underline; }
.chips { display: flex; align-items: center; justify-content: flex-end; gap: 7px; flex-wrap: wrap; }
.chip { padding: 5px 9px; border: 1px solid var(--border-soft); border-radius: 4px; color: var(--text-dim); font-size: 11px; white-space: nowrap; }
.chip.on { color: var(--green); border-color: rgba(85, 201, 138, .35); background: rgba(85, 201, 138, .06); }
.chip.ws.on { color: var(--accent-strong); border-color: rgba(85, 184, 200, .34); }
.autosend-toggle { min-height: 30px; padding: 5px 10px; border-radius: 4px; font-size: 11px; }
.autosend-toggle.on { background: rgba(233, 121, 112, .1); border-color: rgba(233, 121, 112, .45); color: var(--red); }
.sidebar {
  grid-row: 2;
  min-height: 0;
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-right: 1px solid var(--border-soft);
  background: rgba(13, 18, 23, .72);
}
.sidebar-label { display: flex; justify-content: space-between; padding: 2px 2px 4px; color: var(--text-dim); font-size: 11px; }
.sidebar-label span { color: var(--text-muted); font-size: 9px; letter-spacing: 1px; }
.sidebar-group { flex-shrink: 0; border: 1px solid var(--border-soft); border-radius: 7px; background: var(--panel); }
.sidebar-group > summary { padding: 13px 14px; color: #c8d4d9; font-size: 12px; cursor: pointer; }
.sidebar-group > summary span { float: right; color: var(--text-muted); font-size: 10px; }
.sidebar-group :deep(.card) { border: 0; border-top: 1px solid var(--border-soft); border-radius: 0 0 7px 7px; box-shadow: none; }
.sidebar-group :deep(h3) { font-size: 11px; }
.sidebar-group :deep(.card + .card) { border-top: 1px solid var(--border-soft); }
.sidebar > :deep(.card) { flex-shrink: 0; }
.sidebar-note { padding: 2px 4px 8px; color: var(--text-muted); font-size: 10px; }
.layout.has-alert { grid-template-rows: auto 66px minmax(0, 1fr); }
.layout.has-alert .topbar { grid-row: 2; }
.layout.has-alert .sidebar,
.layout.has-alert .main { grid-row: 3; }
.main { grid-row: 2; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.toast { position: fixed; left: 50%; bottom: 10px; z-index: 100; transform: translateX(-50%); max-width: min(520px, calc(100vw - 32px)); padding: 5px 12px; border: 1px solid var(--border-soft); border-radius: 4px; background: rgba(13, 18, 23, .85); color: var(--text-muted); font-size: 10px; opacity: .75; box-shadow: none; }
.fade-enter-active, .fade-leave-active { transition: opacity .2s, transform .2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translate(-50%, 8px); }
@media (max-width: 850px) {
  .layout { display: flex; flex-direction: column; height: auto; min-height: 100vh; }
  .sidebar { order: 2; overflow: visible; border-right: 0; border-top: 1px solid var(--border-soft); }
  .main { order: 1; height: 80dvh; min-height: 520px; flex: none; }
  .layout.has-alert .sidebar,
  .layout.has-alert .main { grid-row: auto; }
}
</style>
