<script setup>
import { nextTick, ref } from 'vue'
import { api } from '../api'

const emit = defineEmits(['error'])

const items = ref([]) // { role: 'user' | 'assistant', content, err? }
const input = ref('')
const sending = ref(false)
const listEl = ref(null)

async function scrollBottom() {
  await nextTick()
  if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  input.value = ''
  items.value.push({ role: 'user', content: text })
  await scrollBottom()
  sending.value = true
  try {
    const data = await api.chat({ content: text })
    items.value.push({ role: 'assistant', content: data.reply.content })
  } catch (e) {
    emit('error', `发送失败: ${e.message}`)
    items.value.push({ role: 'assistant', content: `⚠️ 生成失败: ${e.message}` })
  } finally {
    sending.value = false
    await scrollBottom()
  }
}
</script>

<template>
  <div class="direct-wrap">
    <div ref="listEl" class="list">
      <div v-if="!items.length" class="empty">
        <p>直接与回复生成器对话（不经过微信/钉钉连接器）</p>
        <p class="hint">用于验证提示词、RAG、JEV 等生成策略，或当前配置的 LLM 引擎。</p>
      </div>
      <div v-for="(m, i) in items" :key="i" class="row" :class="m.role">
        <div class="bubble">{{ m.content }}</div>
      </div>
      <div v-if="sending" class="row assistant"><div class="bubble dim">生成中...</div></div>
    </div>
    <div class="input-bar">
      <textarea
        v-model="input"
        rows="1"
        placeholder="输入消息，Enter 发送"
        @keydown.enter.exact.prevent="send"
      ></textarea>
      <button class="primary" :disabled="sending || !input.trim()" @click="send">发送</button>
    </div>
  </div>
</template>

<style scoped>
.direct-wrap { display: flex; flex-direction: column; flex: 1; overflow: hidden; padding: 0 16px 16px; }
.list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding: 8px 2px; }
.empty { margin: auto; text-align: center; color: var(--text-dim); }
.empty .hint { font-size: 12px; margin-top: 6px; }
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.assistant { justify-content: flex-start; }
.bubble {
  max-width: 70%;
  padding: 9px 13px;
  border-radius: 10px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.row.user .bubble { background: var(--accent); color: #fff; border-bottom-right-radius: 3px; }
.row.assistant .bubble { background: var(--panel); border: 1px solid var(--border); border-bottom-left-radius: 3px; }
.bubble.dim { color: var(--text-dim); }
.input-bar { display: flex; gap: 8px; padding-top: 10px; border-top: 1px solid var(--border); }
.input-bar textarea { resize: none; }
.input-bar button { white-space: nowrap; }
</style>
