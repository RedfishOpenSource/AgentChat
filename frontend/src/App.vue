<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, connectWS } from './api'
import StatusPanel from './components/StatusPanel.vue'
import ContactPanel from './components/ContactPanel.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import ChatStream from './components/ChatStream.vue'
import DirectChat from './components/DirectChat.vue'

const status = ref(null)
const config = ref(null)  // GET /api/config（设置面板用）
const messages = ref([])
const auditRows = ref([])
const messageOffset = ref(0)
const messagePageSize = 500
const loadingOlder = ref(false)
const savingSettings = ref(false)
const hasOlderMessages = ref(true)
let disposed = false
const wsConnected = ref(false)
const toastMsg = ref('')
const activeTab = ref('stream') // stream | direct
// 顶部告警横幅（来自 /api/status 的 alerts 字段；后端未返回时为空数组）
const alerts = computed(() => status.value?.alerts || [])
const pendingCount = computed(() => messages.value.filter((m) => ['pending', 'blocked', 'failed', 'unknown'].includes(m.status)).length)
let ws = null
let toastTimer = null
let statusTimer = null
let reconnectTimer = null
let reconnectDelay = 1000

const engineLabel = computed(() => {
  if (!status.value) return '...'
  return status.value.engine === 'llm' ? 'LLM（OpenAI 兼容）'
    : status.value.engine === 'acp' ? 'Agent（ACP）' : 'Mock 规则引擎'
})

function toast(text) {
  toastMsg.value = text
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => (toastMsg.value = ''), 3000)
}

function upsert(msg) {
  if (!msg || msg.platform === 'web') return
  const i = messages.value.findIndex((m) => m.id === msg.id)
  if (i >= 0) messages.value[i] = msg
  else messages.value.push(msg)
  messages.value.sort((a, b) => a.ts - b.ts)
}

function handleEvent(ev) {
  if (ev.type === 'message' || ev.type === 'reply' || ev.type === 'reply_updated' || ev.type === 'message_updated') {
    upsert(ev.message)
  } else if (ev.type === 'status') {
    if (status.value) status.value.connectors = ev.connectors
  } else if (ev.type === 'error') {
    toast(ev.detail || '发生错误')
  }
}

async function refreshStatus() {
  try { status.value = await api.status() } catch { /* 后端未启动时忽略 */ }
}

async function refreshConfig() {
  try { config.value = await api.config() } catch (e) { toast(`配置加载失败: ${e.message}`) }
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
    if (action === 'approve') await refreshAudit()
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

async function refreshAudit() {
  try { auditRows.value = (await api.audit()).audit } catch (e) { toast(`审计记录加载失败: ${e.message}`) }
}

async function onConnector({ platform, action }) {
  try {
    const data = await api.connector(platform, action)
    if (status.value) status.value.connectors[platform] = data.connector
  } catch (e) {
    toast(`操作失败: ${e.message}`)
  }
}

async function onSaveContacts({ platform, list }) {
  try {
    const data = await api.setContacts(platform, list)
    if (status.value) {
      status.value.allowed_contacts = { ...(status.value.allowed_contacts || {}), [platform]: data.allowed_contacts }
    }
    const label = platform === 'wechat' ? '微信' : '钉钉'
    const warn = data.persisted === false ? '；⚠️ 写入配置文件失败，重启后失效' : ''
    toast(`${label}白名单已保存（${data.allowed_contacts.length || '允许全部'}）${warn}`)
  } catch (e) {
    toast(`保存失败: ${e.message}`)
  }
}

async function onToggleAutoSend() {
  try {
    const target = !(status.value?.auto_send)
    if (target && !window.confirm('开启后，符合策略的普通回复会直接发送到真实会话。确认开启？')) return
    const data = await api.updateConfig({ auto_send: target, persist: true })
    if (status.value) status.value.auto_send = data.config.auto_send
    toast(target ? "⚠️ 已开启自动发送：新消息将直接回复" : "已切换为人工确认模式")
  } catch (e) {
    toast(`切换失败: ${e.message}`)
  }
}

async function onSaveConfig(updates) {
  savingSettings.value = true
  try {
    const data = await api.updateConfig(updates)
    toast(`配置已生效: ${data.changed.join('、') || '无变更'}`)
    await refreshStatus()
  } catch (e) {
    toast(`保存失败: ${e.message}`)
  } finally {
    savingSettings.value = false
  }
}

onMounted(async () => {
  await Promise.all([refreshStatus(), refreshMessages(), refreshConfig()])
  connectRealtime()
  statusTimer = setInterval(() => { refreshStatus(); refreshConfig() }, 10000)
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
  <div class="layout">
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
        <span class="chip">引擎: {{ engineLabel }}</span>
        <span class="chip" :class="{ on: status?.jev?.enabled }">
          JEV: {{ status?.jev?.enabled ? `${status.jev.provider} 已启用` : '未启用' }}
        </span>
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
      <StatusPanel :status="status" @action="onConnector" />
      <details class="sidebar-group" open>
        <summary>微信 <span>联系人白名单</span></summary>
        <ContactPanel platform="wechat" :contacts="status?.allowed_contacts?.wechat || []" @save="onSaveContacts" />
      </details>
      <details class="sidebar-group">
        <summary>钉钉 <span>联系人白名单</span></summary>
        <ContactPanel platform="dingtalk" :contacts="status?.allowed_contacts?.dingtalk || []" @save="onSaveContacts" />
      </details>
      <SettingsPanel :config="config" :saving="savingSettings" @save="onSaveConfig" />
      <p class="sidebar-note">白名单弹窗选择；AI 接入支持 OpenAI 兼容接口与外部 Agent；默认人工审核，发送前请核对目标会话</p>
    </aside>

    <main class="main">
      <div class="workspace-heading">
        <div>
          <div class="eyebrow">CHAT OPERATIONS</div>
          <h1>{{ activeTab === 'audit' ? '操作审计' : activeTab === 'direct' ? '生成器测试' : '消息审核' }}</h1>
        </div>
        <div v-if="activeTab === 'stream'" class="queue-summary">
          <span class="queue-dot"></span>
          <strong>{{ pendingCount }}</strong><span>条待处理</span>
        </div>
      </div>
      <div class="tabs">
        <button :class="{ active: activeTab === 'stream' }" @click="activeTab = 'stream'">消息流 ({{ messages.length }})</button>
        <button :class="{ active: activeTab === 'direct' }" @click="activeTab = 'direct'">对话测试</button>
        <button :class="{ active: activeTab === 'audit' }" @click="activeTab = 'audit'; refreshAudit()">审计记录</button>
      </div>
      <ChatStream v-show="activeTab === 'stream'" :messages="messages" :loading-older="loadingOlder" :has-older="hasOlderMessages" @load-older="loadOlderMessages" @action="onAction" @select-candidate="onSelectCandidate" />
      <DirectChat v-show="activeTab === 'direct'" @error="toast" />
      <div v-if="activeTab === 'audit'" class="audit-view">
        <div v-for="row in auditRows" :key="row.id" class="audit-row">
          <span>{{ new Date(row.ts).toLocaleString() }}</span><strong>{{ row.action }}</strong>
          <span>{{ row.message_id }}</span><pre>{{ row.detail }}</pre>
        </div>
        <p v-if="!auditRows.length" class="audit-empty">暂无审计记录</p>
      </div>
    </main>

    <transition name="fade">
      <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>
    </transition>
  </div>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: 306px minmax(0, 1fr);
  grid-template-rows: auto 66px minmax(0, 1fr);
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
  grid-row: 2;
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
.chips { display: flex; align-items: center; justify-content: flex-end; gap: 7px; flex-wrap: wrap; }
.chip {
  padding: 5px 9px;
  border: 1px solid var(--border-soft);
  border-radius: 4px;
  color: var(--text-dim);
  font-size: 11px;
  white-space: nowrap;
}
.chip.on { color: var(--green); border-color: rgba(85, 201, 138, .35); background: rgba(85, 201, 138, .06); }
.chip.ws.on { color: var(--accent-strong); border-color: rgba(85, 184, 200, .34); }
.autosend-toggle { min-height: 30px; padding: 5px 10px; border-radius: 4px; font-size: 11px; }
.autosend-toggle.on { background: rgba(233, 121, 112, .1); border-color: rgba(233, 121, 112, .45); color: var(--red); }
.sidebar {
  grid-row: 3;
  min-height: 0;
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-right: 1px solid var(--border-soft);
  background: rgba(13, 18, 23, .72);
}
.logo span { color: var(--accent-strong); }
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
.main { grid-row: 3; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.workspace-heading { display: flex; align-items: center; justify-content: space-between; padding: 19px 20px 10px; }
.eyebrow { margin-bottom: 4px; color: var(--accent); font-size: 9px; font-weight: 700; letter-spacing: 1.4px; }
.workspace-heading h1 { color: #e7eff2; font-size: 17px; line-height: 1.2; font-weight: 650; }
.queue-summary { display: flex; align-items: center; gap: 7px; color: var(--text-dim); font-size: 11px; }
.queue-summary strong { color: var(--orange); font-size: 16px; font-variant-numeric: tabular-nums; }
.queue-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--orange); box-shadow: 0 0 12px rgba(230, 173, 98, .35); }
.audit-view { flex: 1; overflow: auto; margin: 12px 18px 18px; padding: 16px 18px; border: 1px solid var(--border-soft); border-radius: 7px; background: rgba(20, 26, 33, .72); }
.audit-row { display: grid; grid-template-columns: 180px 90px minmax(100px, 1fr); gap: 10px; padding: 11px 0; border-bottom: 1px solid var(--border-soft); color: var(--text-dim); font-size: 12px; }
.audit-row strong { color: var(--accent-strong); font-weight: 600; }
.audit-row pre { grid-column: 1 / -1; white-space: pre-wrap; color: var(--text-muted); font-size: 11px; }
.audit-empty { color: var(--text-dim); padding: 24px; text-align: center; }
.tabs { display: flex; flex-shrink: 0; gap: 2px; padding: 2px 18px 0; border-bottom: 1px solid var(--border-soft); }
.tabs button { min-height: 38px; padding: 8px 14px; border: 0; border-radius: 5px 5px 0 0; background: transparent; color: var(--text-dim); font-size: 12px; }
.tabs button:hover:not(:disabled) { background: rgba(85, 184, 200, .07); color: var(--text); }
.tabs button.active { background: var(--panel); color: var(--accent-strong); box-shadow: inset 0 -2px 0 var(--accent); }
.toast { position: fixed; left: 50%; bottom: 24px; z-index: 100; transform: translateX(-50%); max-width: min(520px, calc(100vw - 32px)); padding: 10px 16px; border: 1px solid rgba(85, 184, 200, .42); border-radius: 5px; background: #17262d; color: #d8f7fa; box-shadow: var(--shadow); font-size: 12px; }
.fade-enter-active, .fade-leave-active { transition: opacity .2s, transform .2s; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translate(-50%, 8px); }
@media (max-width: 850px) {
  .layout { display: flex; flex-direction: column; height: auto; min-height: 100vh; }
  .topbar { align-items: flex-start; flex-direction: column; gap: 12px; padding: 14px 16px; }
  .chips { justify-content: flex-start; }
  .sidebar { order: 2; overflow: visible; border-right: 0; border-top: 1px solid var(--border-soft); }
  .main { order: 1; height: 80dvh; min-height: 520px; flex: none; }
  .audit-row { grid-template-columns: 1fr 1fr; }
  .audit-row span:nth-child(3) { grid-column: 1 / -1; }
}
</style>
