<script setup>
import { computed, ref } from 'vue'
import { api } from '../api'

const props = defineProps({
  platform: { type: String, required: true }, // wechat | dingtalk
  contacts: { type: Array, default: () => [] },
})
const emit = defineEmits(['save'])

const PLATFORM_LABELS = { wechat: '微信', dingtalk: '钉钉' }
const label = PLATFORM_LABELS[props.platform] || props.platform
const saving = ref(false)
const showPicker = ref(false)
const loading = ref(false)
const suggestions = ref([]) // [{name, username, source}]
const checked = ref([])     // 选中的值（会话名 / ID）
const manual = ref('')
const keyword = ref('')

const filteredSuggestions = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return suggestions.value
  return suggestions.value.filter((s) =>
    (s.name || '').toLowerCase().includes(k) || (s.username || '').toLowerCase().includes(k))
})

function valueOf(s) { return s.name || s.username || '' }

function isChecked(s) { return checked.value.includes(valueOf(s)) }

function toggle(s) {
  const v = valueOf(s)
  if (!v) return
  const i = checked.value.indexOf(v)
  if (i >= 0) checked.value.splice(i, 1)
  else checked.value.push(v)
}

function addManual() {
  const v = manual.value.trim()
  if (v && !checked.value.includes(v)) checked.value.push(v)
  manual.value = ''
}

async function openPicker() {
  showPicker.value = true
  keyword.value = ''
  checked.value = [...props.contacts]
  loading.value = true
  try {
    const data = await api.contactSuggestions(props.platform)
    suggestions.value = data.suggestions || []
  } catch { suggestions.value = [] } finally { loading.value = false }
}

function removeOne(c) {
  if (saving.value) return
  save(contacts().filter((x) => x !== c))
}

function contacts() { return [...props.contacts] }

function save(list) {
  if (saving.value) return
  saving.value = true
  try { emit('save', { platform: props.platform, list: [...list] }) } finally { saving.value = false }
}

function savePicker() {
  showPicker.value = false
  save(checked.value)
}
</script>

<template>
  <div class="card">
    <h3>联系人白名单 · {{ label }}</h3>
    <p class="hint">空名单 = 允许全部会话。点按钮弹窗选择，也可手动输入。</p>
    <div class="current">
      <span v-for="c in contacts" :key="c" class="chip">
        {{ c }}
        <button class="x" :title="`移除 ${c}`" @click="removeOne(c)">×</button>
      </span>
      <span v-if="!contacts.length" class="none">允许全部</span>
    </div>
    <button class="primary full mt" :disabled="saving" @click="openPicker">👥 选择{{ label }}联系人</button>

    <div v-if="showPicker" class="mask" @click.self="showPicker = false">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-head">
          <strong>选择{{ label }}联系人</strong>
          <button class="x" @click="showPicker = false">✕</button>
        </div>
        <input v-model="keyword" class="kw" placeholder="搜索会话名 / ID" />
        <div class="sug-list">
          <div v-if="loading" class="empty">加载中…</div>
          <div v-else-if="!filteredSuggestions.length" class="empty">暂无建议（启动监听后可读取会话列表），可手动添加</div>
          <label v-for="s in filteredSuggestions" :key="(s.username || '') + '|' + (s.name || '')" class="sug">
            <input type="checkbox" :checked="isChecked(s)" @change="toggle(s)" />
            <span class="name">{{ s.name || s.username }}</span>
            <span v-if="s.username && s.username !== s.name" class="uname">{{ s.username }}</span>
            <span class="src">{{ s.source }}</span>
          </label>
        </div>
        <div class="manual-row">
          <input v-model="manual" placeholder="手动输入会话名 / ID，回车添加" @keydown.enter.prevent="addManual" />
          <button @click="addManual">添加</button>
        </div>
        <div class="modal-actions">
          <button @click="showPicker = false">取消</button>
          <button class="primary" @click="savePicker">保存（{{ checked.length }} 个）</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hint { font-size: 11px; color: var(--text-dim); margin-bottom: 8px; line-height: 1.6; }
.current { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 4px; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 4px 2px 8px; border: 1px solid var(--border-soft); border-radius: 4px;
  background: var(--panel-2); color: var(--text-dim); font-size: 11px; max-width: 100%;
}
.chip .x { min-height: 0; padding: 0 4px; border: none; background: transparent; color: var(--text-muted); font-size: 12px; }
.chip .x:hover { color: var(--red); background: transparent; }
.none { color: var(--text-muted); font-size: 11px; }
.full { width: 100%; }
.mt { margin-top: 8px; }
.mask {
  position: fixed; inset: 0; z-index: 60;
  background: rgba(4, 7, 10, .62); backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center; padding: 20px;
}
.modal {
  width: min(460px, 94vw); max-height: 78vh; display: flex; flex-direction: column;
  padding: 14px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--panel); box-shadow: var(--shadow);
}
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.modal-head .x, .modal-actions button { min-height: 30px; padding: 4px 10px; }
.modal-head .x { border: none; background: transparent; color: var(--text-dim); font-size: 13px; }
.kw { margin-bottom: 8px; }
.sug-list { flex: 1; min-height: 140px; overflow-y: auto; border: 1px solid var(--border-soft); border-radius: 5px; background: rgba(9, 13, 16, .4); }
.sug { display: flex; align-items: center; gap: 8px; padding: 7px 10px; cursor: pointer; border-bottom: 1px solid var(--border-soft); }
.sug:hover { background: rgba(85, 184, 200, .06); }
.sug input[type='checkbox'] { width: auto; min-height: 0; }
.sug .name { color: #dfe9ec; font-size: 12px; }
.sug .uname { flex: 1; overflow: hidden; color: var(--text-muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.sug .src { color: var(--text-muted); font-size: 10px; }
.empty { padding: 18px 10px; color: var(--text-muted); font-size: 11px; text-align: center; }
.manual-row { display: flex; gap: 6px; margin-top: 8px; }
.manual-row input { flex: 1; }
.modal-actions { display: flex; justify-content: flex-end; gap: 7px; margin-top: 10px; }
</style>
