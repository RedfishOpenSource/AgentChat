<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import AgentDebug from './AgentDebug.vue'
import { api } from '../api'

const props = defineProps({
  saving: { type: Boolean, default: false },
  debugMessages: { type: Array, default: () => [] },
  processing: { type: Object, default: () => ({}) },
  connected: { type: Boolean, default: true },
})
const emit = defineEmits(['toast', 'message'])
const debugProfile = ref(null)
const debugSession = ref('')  // 非空 = 打开指定会话（init = 初始化问询确认）

const ENGINES = [
  { id: 'pi', name: 'PI', command: 'pi-acp' },
  { id: 'claude-code', name: 'Claude Code', command: 'claude-code-acp' },
  { id: 'codex', name: 'Codex', command: 'codex-acp' },
  { id: 'hermes', name: 'Hermes', command: 'hermes-acp' },
]
const CAPABILITIES = ['文件读写', '执行命令', '网页抓取']

const profiles = ref([])      // [{...profile, available}]
const showForm = ref(false)
const savingProfile = ref(false)
const pickingWorkspace = ref(false)
const pickingKnowledge = ref(false)
const pickingSkill = ref(false)
const openingMcp = ref(false)
const skillUrl = ref('')
const form = reactive({
  id: '', name: '', description: '',
  engine: 'pi', workspace: '',
  capabilities: [], skills: [], knowledge_dir: '',
  plan_before_execute: false, timeout: 300,
})
const formTab = ref('build') // build | settings
const formError = ref('')
const fieldErrors = reactive({ name: false, workspace: false, description: false })

async function load() {
  try {
    const data = await api.agentProfiles()
    profiles.value = data.profiles || []
    if (profiles.value.some(p => p.pending)) startPendingPoll()
    openInitInterviewAuto(profiles.value)
  } catch (e) { emit('toast', `Agent 配置加载失败: ${e.message}`) }
}

function openInitInterviewAuto(list) {
  // 初始化问询完成后自动打开调试窗口，把待确认问题展示给用户（每个浏览器会话只自动弹一次）。
  const target = list.find(p => p.init_interview?.status === 'done' && !p.init_interview.answered)
  if (!target || debugProfile.value) return
  const key = `chatagent:init-interview-opened:${target.id}`
  try {
    if (sessionStorage.getItem(key)) return
    sessionStorage.setItem(key, '1')
  } catch { return }
  debugSession.value = 'init'
  debugProfile.value = target
}

function openInitInterview(p) {
  debugSession.value = 'init'
  debugProfile.value = p
}

let pendingTimer = null
function startPendingPoll() {
  if (pendingTimer) return
  pendingTimer = setInterval(async () => {
    if (!profiles.value.some((p) => p.pending)) { clearInterval(pendingTimer); pendingTimer = null; return }
    try {
      const data = await api.agentProfiles()
      const previous = profiles.value
      profiles.value = data.profiles || []
      for (const p of profiles.value) {
        if (previous.find(old => old.id === p.id)?.pending && !p.pending) {
          emit('toast', p.rewrite_status === 'failed' ? `角色改写失败：${p.rewrite_error}` : `「${p.name}」角色已重新改写并保存到 role.md`)
        }
      }
      openInitInterviewAuto(profiles.value)
    } catch { /* ignore */ }
  }, 2500)
}
onMounted(load)
onUnmounted(() => clearInterval(pendingTimer))

function engineCommand(id) {
  return ENGINES.find((e) => e.id === id)?.command || id
}

function startCreate() {
  Object.assign(form, {
    id: '', name: '', description: '',
    engine: 'pi', workspace: '',
    capabilities: [...CAPABILITIES],  // 基础能力默认全开
    skills: [], knowledge_dir: '',
    plan_before_execute: false, timeout: 300,
  })
  skillUrl.value = ''
  formTab.value = 'build'
  formError.value = ''
  fieldErrors.name = false
  fieldErrors.workspace = false
  showForm.value = true
}

function editProfile(p) {
  Object.assign(form, {
    id: p.id, name: p.name, description: p.description || '',
    engine: p.engine || 'pi', workspace: p.workspace || '',
    capabilities: [...(p.capabilities || [])],
    skills: [...(p.skills || [])],
    knowledge_dir: p.knowledge_dir || '',
    plan_before_execute: !!p.plan_before_execute, timeout: p.timeout || 300,
  })
  skillUrl.value = ''
  formTab.value = 'build'
  formError.value = ''
  fieldErrors.name = false
  fieldErrors.workspace = false
  showForm.value = true
}

function addSkillUrl() {
  const v = skillUrl.value.trim()
  if (!v) return
  if (!/^https:\/\/|http:\/\/|git@/.test(v)) { formError.value = 'Skill 链接需以 http(s):// 或 git@ 开头'; return }
  if (!form.skills.includes(v)) form.skills.push(v)
  skillUrl.value = ''
}

async function addSkillDir() {
  if (pickingSkill.value) return
  pickingSkill.value = true
  try {
    const { path } = await api.pickDirectory('')
    if (path && !form.skills.includes(path)) form.skills.push(path)
  } catch (e) { emit('toast', e.message) } finally { pickingSkill.value = false }
}

function removeSkill(i) { form.skills.splice(i, 1) }

async function chooseKnowledge() {
  if (pickingKnowledge.value) return
  pickingKnowledge.value = true
  try {
    const { path } = await api.pickDirectory('')
    if (path) form.knowledge_dir = path
  } catch (e) { emit('toast', e.message) } finally { pickingKnowledge.value = false }
}

async function openMcpConfig() {
  if (!form.workspace) { formError.value = '请先选择工作空间，再编辑 MCP 配置'; formTab.value = 'settings'; return }
  if (openingMcp.value) return
  openingMcp.value = true
  try {
    await api.openMcpConfig(form.workspace)
    emit('toast', '已打开 MCP 配置文件，保存后下次会话生效')
  } catch (e) { formError.value = e.message } finally { openingMcp.value = false }
}

async function chooseWorkspace() {
  if (pickingWorkspace.value) return
  pickingWorkspace.value = true
  try {
    const { path } = await api.pickDirectory(form.workspace)
    if (path) form.workspace = path
  } catch (e) { emit('toast', e.message) } finally { pickingWorkspace.value = false }
}

async function saveProfile() {
  if (savingProfile.value) return
  fieldErrors.name = !form.name.trim()
  fieldErrors.workspace = !form.workspace.trim()
  fieldErrors.description = !form.description.trim()
  const missing = []
  if (fieldErrors.name) missing.push('名称（构建页）')
  if (fieldErrors.workspace) missing.push('工作空间（设置页）')
  if (fieldErrors.description) missing.push('描述（构建页）')
  if (missing.length) {
    formError.value = `请填写红框标注的必填项：${missing.join('、')}`
    formTab.value = fieldErrors.workspace ? 'settings' : 'build'
    return
  }
  formError.value = ''
  savingProfile.value = true
  try {
    const body = {
      id: form.id || undefined, name: form.name, description: form.description,
      skills: [...form.skills],
      engine: form.engine, command: '', args: '',
      workspace: form.workspace, capabilities: form.capabilities,
      knowledge_dir: form.knowledge_dir, mcp_servers: [],
      plan_before_execute: form.plan_before_execute, timeout: Number(form.timeout),
    }
    const data = await api.saveAgentProfile(body)
    showForm.value = false
    emit('toast', `Agent 已保存：${data.prompts_note || '后台同步中'}`)
    await load()
    startPendingPoll()
  } catch (e) { formError.value = `保存失败: ${e.message}` } finally { savingProfile.value = false }
}

async function removeProfile(id) {
  if (!window.confirm('确定删除该 Agent？')) return
  try { await api.deleteAgentProfile(id); emit('toast', 'Agent 已删除'); await load() } catch (e) { emit('toast', `删除失败: ${e.message}`) }
}
</script>

<template>
  <div class="card">
    <details class="studio">
      <summary>
        <h3>Agent Studio <span class="badge">{{ profiles.length }}</span></h3>
        <span class="chevron">▸</span>
      </summary>
      <p class="hint">创建并维护 Agent 集合；创建并初始化完成后，Agent 会自动列出待确认的环境等问题，并在调试窗口中与用户逐条确认。</p>

    <div v-for="p in profiles" :key="p.id" class="profile-row">
      <span class="p-name" :title="p.description || p.name">{{ p.name }}</span>
      <span v-if="p.pending" class="tag sync">后台处理中…</span>
      <template v-else>
        <span v-if="p.rewrite_status === 'failed'" class="tag bad" :title="p.rewrite_error">改写失败</span>
        <span v-else class="tag" :class="p.available ? 'ok' : 'bad'" :title="p.available ? '' : '命令未检测到'">
          {{ p.available ? engineCommand(p.engine) : '未检测到' }}
        </span>
        <span v-if="p.init_interview?.status === 'running' || p.init_interview?.status === 'pending'" class="tag sync" title="正在初始化问询，完成后自动打开调试">初始化问询中…</span>
        <button v-else-if="p.init_interview?.status === 'done' && !p.init_interview.answered" class="tag init-tag" title="Agent 有待确认的问题，点击查看并回复" @click="openInitInterview(p)">待确认</button>
        <span v-else-if="p.init_interview?.status === 'failed'" class="tag bad" :title="p.init_interview.error">初始化问询失败</span>
      </template>
      <span class="grow"></span>
      <button class="sm" :disabled="p.pending || p.rewrite_status === 'failed'" @click="debugSession = ''; debugProfile = p">调试</button>
      <button class="sm" @click="editProfile(p)">编辑</button>
      <button class="sm danger" @click="removeProfile(p.id)">删除</button>
    </div>
    <p v-if="!profiles.length" class="empty">还没有 Agent，点击下方创建。</p>

    <button class="primary full" @click="startCreate">＋ 创建 Agent</button>

    <div v-if="showForm" class="mask" @click.self="showForm = false">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-head">
          <strong>{{ form.id ? '编辑 Agent' : '创建 Agent' }}</strong>
          <button class="x" @click="showForm = false">✕</button>
        </div>
        <div class="tabs">
          <button :class="{ active: formTab === 'build' }" @click="formTab = 'build'">构建</button>
          <button :class="{ active: formTab === 'settings' }" @click="formTab = 'settings'">设置</button>
        </div>
        <div v-if="formTab === 'build'" class="form">
          <label>名称 *<input v-model.trim="form.name" :class="{ invalid: fieldErrors.name }" placeholder="如：客服助手" @input="fieldErrors.name = false" />
            <span v-if="fieldErrors.name" class="field-err">名称为必填项</span>
          </label>
          <label>描述 *（每次保存都由 AI 重新改写）<textarea v-model="form.description" :class="{ invalid: fieldErrors.description }" rows="6" placeholder="描述这个 Agent 负责做什么：比如「售后客服，负责回答退货、报销流程问题，回复简短友好」" @input="fieldErrors.description = false" /></label>
          <p v-if="profiles.find(p => p.id === form.id)?.rewrite_error" class="form-error">{{ profiles.find(p => p.id === form.id).rewrite_error }}</p>
          <p class="tab-hint">保存后可在列表查看改写状态；成功后重新打开编辑会显示优化后的描述，role.md 同步更新。</p>
          <p class="tab-hint">Skills、引擎、工作空间、知识库等在「设置」页配置。</p>
        </div>
        <div v-else class="form">
          <div class="grid2">
            <label>底层引擎
              <select v-model="form.engine">
                <option v-for="e in ENGINES" :key="e.id" :value="e.id">{{ e.name }}</option>
              </select>
            </label>
            <label>超时（秒）<input v-model.number="form.timeout" type="number" min="5" max="3600" /></label>
          </div>
          <div class="ws-row">
            <label class="grow">工作空间 *
              <input :value="form.workspace" readonly :class="{ invalid: fieldErrors.workspace }" placeholder="选择该 Agent 的专用目录" />
              <span v-if="fieldErrors.workspace" class="field-err">工作空间为必填项，请选择文件夹</span>
            </label>
            <button type="button" class="ws-btn" :disabled="pickingWorkspace" @click="chooseWorkspace">
              {{ pickingWorkspace ? '选择中…' : '选择文件夹…' }}
            </button>
          </div>
          <label>能力</label>
          <div class="caps">
            <label v-for="c in CAPABILITIES" :key="c" class="cap">
              <input type="checkbox" :value="c" v-model="form.capabilities" /> {{ c }}
            </label>
          </div>
          <label>Skills（保存后自动安装到工作空间 .agents/skills/）</label>
          <div v-for="(s, i) in form.skills" :key="s" class="skill-row">
            <span class="skill-name" :title="s">{{ s }}</span>
            <button type="button" class="x" @click="removeSkill(i)">✕</button>
          </div>
          <div class="ws-row">
            <input v-model="skillUrl" class="grow" placeholder="输入 Skill 链接（http(s):// 或 git@），回车添加" @keydown.enter.prevent="addSkillUrl" />
            <button type="button" class="ws-btn plain" @click="addSkillUrl">添加链接</button>
            <button type="button" class="ws-btn plain" :disabled="pickingSkill" @click="addSkillDir">
              {{ pickingSkill ? '选择中…' : '选择本地目录' }}
            </button>
          </div>
          <div class="ws-row">
            <label class="grow">知识库目录（可选；.md/.txt，Agent 可自我更新）
              <input :value="form.knowledge_dir" readonly placeholder="选择知识库目录" />
            </label>
            <button type="button" class="ws-btn" :disabled="pickingKnowledge" @click="chooseKnowledge">
              {{ pickingKnowledge ? '选择中…' : '选择文件夹…' }}
            </button>
          </div>
          <div class="ws-row">
            <span class="mcp-label">MCP（工作空间 .agents/mcps/.mcp.json）</span>
            <button type="button" class="ws-btn" :disabled="openingMcp" @click="openMcpConfig">
              {{ openingMcp ? '打开中…' : '在编辑器中打开' }}
            </button>
          </div>
          <div class="plan-row">
            <span>先制定计划再执行</span>
            <button
              type="button"
              class="switch"
              :class="{ on: form.plan_before_execute }"
              :aria-pressed="form.plan_before_execute"
              @click="form.plan_before_execute = !form.plan_before_execute"
            >
              <span class="knob"></span>
            </button>
          </div>
        </div>
        <div v-if="formError" class="form-error" role="alert">⚠️ {{ formError }}</div>
        <div class="modal-actions">
          <button @click="showForm = false">取消</button>
          <button class="primary" :disabled="savingProfile" @click="saveProfile">
            {{ savingProfile ? '保存中…' : '保存并生效' }}
          </button>
        </div>
      </div>
    </div>
    </details>
    <AgentDebug
      v-if="debugProfile"
      :key="`${debugProfile.id}:${debugSession}`"
      :initial-session="debugSession"
      :profile="debugProfile"
      :messages="debugMessages"
      :processing="processing"
      :connected="connected"
      @close="debugProfile = null"
      @message="emit('message', $event)"
    />
  </div>
</template>

<style scoped>
.badge { margin-left: 6px; padding: 1px 7px; border-radius: 8px; background: rgba(85, 184, 200, .12); color: var(--accent-strong); font-size: 10px; }
.studio > summary { display: flex; align-items: center; justify-content: space-between; cursor: pointer; list-style: none; }
.studio > summary::-webkit-details-marker { display: none; }
.studio > summary .chevron { color: var(--text-muted); font-size: 10px; transition: transform .15s; }
.studio[open] > summary .chevron { transform: rotate(90deg); }
.studio[open] > summary { margin-bottom: 4px; }
.hint { font-size: 11px; color: var(--text-dim); margin: 6px 0 10px; line-height: 1.6; }
.profile-row { display: flex; align-items: center; gap: 6px; padding: 7px 9px; margin-bottom: 6px; border: 1px solid var(--border-soft); border-radius: 6px; background: rgba(9, 13, 16, .4); }
.p-name { flex: 1; min-width: 0; max-width: 120px; overflow: hidden; color: #dfe9ec; font-size: 12px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.grow { flex: 1; }
.tag { padding: 1px 7px; border-radius: 4px; background: var(--panel-2); color: var(--text-dim); font-size: 10px; flex: none; }
.tag.ok { color: var(--green); border-color: rgba(85, 201, 138, .3); }
.tag.bad { color: var(--red); border-color: rgba(233, 121, 112, .3); }
.tag.sync { color: var(--accent-strong); border-color: rgba(85, 184, 200, .35); animation: pulse 1.2s ease-in-out infinite; }
.tag.init-tag { color: var(--accent-strong); border: 1px solid rgba(85, 184, 200, .45); cursor: pointer; }
@keyframes pulse { 50% { opacity: .45; } }
.sm { min-height: 26px; padding: 3px 9px; font-size: 11px; }
.sm.danger { color: var(--red); }
.empty { color: var(--text-muted); font-size: 11px; text-align: center; padding: 8px 0; }
.full { width: 100%; }
.mask { position: fixed; inset: 0; z-index: 70; background: rgba(4, 7, 10, .62); display: flex; align-items: center; justify-content: center; padding: 20px; }
.form-error { margin-top: 8px; color: var(--orange); font-size: 11px; }
.label-err { color: var(--red) !important; font-weight: 600; }
.field-err { color: var(--red); font-size: 10px; }
.invalid { border-color: var(--red) !important; box-shadow: 0 0 0 3px rgba(233, 121, 112, .14) !important; }
.invalid::placeholder { color: var(--red); opacity: .75; }
.modal { width: min(720px, 96vw); max-height: 88vh; display: flex; flex-direction: column; padding: 16px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); box-shadow: var(--shadow); overflow: hidden; }
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.modal-head .x { min-height: 0; padding: 0 4px; border: none; background: transparent; color: var(--text-dim); font-size: 13px; }
.form { overflow-y: auto; display: flex; flex-direction: column; gap: 9px; padding-right: 4px; }
.form label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--text-dim); }
.form input, .form select, .form textarea { font-size: 12px; }
.tabs { display: flex; gap: 2px; margin-bottom: 10px; border-bottom: 1px solid var(--border-soft); }
.tabs button { min-height: 32px; padding: 6px 14px; border: 0; background: transparent; color: var(--text-dim); font-size: 12px; border-radius: 5px 5px 0 0; }
.tabs button.active { background: var(--panel-2); color: var(--accent-strong); box-shadow: inset 0 -2px 0 var(--accent); }
.tab-hint { color: var(--text-muted); font-size: 10px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.ws-row { display: flex; align-items: flex-start; gap: 7px; }
.ws-row .grow { flex: 1; min-width: 0; }
/* 与带标题的输入框水平对齐：标题行高 ≈16px + (输入框36 - 按钮30)/2 ≈ 19px */
.ws-btn { flex: none; margin-top: 19px; min-height: 30px; padding: 4px 10px; font-size: 11px; white-space: nowrap; }
.ws-btn.plain { margin-top: 3px; }
.skill-row { display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 4px 8px; border: 1px solid var(--border-soft); border-radius: 4px; background: rgba(9, 13, 16, .4); }
.skill-name { overflow: hidden; color: #cbd8dc; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.skill-row .x { min-height: 0; padding: 0 6px; border: none; background: transparent; color: var(--red); font-size: 11px; }
.mcp-label { flex: 1; color: var(--text-dim); font-size: 11px; align-self: center; }
.plan-row { display: flex; align-items: center; justify-content: flex-start; gap: 10px; color: var(--text-dim); font-size: 11px; }
.switch { position: relative; width: 34px; height: 18px; min-height: 0; padding: 0; border-radius: 9px; border: 1px solid var(--border); background: var(--panel-2); }
.switch .knob { position: absolute; top: 1px; left: 1px; width: 14px; height: 14px; border-radius: 50%; background: var(--text-muted); transition: left .15s, background .15s; }
.switch.on { background: rgba(85, 184, 200, .25); border-color: var(--accent); }
.switch.on .knob { left: 17px; background: var(--accent-strong); }
.ws-field { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--text-dim); }
.ws-field button { align-self: flex-start; min-height: 28px; padding: 4px 10px; font-size: 11px; }
.caps { display: flex; flex-wrap: wrap; gap: 6px 12px; }
.cap { flex-direction: row !important; align-items: center; gap: 4px; color: var(--text-dim); }
.cap input { width: auto; min-height: 0; }
.plan { flex-direction: row !important; align-items: center; gap: 6px; }
.plan input { width: auto; min-height: 0; }
.modal-actions { display: flex; justify-content: flex-end; gap: 7px; margin-top: 10px; }
</style>
