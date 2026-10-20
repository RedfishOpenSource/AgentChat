<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  profile: { type: Object, required: true },
  messages: { type: Array, default: () => [] },
  processing: { type: Object, default: () => ({}) },
  connected: { type: Boolean, default: true },
})
const emit = defineEmits(['close', 'message'])
const storageKey = `chatagent:debug:${props.profile.id}`
function readSession() {
  try { return sessionStorage.getItem(storageKey) || crypto.randomUUID() } catch { return crypto.randomUUID() }
}
const sessionId = ref(readSession())
function saveSession() { try { sessionStorage.setItem(storageKey, sessionId.value) } catch {} }
saveSession()
const chatId = computed(() => `debug:${props.profile.id}:${sessionId.value}`)
const text = ref('')
const sending = ref(false)
const error = ref('')
const history = ref([])
const bodyEl = ref(null)
const now = ref(Date.now())
const ticker = setInterval(() => { now.value = Date.now() }, 1000)
onUnmounted(() => clearInterval(ticker))
const messages = computed(() => {
  const byId = new Map([...history.value, ...props.messages].filter(m => m.chat_id === chatId.value).map(m => [m.id, m]))
  return [...byId.values()].sort((a, b) => a.ts - b.ts)
})
const activeProgress = computed(() => Object.values(props.processing).find(p => p.chat_id === chatId.value && !p.finished_at))
const busy = computed(() => sending.value || !!activeProgress.value)
const phases = { preparing: '准备上下文', queued: '等待 Agent 空闲', starting: '正在连接 Agent', session: '创建调试会话', thinking: 'AI 思考中', tool: '工具执行中', writing: '正在生成回复' }
function scroll() { nextTick(() => { if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight }) }
watch(() => [messages.value.length, activeProgress.value?.updated_at], scroll)
watch(chatId, async id => {
  history.value = []
  try {
    const data = await api.messages({ platform: 'web', chat_id: id, limit: 100 })
    if (chatId.value === id) history.value = data.messages || []
  } catch (e) { if (chatId.value === id) error.value = `加载调试记录失败：${e.message}` }
}, { immediate: true })

function newSession() {
  if (busy.value) return
  sessionId.value = crypto.randomUUID()
  saveSession()
  text.value = ''
  error.value = ''
}
async function send() {
  const content = text.value.trim()
  if (!content || busy.value) return
  sending.value = true
  error.value = ''
  try {
    const data = await api.debugAgent(props.profile.id, sessionId.value, content)
    emit('message', data.message)
    if (data.reply) emit('message', data.reply)
    if (text.value.trim() === content) text.value = ''
  } catch (e) { error.value = e.message }
  finally { sending.value = false; scroll() }
}
function onKey(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send() }
}
</script>

<template>
  <Teleport to="body">
    <div class="debug-mask" @click.self="emit('close')">
      <section class="debug-dialog" role="dialog" aria-modal="true" aria-label="Agent 调试">
        <header class="debug-head">
          <strong>调试 · {{ profile.name }}</strong>
          <button :disabled="busy" @click="newSession">新建会话</button>
          <button aria-label="关闭调试" @click="emit('close')">✕</button>
        </header>
        <p class="debug-note">使用该 Agent 当前保存的配置、工作空间与 Skills。此处为独立调试对话。</p>
        <div ref="bodyEl" class="debug-history">
          <p v-if="!messages.length && !busy" class="debug-empty">输入问题，开始调试 {{ profile.name }}</p>
          <article v-for="m in messages" :key="m.id" class="debug-message" :class="m.direction">
            <strong>{{ m.direction === 'in' ? '开发者' : profile.name }}</strong>
            <div class="debug-content">{{ m.content }}</div>
            <div v-if="m.meta?.error" class="debug-error">{{ m.meta.error }}</div>
            <details v-if="m.meta?.processing?.finished_at" class="debug-record">
              <summary>执行记录 · 用时 {{ Math.max(0, Math.floor((m.meta.processing.finished_at - m.meta.processing.since) / 1000)) }} 秒</summary>
              <ol><li v-for="(step, i) in m.meta.processing.steps" :key="i">{{ step.detail || phases[step.phase] }}</li></ol>
            </details>
          </article>
          <div v-if="busy" class="debug-progress" role="status">
            <strong>{{ phases[activeProgress?.phase] || '正在等待 AI 回复' }}</strong>
            <span v-if="activeProgress"> · 已用时 {{ Math.max(0, Math.floor((now - activeProgress.since) / 1000)) }} 秒</span>
            <p>{{ connected ? activeProgress?.detail : '实时连接已断开，正在重连' }}</p>
            <details v-if="activeProgress?.steps?.length">
              <summary>查看执行进度</summary>
              <ol><li v-for="(step, i) in activeProgress.steps" :key="i">{{ step.detail || phases[step.phase] }}</li></ol>
            </details>
            <div v-if="activeProgress?.preview" class="debug-content">{{ activeProgress.preview }}</div>
          </div>
        </div>
        <p v-if="error" class="debug-error" role="alert">{{ error }}</p>
        <form class="debug-composer" @submit.prevent="send">
          <textarea v-model="text" aria-label="调试消息" rows="3" maxlength="20000" :disabled="busy" placeholder="输入问题，调试该 Agent…" @keydown="onKey"></textarea>
          <footer><span>Enter 发送 · Shift + Enter 换行</span><button class="primary" :disabled="busy || !text.trim()">{{ busy ? 'AI 处理中…' : '发送' }}</button></footer>
        </form>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.debug-mask { position: fixed; inset: 0; z-index: 90; display: flex; align-items: center; justify-content: center; padding: 16px; background: rgba(4, 7, 10, .7); }
.debug-dialog { width: min(900px, 96vw); height: min(800px, 90dvh); min-height: 350px; display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: 9px; box-shadow: var(--shadow); }
.debug-head { display: flex; align-items: center; gap: 10px; }
.debug-head strong { flex: 1; color: #e2ecef; font-size: 14px; }
.debug-head button { min-height: 28px; padding: 4px 9px; font-size: 11px; }
.debug-note { margin: 0; color: var(--text-muted); font-size: 11px; }
.debug-history { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding: 12px; border: 1px solid var(--border-soft); border-radius: 6px; background: var(--bg); }
.debug-empty { margin: auto; color: var(--text-dim); font-size: 13px; }
.debug-message { width: fit-content; max-width: 90%; padding: 10px 12px; background: var(--panel-2); border: 1px solid var(--border-soft); border-radius: 6px; }
.debug-message.in { align-self: flex-end; border-color: rgba(85, 184, 200, .3); }
.debug-message strong { font-size: 11px; color: var(--accent-strong); }
.debug-content { margin-top: 7px; white-space: pre-wrap; overflow-wrap: anywhere; color: #dce6e9; font-size: 13px; line-height: 1.65; }
.debug-progress { padding: 10px; color: var(--accent-strong); font-size: 12px; border: 1px solid var(--border-soft); border-radius: 6px; }
.debug-progress p { color: var(--text-dim); }
.debug-progress summary, .debug-record summary { cursor: pointer; font-size: 11px; }
.debug-record { margin-top: 8px; font-size: 11px; color: var(--text-dim); }
.debug-progress ol { color: var(--text-dim); font-size: 11px; }
.debug-error { margin: 0; color: var(--red); font-size: 12px; overflow-wrap: anywhere; }
.debug-composer { display: flex; flex-direction: column; gap: 8px; }
.debug-composer textarea { min-height: 70px; max-height: 160px; resize: vertical; font-family: inherit; font-size: 13px; }
.debug-composer footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.debug-composer footer span { color: var(--text-muted); font-size: 11px; }
.debug-composer button { min-height: 30px; padding: 5px 16px; font-size: 12px; }
</style>
