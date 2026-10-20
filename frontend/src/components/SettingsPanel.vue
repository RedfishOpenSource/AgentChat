<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  config: { type: Object, default: null }, // GET /api/config 的返回
  saving: { type: Boolean, default: false },
})
const emit = defineEmits(['save'])
const dirty = ref(false)

// UI 只提供 Agent 接入（engine 固定 acp）；LLM / Mock 仍可经 config.yaml 配置，但不在界面暴露
const form = reactive({
  engine: 'acp',
  group_trigger: 'at-mention',
  non_text_action: 'placeholder',
  debounce_seconds: 3.0,
  acp_preset: 'custom',
  acp_mode: 'acp',
  acp_command: '',
  acp_args: '',
  acp_cwd: '',
  acp_env_name: '',
  acp_env_key: '',
  acp_timeout: 300,
})

// 可选 Agent 列表（后端注册表 + 本机可用性检测）；CUSTOM_ID 为「手填任意 ACP 命令」入口
const agents = ref([]) // [{id,name,mode,command,args,description,available}]
const CUSTOM_ID = 'custom'
const agentOptions = computed(() => [
  ...agents.value.map((a) => ({
    ...a,
    label: `${a.name} — ${a.command}${a.available ? '' : '（未检测到，需先安装）'}`,
  })),
  { id: CUSTOM_ID, name: '自定义', label: '自定义（手填任意 ACP Agent 命令）', available: true },
])

async function loadAgents() {
  try {
    const data = await api.agents()
    agents.value = data.agents || []
  } catch { /* 后端未启动时忽略 */ }
}
onMounted(loadAgents)

// 预填：选中注册表项时自动填 mode/command/args；custom 保持当前手填值
function applyAgent() {
  if (form.acp_preset === CUSTOM_ID) {
    dirty.value = true
    return
  }
  const a = agents.value.find((x) => x.id === form.acp_preset)
  if (!a) return
  form.acp_mode = a.mode
  form.acp_command = a.command
  form.acp_args = a.args || ''
  dirty.value = true
}

// 回显：根据 mode+command 反推选中的 Agent（匹配不到则归为自定义）
function detectPreset(mode, command) {
  const hit = agents.value.find((a) => a.mode === mode && a.command === (command || '').trim())
  return hit ? hit.id : CUSTOM_ID
}

const presetHint = computed(() => {
  if (form.acp_preset === CUSTOM_ID) return '任何实现 Agent Client Protocol（JSON-RPC over stdio）的命令均可接入。'
  const a = agents.value.find((x) => x.id === form.acp_preset)
  return a?.description || '通过 ACP 协议（JSON-RPC over stdio）接入现成 Agent。'
})

function autoSelectAvailable() {
  if (dirty.value || form.acp_command.trim()) return
  const first = agents.value.find((a) => a.available) || agents.value[0]
  if (first) {
    form.acp_preset = first.id
    form.acp_mode = first.mode
    form.acp_command = first.command
    form.acp_args = first.args || ''
  }
}

watch(
  () => props.config,
  (c) => {
    if (!c || dirty.value) return
    form.group_trigger = c.group_trigger || 'at-mention'
    form.non_text_action = c.non_text_action || 'placeholder'
    form.debounce_seconds = c.debounce_seconds ?? 3.0
    form.acp_mode = c.acp?.mode || 'acp'
    form.acp_command = c.acp?.command || ''
    form.acp_args = c.acp?.args || ''
    form.acp_cwd = c.acp?.cwd || ''
    form.acp_env_name = c.acp?.env_name || ''
    form.acp_env_key = c.acp?.env_key || ''
    form.acp_timeout = c.acp?.timeout ?? 300
    form.acp_preset = detectPreset(form.acp_mode, form.acp_command)
    if (!form.acp_command.trim()) autoSelectAvailable()
  },
  { immediate: true }
)

watch(agents, () => {
  // 注册表加载完成后重新回显（config 先到、agents 后到的时序）
  if (!dirty.value) {
    form.acp_preset = detectPreset(form.acp_mode, form.acp_command)
    autoSelectAvailable()
  }
})

watch(() => props.saving, (saving, wasSaving) => {
  if (wasSaving && !saving) dirty.value = false
})

function save() {
  if (props.saving) return
  emit('save', {
    engine: 'acp',
    group_trigger: form.group_trigger,
    non_text_action: form.non_text_action,
    debounce_seconds: Number(form.debounce_seconds),
    acp_mode: form.acp_mode,
    acp_command: form.acp_command,
    acp_args: form.acp_args,
    acp_cwd: form.acp_cwd,
    acp_env_name: form.acp_env_name,
    acp_env_key: form.acp_env_key,
    acp_timeout: Number(form.acp_timeout),
    persist: true,
  })
}
</script>

<template>
  <div class="card">
    <h3>设置（热更新）</h3>
    <details>
      <summary>展开设置项</summary>
      <div class="fields" @input="dirty = true" @change="dirty = true">
        <label>
          AI Agent
          <select v-model="form.acp_preset" @change="applyAgent">
            <option v-for="a in agentOptions" :key="a.id" :value="a.id">{{ a.label }}</option>
          </select>
          <span class="hint">{{ presetHint }}</span>
        </label>
        <label>
          会话模式
          <select v-model="form.acp_mode">
            <option value="acp">ACP 协议（单进程多会话）</option>
            <option value="pi">pi RPC（每联系人独立进程，会话落盘）</option>
          </select>
        </label>
        <label>
          启动命令
          <input v-model.trim="form.acp_command" type="text" :placeholder="form.acp_mode === 'pi' ? 'pi' : 'claude-code-acp'" autocomplete="off" />
        </label>
        <label>
          启动参数
          <input v-model="form.acp_args" type="text" placeholder="额外参数，空格分隔" autocomplete="off" />
        </label>
        <label>
          工作空间（cwd）
          <input v-model.trim="form.acp_cwd" type="text" placeholder="D:\workspace\chatagent" autocomplete="off" />
          <span class="hint">⚠️ Agent 具备工具执行（含 shell）能力，请指向专用目录。</span>
        </label>
        <label>
          鉴权环境变量名
          <input v-model.trim="form.acp_env_name" type="text" placeholder="如 ANTHROPIC_AUTH_TOKEN（可留空）" autocomplete="off" />
        </label>
        <label>
          Key（环境变量值）
          <input v-model="form.acp_env_key" type="password" placeholder="留空 = 使用 Agent 已有登录态" autocomplete="new-password" />
        </label>
        <label>
          单次回复超时（秒）
          <input v-model.number="form.acp_timeout" type="number" min="5" max="3600" step="10" />
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
        <button class="primary full" :disabled="props.saving" @click="save">
          {{ props.saving ? '应用中...' : '保存并立即生效（写回配置文件）' }}
        </button>
        <p class="hint">所有设置保存后写回本机 config.yaml，重启不失效。切换 Agent 会重建引擎（Agent 会话将重开）。</p>
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
