<script setup>
import { computed, reactive, ref, watch } from 'vue'

const props = defineProps({
  config: { type: Object, default: null }, // GET /api/config 的返回
  saving: { type: Boolean, default: false },
})
const emit = defineEmits(['save'])
const dirty = ref(false)
const showKey = ref(false)

const form = reactive({
  engine: 'mock',
  llm_base_url: '',
  llm_model: '',
  llm_api_key: '',
  num_candidates: 1,
  llm_style: 'default',
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

// Agent 预设：pi 走 ChatAgent 内置 RPC 适配；其余为原生 ACP Agent
const PRESETS = {
  custom: { label: '自定义（任意 ACP Agent）', mode: 'acp', command: '', args: '', env_name: '' },
  pi: { label: 'pi（内置 RPC 适配）', mode: 'pi', command: 'pi', args: '', env_name: '' },
  claude: { label: 'Claude Code（claude-code-acp）', mode: 'acp', command: 'claude-code-acp', args: '', env_name: 'ANTHROPIC_AUTH_TOKEN' },
  codex: { label: 'Codex（codex-acp）', mode: 'acp', command: 'codex-acp', args: '', env_name: '' },
  hermes: { label: 'Hermes（填自定义命令）', mode: 'acp', command: '', args: '', env_name: '' },
}

function applyPreset() {
  const p = PRESETS[form.acp_preset]
  if (!p) return
  form.acp_mode = p.mode
  form.acp_command = p.command
  form.acp_args = p.args
  form.acp_env_name = p.env_name
  dirty.value = true
}

watch(
  () => props.config,
  (c) => {
    if (!c || dirty.value) return
    form.engine = c.engine || 'mock'
    form.llm_base_url = c.llm?.base_url || ''
    form.llm_model = c.llm?.model || ''
    form.llm_api_key = c.llm?.api_key || ''
    form.num_candidates = c.llm?.num_candidates ?? 1
    form.llm_style = c.llm?.style || 'default'
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
    form.acp_preset = 'custom'
  },
  { immediate: true }
)

watch(() => props.saving, (saving, wasSaving) => {
  if (wasSaving && !saving) dirty.value = false
})

const presetHint = computed(() => ({
  pi: 'pi 由 ChatAgent 内置适配（pi --mode rpc）；每个联系人一个会话，多轮上下文由 pi 会话文件保持，服务重启不丢。',
  claude: '通过 claude-code-acp 适配器接入；如 claude CLI 已登录可不填 Key。',
  codex: '通过 codex-acp 适配器接入；使用 codex CLI 已有登录态。',
  hermes: 'Hermes 未提供预设，请填写其 ACP 启动命令与参数。',
  custom: '任何实现 Agent Client Protocol 的命令均可接入。',
}[form.acp_preset] || '任何实现 Agent Client Protocol 的命令均可接入。'))

function save() {
  if (props.saving) return
  emit('save', {
    engine: form.engine,
    llm_base_url: form.llm_base_url,
    llm_model: form.llm_model,
    llm_api_key: form.llm_api_key,
    num_candidates: Number(form.num_candidates),
    llm_style: form.llm_style,
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
          AI 接入
          <select v-model="form.engine">
            <option value="mock">Mock 规则（测试用）</option>
            <option value="llm">LLM（OpenAI 兼容接口）</option>
            <option value="acp">Agent（pi / claude code / codex / hermes）</option>
          </select>
        </label>

        <template v-if="form.engine === 'llm'">
          <label>
            OpenAI API URL
            <input v-model.trim="form.llm_base_url" type="url" placeholder="https://api.example.com/v1" autocomplete="url" />
            <span class="hint">填写 Base URL（如包含 /v1）；程序会追加 /chat/completions。</span>
          </label>
          <label>
            模型名称
            <input v-model.trim="form.llm_model" type="text" placeholder="例如 glm-5.3-flash" autocomplete="off" />
          </label>
          <label>
            API Key
            <span class="key-wrap">
              <input v-model="form.llm_api_key" :type="showKey ? 'text' : 'password'" placeholder="填入服务商 API Key" autocomplete="new-password" />
              <button type="button" class="eye" :title="showKey ? '隐藏 Key' : '显示 Key'" @click="showKey = !showKey">{{ showKey ? '🙈' : '👁' }}</button>
            </span>
            <span class="hint">保存后此框显示已保存的 Key，点右侧图标切换明文/圆点；输入新值可替换。</span>
          </label>
        </template>

        <template v-if="form.engine === 'acp'">
          <label>
            Agent 预设
            <select v-model="form.acp_preset" @change="applyPreset">
              <option v-for="(p, k) in PRESETS" :key="k" :value="k">{{ p.label }}</option>
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
            <span class="hint">⚠️ pi / claude code / codex 具备工具执行能力，请指向专用目录。</span>
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
        </template>

        <label>
          候选数量（1-5）
          <input v-model.number="form.num_candidates" type="number" min="1" max="5" />
        </label>
        <label>
          回复风格（LLM）
          <select v-model="form.llm_style">
            <option value="default">默认（口语化、简洁、礼貌）</option>
            <option value="formal">正式专业</option>
            <option value="concise">极简高效</option>
            <option value="casual">轻松口语</option>
            <option value="friendly">热情友好</option>
          </select>
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
        <p class="hint">所有设置保存后写回本机 config.yaml，重启不失效。切换 AI 接入会重建引擎（Agent 会话将重开）。</p>
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
.key-wrap { position: relative; display: block; }
.key-wrap input { padding-right: 42px; }
.eye {
  position: absolute; right: 4px; top: 50%; transform: translateY(-50%);
  min-height: 0; padding: 4px 8px; border: none; background: transparent;
  font-size: 15px; line-height: 1; opacity: .7;
}
.eye:hover:not(:disabled) { background: transparent; opacity: 1; }
</style>
