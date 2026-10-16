<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  contacts: { type: Array, default: () => [] },
})
const emit = defineEmits(['save'])

const text = ref('')
const saving = ref(false)

watch(
  () => props.contacts,
  (v) => {
    text.value = (v || []).join('\n')
  },
  { immediate: true }
)

function save() {
  if (saving.value) return
  saving.value = true
  try {
    const list = text.value
      .split('\n')
      .map((x) => x.trim())
      .filter(Boolean)
    emit('save', list)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="card">
    <h3>联系人白名单</h3>
    <p class="hint">
      只有名单内的会话会生成草稿；留空 = 允许全部。每行一个（微信为昵称/备注，钉钉为会话 ID）。
    </p>
    <textarea v-model="text" rows="4" placeholder="张三&#10;项目交流群"></textarea>
    <button class="primary full mt" :disabled="saving" @click="save">
      {{ saving ? '保存中...' : '保存白名单（立即生效）' }}
    </button>
  </div>
</template>

<style scoped>
textarea { width: 100%; resize: vertical; font-family: inherit; }
.full { width: 100%; margin-top: 8px; }
.hint { font-size: 11px; color: var(--text-dim); margin-bottom: 8px; line-height: 1.6; }
</style>
