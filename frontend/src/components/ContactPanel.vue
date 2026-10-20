<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  platform: { type: String, required: true }, // wechat | dingtalk
  contacts: { type: Array, default: () => [] }, // 生效监听名单
  connector: { type: Object, default: null }, // 连接器状态（启停/错误）
})
const emit = defineEmits(['saved', 'error', 'connector-changed'])

const running = computed(() => props.connector?.status === 'running')
const statusText = computed(() =>
  ({ running: '监听中', stopped: '已停止', error: '异常' }[props.connector?.status] || '已停止'))

async function toggleConnector() {
  try {
    await api.connector(props.platform, running.value ? 'stop' : 'start')
    emit('connector-changed')
  } catch (e) { emit('error', e.message) }
}

const PLATFORM_LABELS = { wechat: '微信', dingtalk: '钉钉' }
const label = PLATFORM_LABELS[props.platform] || props.platform
const saving = ref(false)
const showPicker = ref(false)
const loading = ref(false)
const suggestions = ref([]) // [{name, username, source}]
const checked = ref([])     // 弹窗中勾选的监听联系人
const manual = ref('')
const keyword = ref('')
const profiles = ref([])    // AgentStudio 创建的 Agent
const rows = ref({})        // chat_id -> {agent_id, timeout, group_trigger, non_text_action, debounce_seconds}

const filteredSuggestions = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return suggestions.value
  return suggestions.value.filter((s) =>
    (s.name || '').toLowerCase().includes(k) || (s.username || '').toLowerCase().includes(k))
})

function valueOf(s) { return s.name || s.username || '' }

function emptyConfig() {
  // 必须从 AgentStudio 选择 Agent：默认取第一个
  return { agent_id: defaultAgentId(), timeout: 300, group_trigger: 'at-mention', non_text_action: 'placeholder', debounce_seconds: 3 }
}

function defaultAgentId() {
  return profiles.value[0]?.id || ''
}

function rowOf(name) {
  if (!rows.value[name]) rows.value[name] = emptyConfig()
  return rows.value[name]
}

async function loadConfigs() {
  try {
    const [cfgData, profileData] = await Promise.all([api.contactConfigs(props.platform), api.agentProfiles()])
    profiles.value = profileData.profiles || []
    const assignments = profileData.assignments || []
    const next = {}
    for (const c of props.contacts) {
      const hit = (cfgData.configs || []).find((x) => x.chat_id === c)
      const asg = assignments.find((a) => a.platform === props.platform && a.chat_id === c)
      const local = rows.value[c]
      next[c] = {
        // 已有绑定优先；否则取本地未保存的选择；都没有则默认第一个 Agent
        agent_id: asg ? asg.agent_id : (local?.agent_id || defaultAgentId()),
        timeout: hit?.timeout ?? local?.timeout ?? 300,
        group_trigger: hit?.group_trigger || local?.group_trigger || 'at-mention',
        non_text_action: hit?.non_text_action || local?.non_text_action || 'placeholder',
        debounce_seconds: hit?.debounce_seconds ?? local?.debounce_seconds ?? 3,
      }
    }
    rows.value = next
  } catch { /* 后端未启动时忽略 */ }
}

onMounted(loadConfigs)
watch(() => props.contacts, loadConfigs)

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

function isChecked(s) { return checked.value.includes(valueOf(s)) }

function toggle(s) {
  const v = valueOf(s)
  if (!v) return
  const i = checked.value.indexOf(v)
  if (i >= 0) checked.value.splice(i, 1)
  else {
    checked.value.push(v)
    rowOf(v) // 勾选即准备配置行，可在选择时指定 Agent
  }
}

function addManual() {
  const v = manual.value.trim()
  if (v && !checked.value.includes(v)) {
    checked.value.push(v)
    rowOf(v)
  }
  manual.value = ''
}

async function finishPicker() {
  if (saving.value) return
  if (!profiles.value.length) {
    emit('error', '请先在 Agent Studio 创建 Agent，再选择监听联系人')
    return
  }
  if (checked.value.some((c) => !rowOf(c).agent_id)) {
    emit('error', '每个监听联系人都必须指定 Agent')
    return
  }
  saving.value = true
  try {
    await api.setContacts(props.platform, [...checked.value])
    showPicker.value = false
    emit('saved') // 父组件刷新后，选中的联系人连同所选 Agent 出现在下方设置区
  } catch (e) { emit('error', e.message) } finally { saving.value = false }
}

async function removeRow(c) {
  if (saving.value) return
  if (!window.confirm(`移除对「${c}」的监听？`)) return
  saving.value = true
  try {
    await api.setContacts(props.platform, props.contacts.filter((x) => x !== c))
    emit('saved')
  } catch (e) { emit('error', e.message) } finally { saving.value = false }
}

const showEdit = ref(false)
const editTarget = ref('')
const editForm = reactive({ ...emptyConfig() })

function openEdit(c) {
  editTarget.value = c
  Object.assign(editForm, rowOf(c))
  showEdit.value = true
}

async function saveEdit() {
  if (saving.value) return
  if (!editForm.agent_id && !profiles.value.length) {
    emit('error', '请先在 Agent Studio 创建 Agent')
    return
  }
  saving.value = true
  try {
    await api.saveContactConfig({ platform: props.platform, chat_id: editTarget.value, ...editForm })
    await api.assignAgent(props.platform, editTarget.value, editForm.agent_id)
    showEdit.value = false
    emit('saved')
  } catch (e) { emit('error', e.message) } finally { saving.value = false }
}

defineExpose({ loadConfigs })
</script>

<template>
  <div class="card">
    <div class="head-row">
      <h3>监听联系人 · {{ label }}</h3>
      <button class="toggle" :class="{ on: running }" :title="connector?.last_error" @click="toggleConnector">
        {{ running ? '⏸ 停止监听' : '▶ 启用监听' }}
      </button>
    </div>
    <p class="conn-line">
      <span class="dot" :class="connector?.status || 'stopped'"></span>
      {{ statusText }}<span v-if="connector"> · {{ connector.mode }} · {{ connector.poll_interval }}s</span>
    </p>
    <p v-if="connector?.last_error" class="conn-err" :title="connector.last_error">{{ connector.last_error }}</p>
    <p class="hint">默认不监听任何会话；启用监听且选中联系人才会回复。</p>
    <div class="current">
      <span v-for="c in contacts" :key="c" class="chip">{{ c }}</span>
      <span v-if="!contacts.length" class="none">未选择（不监听）</span>
    </div>
    <button class="primary full mt" :disabled="saving" @click="openPicker">👥 选择监听联系人</button>

    <div v-if="contacts.length" class="config-block">
      <div class="section-title">监听联系人（点「编辑」配置 Agent 等参数）</div>
      <div v-for="c in contacts" :key="c" class="row-line">
        <span class="cname" :title="c">{{ c }}</span>
        <button class="sm" @click="openEdit(c)">编辑</button>
        <button class="sm danger" @click="removeRow(c)">删除</button>
      </div>
    </div>

    <div v-if="showEdit" class="mask" @click.self="showEdit = false">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-head">
          <strong>监听设置 · {{ editTarget }}</strong>
          <button class="x" @click="showEdit = false">✕</button>
        </div>
        <div class="edit-form">
          <label>Agent *
            <select v-model="editForm.agent_id">
              <option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <span v-if="!profiles.length" class="field-err">请先在 Agent Studio 创建 Agent</span>
          </label>
          <div class="grid2">
            <label>群聊触发
              <select v-model="editForm.group_trigger">
                <option value="at-mention">仅 @我</option>
                <option value="all">全部消息</option>
                <option value="listen-only">只监听不回复</option>
              </select>
            </label>
            <label>非文本
              <select v-model="editForm.non_text_action">
                <option value="placeholder">生成提示草稿</option>
                <option value="skip">直接忽略</option>
              </select>
            </label>
            <label>连发合并（秒）<input v-model.number="editForm.debounce_seconds" type="number" min="0" step="0.5" /></label>
            <label>超时（秒）<input v-model.number="editForm.timeout" type="number" min="5" max="3600" /></label>
          </div>
        </div>
        <div class="modal-actions">
          <button @click="showEdit = false">取消</button>
          <button class="primary" :disabled="saving" @click="saveEdit">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>

    <div v-if="showPicker" class="mask" @click.self="showPicker = false">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-head">
          <strong>选择{{ label }}监听联系人</strong>
          <button class="x" @click="showPicker = false">✕</button>
        </div>
        <input v-model="keyword" class="kw" placeholder="搜索会话名 / ID" />
        <div class="sug-list">
          <div v-if="loading" class="empty">加载中…</div>
          <div v-else-if="!filteredSuggestions.length" class="empty">暂无建议（启动监听后可读取会话列表），可手动添加</div>
          <div v-for="s in filteredSuggestions" :key="(s.username || '') + '|' + (s.name || '')" class="sug-wrap">
            <div class="sug" @click="toggle(s)">
              <input type="checkbox" :checked="isChecked(s)" @click.stop @change="toggle(s)" />
              <span class="name">{{ s.name || s.username }}</span>
              <span v-if="s.username && s.username !== s.name" class="uname">{{ s.username }}</span>
              <span class="src">{{ s.source }}</span>
            </div>
            <div v-if="isChecked(s)" class="sug-agent">
              <span>Agent</span>
              <select v-model="rowOf(valueOf(s)).agent_id" @click.stop>
                <option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}</option>
              </select>
              <span v-if="!profiles.length" class="no-agent">请先在 Agent Studio 创建</span>
            </div>
          </div>
        </div>
        <div class="manual-row">
          <input v-model="manual" placeholder="手动输入会话名 / ID，回车添加" @keydown.enter.prevent="addManual" />
          <button @click="addManual">添加</button>
        </div>
        <div class="modal-actions">
          <button :disabled="saving" @click="finishPicker">
            {{ saving ? '保存中…' : `完成（监听 ${checked.length} 人）` }}
          </button>
        </div>
        <p class="pick-hint">勾选时即可指定 Agent；点「完成」后可在下方继续调整逐人设置。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.head-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.head-row h3 { margin: 0; }
.toggle { min-height: 26px; padding: 3px 9px; font-size: 10px; flex: none; }
.toggle.on { background: rgba(233, 121, 112, .1); border-color: rgba(233, 121, 112, .4); color: var(--red); }
.conn-line { display: flex; align-items: center; gap: 6px; margin: 6px 0 0; color: var(--text-muted); font-size: 10px; }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); flex: none; }
.dot.running { background: var(--green); box-shadow: 0 0 8px rgba(85, 201, 138, .4); }
.dot.error { background: var(--red); box-shadow: 0 0 8px rgba(233, 121, 112, .4); }
.conn-err { margin: 5px 0 0; overflow: hidden; color: var(--red); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.hint { font-size: 11px; color: var(--text-dim); margin-bottom: 8px; line-height: 1.6; }
.current { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 4px; }
.chip { padding: 2px 8px; border: 1px solid var(--border-soft); border-radius: 4px; background: var(--panel-2); color: var(--text-dim); font-size: 11px; }
.none { color: var(--text-muted); font-size: 11px; }
.full { width: 100%; }
.mt { margin-top: 8px; }
.config-block { margin-top: 10px; }
.section-title { color: var(--text-dim); font-size: 11px; margin-bottom: 6px; }
.row-line { display: flex; align-items: center; gap: 6px; padding: 6px 9px; margin-bottom: 5px; border: 1px solid var(--border-soft); border-radius: 6px; background: rgba(9, 13, 16, .4); }
.cname { flex: 1; min-width: 0; overflow: hidden; color: #dfe9ec; font-size: 12px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.sm { min-height: 24px; padding: 2px 9px; font-size: 10px; flex: none; }
.sm.danger { color: var(--red); }
.edit-form { display: flex; flex-direction: column; gap: 10px; }
.edit-form label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--text-dim); }
.edit-form select, .edit-form input { min-height: 30px; font-size: 12px; }
.field-err { color: var(--red); font-size: 10px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
.mask { position: fixed; inset: 0; z-index: 60; background: rgba(4, 7, 10, .62); display: flex; align-items: center; justify-content: center; padding: 20px; }
.modal { width: min(600px, 94vw); max-height: 82vh; display: flex; flex-direction: column; padding: 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); box-shadow: var(--shadow); }
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.modal-head .x, .modal-actions button { min-height: 30px; padding: 4px 10px; }
.modal-head .x { border: none; background: transparent; color: var(--text-dim); font-size: 13px; }
.kw { margin-bottom: 8px; }
.sug-list { flex: 1; min-height: 140px; overflow-y: auto; border: 1px solid var(--border-soft); border-radius: 5px; background: rgba(9, 13, 16, .4); }
.sug-wrap { border-bottom: 1px solid var(--border-soft); }
.sug { display: flex; align-items: center; gap: 8px; padding: 7px 10px; cursor: pointer; }
.sug:hover { background: rgba(85, 184, 200, .06); }
.sug input[type='checkbox'] { width: auto; min-height: 0; }
.sug .name { color: #dfe9ec; font-size: 12px; }
.sug .uname { flex: 1; overflow: hidden; color: var(--text-muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.sug .src { color: var(--text-muted); font-size: 10px; }
.sug-agent { display: flex; align-items: center; gap: 7px; padding: 0 10px 8px 30px; color: var(--text-dim); font-size: 10px; }
.sug-agent select { width: 180px; min-height: 26px; padding: 2px 6px; font-size: 11px; }
.no-agent { color: var(--red); font-size: 10px; }
.empty { padding: 18px 10px; color: var(--text-muted); font-size: 11px; text-align: center; }
.manual-row { display: flex; gap: 6px; margin-top: 8px; }
.manual-row input { flex: 1; }
.modal-actions { display: flex; justify-content: flex-end; gap: 7px; margin-top: 10px; }
.pick-hint { margin-top: 7px; color: var(--text-muted); font-size: 10px; text-align: center; }
</style>
