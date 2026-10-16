<script setup>
import { reactive, ref } from 'vue'

const emit = defineEmits(['send'])
const sending = ref(false)

const form = reactive({
  platform: 'wechat',
  chat_id: '张三',
  sender: '张三',
  content: '',
})

async function submit() {
  if (!form.content.trim() || sending.value) return
  sending.value = true
  try {
    await emit('send', { ...form, content: form.content.trim() })
    form.content = ''
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="card">
    <h3>模拟新消息（触发自动回复）</h3>
    <div class="grid">
      <label>
        平台
        <select v-model="form.platform">
          <option value="wechat">微信</option>
          <option value="dingtalk">钉钉</option>
        </select>
      </label>
      <label>
        会话 ID
        <input v-model="form.chat_id" placeholder="如：张三 / 项目群" />
      </label>
    </div>
    <label class="mt">
      发送人
      <input v-model="form.sender" placeholder="对方昵称" />
    </label>
    <label class="mt">
      消息内容
      <textarea v-model="form.content" rows="3" placeholder="输入对方发来的消息，发送后自动触发回复生成" @keydown.enter.exact.prevent="submit"></textarea>
    </label>
    <button class="primary mt full" :disabled="sending || !form.content.trim()" @click="submit">
      {{ sending ? '处理中...' : '📨 注入消息' }}
    </button>
    <p class="tip">
      提示：消息会通过 Mock 连接器注入，Agent 监听到后自动生成回复（默认进入“待审核”，可在消息流中批准/驳回）。
    </p>
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-dim); }
.mt { margin-top: 8px; }
.full { width: 100%; margin-top: 10px; }
.tip { margin-top: 10px; font-size: 11px; color: var(--text-dim); line-height: 1.6; }
</style>
