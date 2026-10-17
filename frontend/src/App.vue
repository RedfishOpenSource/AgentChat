<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, connectWS } from './api'
import StatusPanel from './components/StatusPanel.vue'
import SimulatorForm from './components/SimulatorForm.vue'
import ContactPanel from './components/ContactPanel.vue'
import KnowledgePanel from './components/KnowledgePanel.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import ChatStream from './components/ChatStream.vue'
import DirectChat from './components/DirectChat.vue'

const status = ref(null)
const config = ref(null)  // GET /api/config（设置面板用）
const messages = ref([])
const wsConnected = ref(false)
const toastMsg = ref('')
const activeTab = ref('stream') // stream | direct
let ws = null
let toastTimer = null

const engineLabel = computed(() => {
  if (!status.value) return '...'
  return status.value.engine === 'llm' ? `LLM(${status.value.engine})` : 'Mock 规则引擎'
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
  try { config.value = await api.getConfig() } catch { /* ignore */ }
}

async function refreshMessages() {
  try {
    const data = await api.messages({ limit: 500 })
    messages.value = data.messages.filter((m) => m.platform !== 'web')
  } catch { /* ignore */ }
}

async function onSimulate(form) {
  try {
    const data = await api.simulate(form)
    upsert(data.message)
    upsert(data.reply)
  } catch (e) {
    toast(`模拟失败: ${e.message}`)
  }
}

async function onAction({ id, action, content }) {
  try {
    const data = action === 'approve' ? await api.approve(id, content) : await api.reject(id)
    upsert(data.message)
  } catch (e) {
    toast(`操作失败: ${e.message}`)
  }
}

async function onConnector({ platform, action }) {
  try {
    const data = await api.connector(platform, action)
    if (status.value) status.value.connectors[platform] = data.connector
  } catch (e) {
    toast(`操作失败: ${e.message}`)
  }
}

async function onSaveContacts(list) {
  try {
    const data = await api.setContacts(list)
    if (status.value) status.value.allowed_contacts = data.allowed_contacts
    toast(`白名单已保存（${data.allowed_contacts.length || '允许全部'}）`)
  } catch (e) {
    toast(`保存失败: ${e.message}`)
  }
}

async function onToggleAutoSend() {
  try {
    const target = !(status.value?.auto_send)
    const data = await api.updateConfig({ auto_send: target })
    if (status.value) status.value.auto_send = data.config.auto_send
    toast(target ? "⚠️ 已开启自动发送：新消息将直接回复" : "已切换为人工确认模式")
  } catch (e) {
    toast(`切换失败: ${e.message}`)
  }
}

async function onSaveConfig(updates) {
  try {
    const data = await api.updateConfig(updates)
    toast(`配置已生效: ${data.changed.join('、') || '无变更'}`)
    await refreshStatus()
  } catch (e) {
    toast(`保存失败: ${e.message}`)
  }
}

onMounted(async () => {
  await Promise.all([refreshStatus(), refreshMessages(), refreshConfig()])
  ws = connectWS(handleEvent, (ok) => (wsConnected.value = ok))
  // 状态兜底轮询
  setInterval(() => { refreshStatus(); refreshConfig() }, 10000)
})

onUnmounted(() => ws && ws.close())
</script>

<template>
  <div class="layout">
    <div v-if="alerts.length" class="alert-banner">
      <span v-for="a in alerts" :key="a.key">⚠️ {{ a.message }}　</span>
    </div>
    <header class="topbar">
      <div class="brand">
        <span class="logo">🤖</span>
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

    <aside class="sidebar">
      <StatusPanel :status="status" @action="onConnector" />
      <ContactPanel :contacts="status?.allowed_contacts || []" @save="onSaveContacts" />
      <KnowledgePanel @notify="toast" />
      <SimulatorForm @send="onSimulate" />
      <SettingsPanel :config="config" @save="onSaveConfig" />
    </aside>

    <main class="main">
      <div class="tabs">
        <button :class="{ active: activeTab === 'stream' }" @click="activeTab = 'stream'">
          消息监听 ({{ messages.length }})
        </button>
        <button :class="{ active: activeTab === 'direct' }" @click="activeTab = 'direct'">
          对话测试
        </button>
      </div>
      <ChatStream v-show="activeTab === 'stream'" :messages="messages" @action="onAction" />
      <DirectChat v-show="activeTab === 'direct'" @error="toast" />
    </main>

    <transition name="fade">
      <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>
    </transition>
  </div>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  grid-template-rows: auto 60px 1fr;
  height: 100vh;
}
.alert-banner {
  grid-column: 1 / 3;
  grid-row: 1;
  background: #3a1512;
  border-bottom: 1px solid var(--red);
  color: #ffb4ae;
  padding: 8px 20px;
  font-size: 13px;
}
.topbar {
  grid-column: 1 / 3;
  grid-row: 2;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: center; gap: 10px; }
.logo { font-size: 26px; }
.title { font-size: 17px; font-weight: 700; }
.subtitle { font-size: 11px; color: var(--text-dim); }
.chips { display: flex; gap: 8px; }
.chip {
  font-size: 12px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 12px;
  color: var(--text-dim);
}
.chip.on { color: var(--green); border-color: rgba(42, 174, 103, 0.4); }
.chip.ws.on { color: var(--accent); }
.autosend-toggle {
  font-size: 12px;
  padding: 4px 12px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--text-dim);
}
.autosend-toggle.on {
  background: rgba(229, 83, 75, 0.15);
  border-color: var(--red);
  color: var(--red);
}
.sidebar {
  grid-row: 3;
  padding: 14px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  border-right: 1px solid var(--border);
}
.main { grid-row: 3; display: flex; flex-direction: column; overflow: hidden; }
.tabs {
  display: flex;
  gap: 6px;
  padding: 10px 16px 0;
}
.tabs button {
  border-radius: 8px 8px 0 0;
  border-bottom: none;
  background: transparent;
  color: var(--text-dim);
}
.tabs button.active {
  background: var(--panel);
  color: var(--text);
}
.toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  background: #2a1f1f;
  border: 1px solid var(--red);
  color: #ffb4ae;
  padding: 10px 18px;
  border-radius: 8px;
  z-index: 100;
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.3s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
