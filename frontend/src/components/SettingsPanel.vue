<script setup>
import { reactive, ref, watch } from 'vue'

const props = defineProps({
  config: { type: Object, default: null }, // GET /api/config 的返回
})
const emit = defineEmits(['save'])

const saving = ref(false)
const form = reactive({
  engine: 'mock',
  num_candidates: 1,
  group_trigger: 'at-mention',
  non_text_action: 'placeholder',
  debounce_seconds: 3.0,
})

watch(
  () => props.config,
  (c) => {
    if (!c) return
    form.engine = c.engine || 'mock'
    form.num_candidates = c.llm?.num_candidates ?? 1
    form.group_trigger = c.group_trigger || 'at-mention'
    form.non_text_action = c.non_text_action || 'placeholder'
    form.debounce_seconds = c.debounce_seconds ?? 3.0
  },
  { immediate: true }
)

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    await emit('save', {
      engine: form.engine,
      num_candidates: Number(form.num_candidates),
      group_trigger: form.group_trigger,
      non_text_action: form.non_text_action,
      debounce_seconds: Number(form.debounce_seconds),
      persist: true,
    })
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="card">
    <h3>设置（热更新）</h3>
    <details>
      <summary>展开设置项</summary>
      <div class="fields">
        <label>
          回复引擎
          <select v-model="form.engine">
            <option value="mock">Mock 规则（无需 Key）</option>
            <option value="llm">LLM（DeepSeek 等）</option>
          </select>
        </label>
        <label>
          候选数量（1-5）
          <input v-model.number="form.num_candidates" type="number" min="1" max="5" />
        </label>
        <label>
          群聊触发
          <select v-model="form.group_trigger">
            <option value="at-mention">仅 @我</option>
            <option value="all">全部消息</option>
            <option value="listen-only">只监听不回复</option>
          </select>
        </label>
        <label>
          非文本消息
          <select v-model="form.non_text_action">
            <option value="placeholder">生成提示草稿</option>
            <option value="skip">直接忽略</option>
          </select>
        </label>
        <label>
          连发合并（秒）
          <input v-model.number="form.debounce_seconds" type="number" min="0" step="0.5" />
        </label>
        <button class="primary full" :disabled="saving" @click="save">
          {{ saving ? '应用中...' : '保存并立即生效（写回配置文件）' }}
        </button>
        <p class="hint">白名单、告警渠道等请使用各自面板；连接器模式修改需重启。</p>
      </div>
    </details>
  </div>
</template>

<style scoped>
details summary { cursor: pointer; color: var(--text-dim); font-size: 12px; }
.fields { margin-top: 10px; display: flex; flex-direction: column; gap: 8px; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-dim); }
.full { width: 100%; margin-top: 4px; }
.hint { font-size: 11px; color: var(--text-dim); line-height: 1.6; margin: 0; }
</style>
