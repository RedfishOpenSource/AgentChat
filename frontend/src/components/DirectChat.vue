<script setup>
import { nextTick, ref } from 'vue'
import { api } from '../api'

const emit = defineEmits(['error'])

const items = ref([]) // { role: 'user' | 'assistant', content, err? }
const input = ref('')
const sending = ref(false)
const listEl = ref(null)
const platform = ref('web') // web = 全局知识库；wechat/dingtalk = 对应连接器的独立知识库

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
    const data = await api.chat({ content: text, platform: platform.value === 'web' ? null : platform.value })
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
        <div class="empty-icon" aria-hidden="true">↗</div>
        <p>先聊一句，看看 AI 如何回复</p>
        <p class="hint">在这里测试提示词和知识库，不向微信或钉钉发送消息。</p>
      </div>
      <div v-for="(m, i) in items" :key="i" class="row" :class="m.role">
        <div class="bubble">{{ m.content }}</div>
      </div>
      <div v-if="sending" class="row assistant"><div class="bubble dim">生成中...</div></div>
    </div>
    <div class="input-bar">
      <select v-model="platform" class="plat-select" title="选择本次对话使用的知识库范围">
        <option value="web">全局知识库</option>
        <option value="wechat">微信知识库</option>
        <option value="dingtalk">钉钉知识库</option>
      </select>
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
.direct-wrap { display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: hidden; margin: 16px 20px 18px; border: 1px solid var(--border-soft); border-radius: 7px; background: rgba(12, 17, 22, .52); }
.list { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; padding: 24px; }
.empty { margin: auto; max-width: 380px; padding: 24px 10px; text-align: center; color: var(--text); font-weight: 600; }
.empty-icon { display: grid; place-items: center; width: 52px; height: 52px; margin: 0 auto 20px; color: var(--accent-strong); background: rgba(85, 184, 200, .08); border: 1px solid rgba(85, 184, 200, .2); border-radius: 14px; font-size: 26px; font-weight: 400; }
.empty .hint { font-size: 12px; margin-top: 10px; color: var(--text-dim); font-weight: 400; line-height: 1.8; }
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.assistant { justify-content: flex-start; }
.bubble { max-width: 78%; padding: 12px 16px; border-radius: 8px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
.row.user .bubble { background: #1c4952; color: #effbfc; border: 1px solid #30616b; border-bottom-right-radius: 2px; }
.row.assistant .bubble { background: var(--panel); border: 1px solid var(--border); border-bottom-left-radius: 2px; }
.bubble.dim { color: var(--text-dim); }
.input-bar { display: flex; align-items: flex-end; gap: 10px; padding: 16px; border-top: 1px solid var(--border-soft); background: var(--panel); }
.plat-select { flex: 0 0 auto; width: auto; min-width: 118px; }
.input-bar textarea { flex: 1; resize: vertical; min-height: 42px; max-height: 180px; }
.input-bar button { min-height: 42px; padding-inline: 20px; white-space: nowrap; }
@media (max-width: 700px) {
  .direct-wrap { margin: 12px 10px; }
  .list { padding: 14px; }
  .bubble { max-width: 92%; }
  .input-bar { padding: 10px; }
}
</style>
