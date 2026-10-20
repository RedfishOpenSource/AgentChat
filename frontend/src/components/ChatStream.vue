<script setup>
import { computed, nextTick, onUnmounted, reactive, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  processing: { type: Object, default: () => ({}) },
  contacts: { type: Object, default: () => ({}) },
  connected: { type: Boolean, default: true },
  loadingOlder: { type: Boolean, default: false },
  hasOlder: { type: Boolean, default: true },
})
const emit = defineEmits(['action', 'select-candidate', 'load-older', 'regenerate', 'toast', 'message'])

const filter = ref('wechat')
const contact = ref('')
const drafts = reactive({})  // 待审核回复的草稿编辑框内容（填入输入框，可修改后发送）
const selected = reactive({}) // 每条回复当前选中的候选下标
const platformNames = { wechat: '微信', dingtalk: '钉钉' }

// 联系人下拉：可搜索
const csOpen = ref(false)
const csKeyword = ref('')
const streamEl = ref(null)
const suppOpenId = ref('')
const suppText = ref('')
const composerDrafts = reactive({})
const submitting = reactive({})
const resetting = reactive({})
const currentContact = computed(() => contact.value || (contactOptions.value.length === 1 ? contactOptions.value[0] : ''))
const conversationKey = computed(() => JSON.stringify([filter.value, currentContact.value]))
const composerText = computed({
  get: () => composerDrafts[conversationKey.value] || '',
  set: value => { composerDrafts[conversationKey.value] = value },
})
const conversationBusy = computed(() => !!submitting[conversationKey.value] || Object.values(props.processing).some(
  p => p.platform === filter.value && p.chat_id === currentContact.value && !p.finished_at,
))
const resetBusy = computed(() => !!resetting[conversationKey.value])

async function resetCurrentSession() {
  const platform = filter.value, chatId = currentContact.value, key = conversationKey.value
  if (!chatId || conversationBusy.value || resetBusy.value) return
  if (!window.confirm(`清空「${chatId}」的 AI 会话？\n下一条消息或补充提问将开启新会话，聊天记录仍保留。`)) return
  resetting[key] = true
  try {
    await api.resetSession(platform, chatId)
    emit('toast', `「${chatId}」的会话已清空，下一条消息将开启新会话`)
  } catch (e) { emit('toast', `清空失败：${e.message}`) }
  finally { delete resetting[key] }
}

async function sendToAI() {
  const platform = filter.value, chatId = currentContact.value, key = conversationKey.value
  const content = composerText.value.trim()
  if (!chatId || !content || conversationBusy.value || resetBusy.value) return
  submitting[key] = true
  try {
    const data = await api.contactChat(platform, chatId, content)
    emit('message', data.message)
    if (data.reply) emit('message', data.reply)
    if (composerDrafts[key]?.trim() === content) composerDrafts[key] = ''
    emit('toast', data.reply?.meta?.error ? 'AI 生成失败，请查看消息中的原因' : 'AI 回复已生成，请审核草稿')
  } catch (e) { emit('toast', `发送给 AI 失败：${e.message}`) }
  finally { delete submitting[key] }
}

function onComposerKey(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    sendToAI()
  }
}

const now = ref(Date.now())
const ticker = setInterval(() => { now.value = Date.now() }, 1000)
onUnmounted(() => clearInterval(ticker))

const phaseNames = {
  merging: '等待合并消息', preparing: '准备上下文', queued: '排队中',
  starting: '连接 Agent', session: '创建会话', thinking: 'AI 思考中',
  tool: '执行工具', writing: '生成回复', completed: '处理完成',
  failed: '生成失败', cancelled: '已中断', skipped: '已跳过',
}
function progressOf(m) { return props.processing[m.id] || m.meta?.processing }
function elapsed(p) {
  return Math.max(0, Math.floor(((p.finished_at || now.value) - p.since) / 1000))
}
function progressHint(p) {
  if (p.finished_at) return p.detail
  if (!props.connected) return '实时连接已断开，正在重连；当前进度可能延迟'
  if (p.phase === 'merging') return `等待 ${p.wait || 0} 秒内无新消息后开始生成`
  if (p.phase === 'queued') return '同一 Agent 正在处理其他消息，请稍候'
  if (now.value - p.updated_at >= 15000) {
    return `已 ${Math.floor((now.value - p.updated_at) / 1000)} 秒没有新进度，仍在等待 Agent 响应${p.timeout ? `（最长等待 ${p.timeout} 秒）` : ''}`
  }
  return p.detail
}

watch(filter, () => { contact.value = '' })

// 新消息到达时自动滚动到底部（加载更早消息不触发）
watch(
  () => props.messages.at(-1)?.id,
  (id, old) => {
    if (id && id !== old) scrollToBottom()
  },
)

function scrollToBottom() {
  nextTick(() => {
    const el = streamEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function toggleSupp(id) {
  suppOpenId.value = suppOpenId.value === id ? '' : id
  suppText.value = ''
}

function submitSupp(m) {
  const extra = suppText.value.trim()
  if (!extra) return
  emit('regenerate', { id: m.id, extra })
  suppOpenId.value = ''
  suppText.value = ''
}

const platformScoped = computed(() =>
  ['wechat', 'dingtalk'].includes(filter.value) ? filter.value : '')

const contactOptions = computed(() => {
  const out = [...(props.contacts[filter.value] || []).filter(c => c !== '*')]
  for (const m of props.messages) {
    if (platformScoped.value && m.platform !== platformScoped.value) continue
    if (m.chat_id && !out.includes(m.chat_id)) out.push(m.chat_id)
  }
  return out
})

const filteredContactOptions = computed(() => {
  const k = csKeyword.value.trim().toLowerCase()
  if (!k) return contactOptions.value
  return contactOptions.value.filter((c) => c.toLowerCase().includes(k))
})

const statusNames = { pending: '草稿·待确认', sending: '发送中', sent: '已发送', failed: '发送失败', unknown: '结果待核实', blocked: '已拦截', rejected: '已驳回', ignored: '已忽略', received: '' }

function sendDraft(m) {
  const confirmUnknown = m.status === 'unknown'
  if (confirmUnknown && !window.confirm('上次发送结果不确定。请先核对平台会话，确认未送达后再重试。继续？')) return
  emit('action', { id: m.id, action: 'approve', content: drafts[m.id] ?? m.content, confirmUnknown })
}

function activeIndex(m) {
  if (m.id in selected) return selected[m.id]
  const cands = m.meta?.candidates || []
  return cands.findIndex((c) => c.content === (drafts[m.id] ?? m.content))
}

function pickCandidate(m, i) {
  selected[m.id] = i
  delete drafts[m.id]
  emit('select-candidate', { id: m.id, index: i })
}

const filtered = computed(() =>
  props.messages.filter((m) =>
    m.platform === filter.value
    && (!contact.value || m.chat_id === contact.value))
)

function fmt(ts) {
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
}

function shortChat(id) {
  return id && id.length > 12 ? id.slice(0, 12) + '…' : id
}
</script>

<template>
  <div class="stream-wrap">
    <div class="filters">
      <button :class="{ active: filter === 'wechat' }" @click="filter = 'wechat'">微信</button>
      <button :class="{ active: filter === 'dingtalk' }" @click="filter = 'dingtalk'">钉钉</button>
      <div v-if="platformScoped && contactOptions.length" class="contact-select">
        <button class="cs-btn" :title="contact || '全部联系人'" @click="csOpen = !csOpen">
          {{ contact || `全部联系人（${contactOptions.length}）` }} ▾
        </button>
        <div v-if="csOpen" class="cs-mask" @click="csOpen = false"></div>
        <div v-if="csOpen" class="cs-pop">
          <input v-model="csKeyword" placeholder="搜索联系人…" autofocus />
          <div class="cs-list">
            <div class="cs-item" :class="{ active: !contact }" @click="contact = ''; csOpen = false">全部联系人</div>
            <div
              v-for="c in filteredContactOptions"
              :key="c"
              class="cs-item"
              :class="{ active: contact === c }"
              @click="contact = c; csOpen = false"
            >{{ c }}</div>
            <div v-if="!filteredContactOptions.length" class="cs-empty">无匹配联系人</div>
          </div>
        </div>
      </div>
      <button
        class="reset-session"
        :disabled="!currentContact || conversationBusy || resetBusy"
        :title="!currentContact ? '请先选择联系人' : conversationBusy ? '请等待当前处理完成后清空' : `清空「${currentContact}」的 AI 会话`"
        @click="resetCurrentSession"
      >{{ resetBusy ? '清空中…' : '清空会话' }}</button>
    </div>

    <div ref="streamEl" class="stream">
      <button v-if="hasOlder" class="load-older" :disabled="loadingOlder" @click="emit('load-older')">
        {{ loadingOlder ? '加载中…' : '加载更早消息' }}
      </button>
      <div v-if="!filtered.length" class="empty">
        <p>暂无匹配消息</p>
        <p class="hint">新消息到达后会自动生成回复草稿，可在此按渠道与联系人筛选。</p>
      </div>

      <div
        v-for="m in filtered"
        :key="m.id"
        class="bubble-row"
        :class="m.direction === 'in' ? 'left' : 'right'"
      >
        <div class="bubble" :class="m.direction">
          <div class="meta-line">
            <span class="platform-mark" :class="m.platform"></span>
            <span class="sender">{{ m.sender || (m.direction === 'in' ? '对方' : 'ChatAgent') }}</span>
            <span class="tag" :class="m.platform">{{ platformNames[m.platform] || m.platform }}</span>
            <span v-if="m.chat_id" class="chat-id">{{ shortChat(m.chat_id) }}</span>
            <span v-if="m.meta?.operator_input" class="tag">补充给 AI</span>
            <span class="time">{{ fmt(m.ts) }}</span>
            <span v-if="statusNames[m.status]" class="tag" :class="m.status">{{ statusNames[m.status] }}</span>
          </div>
          <div class="content" v-if="!['pending', 'blocked', 'failed', 'unknown', 'sending'].includes(m.status)">{{ m.content }}</div>
          <div v-if="m.status === 'ignored' && m.meta?.ignore_reason" class="ignore-reason">
            ⏭ {{ m.meta.ignore_reason }}
          </div>

          <div
            v-if="m.direction === 'in' && progressOf(m)"
            class="ai-progress"
            :class="{ failed: progressOf(m).phase === 'failed' }"
            role="status"
          >
            <div class="progress-head">
              <span v-if="!progressOf(m).finished_at" class="spinner" aria-hidden="true"></span>
              <strong>{{ phaseNames[progressOf(m).phase] || '处理中' }}</strong>
              <span v-if="progressOf(m).agent">{{ progressOf(m).agent }}</span>
              <span class="elapsed">{{ progressOf(m).finished_at ? '用时' : '已用时' }} {{ elapsed(progressOf(m)) }} 秒</span>
            </div>
            <p class="progress-hint">{{ progressHint(progressOf(m)) }}</p>
            <details class="progress-details">
              <summary>查看执行进度{{ progressOf(m).preview ? '与回复预览' : '' }}</summary>
              <ol>
                <li v-for="(step, i) in progressOf(m).steps" :key="i">
                  <time>{{ fmt(step.ts) }}</time>
                  <span>{{ step.detail || phaseNames[step.phase] }}</span>
                </li>
              </ol>
              <div v-if="progressOf(m).finished_at && progressOf(m).preview" class="reply-preview">{{ progressOf(m).preview }}</div>
            </details>
            <div v-if="!progressOf(m).finished_at && progressOf(m).preview" class="live-preview">{{ progressOf(m).preview }}</div>
          </div>
          <div v-if="m.meta?.error" class="blocked-reason">{{ m.meta.error }}</div>

          <!-- 收到的消息：补充说明后重新生成回复 -->
          <div v-if="m.direction === 'in'" class="supp">
            <button class="chip-btn" @click="toggleSupp(m.id)">✎ 补充并重新生成</button>
            <div v-if="suppOpenId === m.id" class="supp-box">
              <textarea v-model="suppText" rows="2" placeholder="补充背景 / 约束后重新生成回复…" />
              <button class="sm primary" :disabled="!suppText.trim()" @click="submitSupp(m)">重新生成</button>
            </div>
          </div>

          <!-- 待审核/已拦截 → 草稿输入框：生成内容填入，可编辑，人工确认后才发送 -->
          <div v-if="['pending', 'blocked', 'failed', 'unknown'].includes(m.status)" class="draft-box">
            <div v-if="(m.meta?.candidates?.length || 0) > 1" class="cand-chips">
              <button
                v-for="(c, i) in m.meta.candidates"
                :key="i"
                class="chip-btn"
                :class="{ active: activeIndex(m) === i }"
                :title="c.content"
                @click="pickCandidate(m, i)"
              >
                候选{{ i + 1 }}
              </button>
            </div>
            <textarea
              class="draft"
              rows="3"
              :value="drafts[m.id] ?? m.content"
              @input="drafts[m.id] = $event.target.value"
              placeholder="草稿内容（可编辑）"
            ></textarea>
            <div class="actions">
              <button class="success sm" @click="sendDraft(m)">
                {{ m.status === 'blocked' ? '忽略拦截，确认发送' : (m.status === 'failed' || m.status === 'unknown' ? '重试发送' : '✓ 确认发送') }}
              </button>
              <button class="danger sm" @click="emit('action', { id: m.id, action: 'reject' })">✕ 驳回</button>
            </div>
            <div v-if="m.meta?.blocked_reason" class="blocked-reason">🚫 {{ m.meta.blocked_reason }}</div>
            <div v-if="m.meta?.dispatch_error" class="blocked-reason">{{ m.meta.dispatch_error }}</div>
          </div>

          <!-- 候选列表 -->
          <details v-if="m.meta && m.meta.candidates?.length" class="meta">
            <summary>生成详情（候选）</summary>
            <div v-if="m.meta.candidates?.length" class="cands">
              <div v-for="(c, i) in m.meta.candidates" :key="i" class="cand" :class="{ chosen: c.content === m.content }">
                <span class="idx">{{ i + 1 }}</span>
                <span>{{ c.content }}</span>
              </div>
            </div>
          </details>
        </div>
      </div>
    </div>

    <form class="composer" @submit.prevent="sendToAI">
      <div class="composer-title">{{ currentContact ? `补充给 AI · ${currentContact}` : '请先在右上角选择一个联系人' }}</div>
      <textarea
        v-model="composerText"
        aria-label="补充发送给 AI"
        rows="3"
        maxlength="20000"
        :disabled="!currentContact || !!submitting[conversationKey] || resetBusy"
        :placeholder="currentContact ? '补充背景、调整要求，或继续向当前对话的 AI 提问…' : '选择联系人后，即可继续该对话'"
        @keydown="onComposerKey"
      ></textarea>
      <div class="composer-actions">
        <span>{{ conversationBusy ? 'AI 正在处理当前对话，请稍候…' : 'Enter 发送 · Shift + Enter 换行 · 回复生成后需审核' }}</span>
        <button class="primary" type="submit" :disabled="!currentContact || !composerText.trim() || conversationBusy || resetBusy">
          {{ submitting[conversationKey] ? 'AI 处理中…' : '发送给 AI' }}
        </button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.stream-wrap { display: flex; flex: 1; min-height: 0; flex-direction: column; overflow: hidden; padding: 0 20px 18px; }
.filters { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; padding: 4px 0 12px; }
.filters button { min-height: 29px; padding: 5px 10px; border-color: transparent; background: transparent; color: var(--text-dim); font-size: 11px; }
.filters .reset-session { flex: none; border-color: var(--border); background: var(--panel-2); color: var(--text-dim); }
.composer { flex: none; display: flex; flex-direction: column; gap: 8px; margin-top: 10px; padding: 10px 12px; border: 1px solid var(--border-soft); border-radius: 7px; background: var(--panel); }
.composer-title { font-size: 11px; color: var(--text-dim); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.composer textarea { width: 100%; min-height: 65px; max-height: 180px; resize: vertical; font-family: inherit; font-size: 13px; line-height: 1.5; }
.composer-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.composer-actions span { color: var(--text-muted); font-size: 10px; }
.composer-actions button { flex: none; min-height: 30px; font-size: 11px; }
.filters button.active { border-color: rgba(85, 184, 200, .2); background: rgba(85, 184, 200, .09); color: var(--accent-strong); }
.contact-select { position: relative; margin-left: auto; }
.cs-btn { min-height: 29px; max-width: min(240px, 40vw); padding: 4px 10px; overflow: hidden; background: var(--panel-2); border-color: var(--border); color: var(--text-dim); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.cs-mask { position: fixed; inset: 0; z-index: 40; }
.cs-pop {
  position: absolute; top: calc(100% + 4px); right: 0; z-index: 50;
  width: 240px; display: flex; flex-direction: column;
  padding: 8px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--panel); box-shadow: var(--shadow);
}
.cs-pop input { min-height: 28px; padding: 4px 8px; font-size: 11px; margin-bottom: 6px; }
.cs-list { max-height: 240px; overflow-y: auto; }
.cs-item { padding: 6px 8px; border-radius: 4px; color: #cbd8dc; cursor: pointer; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cs-item:hover { background: rgba(85, 184, 200, .08); }
.cs-item.active { color: var(--accent-strong); background: rgba(85, 184, 200, .12); }
.cs-empty { padding: 10px; color: var(--text-muted); font-size: 11px; text-align: center; }
.supp { margin-top: 6px; }
.supp-box { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; padding: 8px; border: 1px solid var(--border-soft); border-radius: 5px; background: rgba(9, 13, 16, .5); }
.supp-box textarea { min-height: 48px; font-size: 12px; }
.supp-box .sm { align-self: flex-end; min-height: 26px; padding: 3px 10px; font-size: 11px; }
.stream { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 11px; padding: 12px clamp(2px, 2vw, 24px); border: 1px solid var(--border-soft); border-radius: 7px; background: rgba(12, 17, 22, .52); }
.load-older { align-self: center; flex: 0 0 auto; min-height: 29px; padding: 4px 10px; border-color: transparent; background: transparent; color: var(--text-dim); font-size: 11px; }
.empty { margin: auto; text-align: center; color: var(--text-dim); }
.empty p:first-child { color: #d2e0e4; font-size: 14px; font-weight: 600; }
.empty .hint { margin-top: 6px; color: var(--text-muted); font-size: 11px; }
.bubble-row { display: flex; width: 100%; }
.bubble-row.left { justify-content: flex-start; }
.bubble-row.right { justify-content: flex-end; }
.bubble {
  width: fit-content;
  max-width: min(74%, 760px);
  min-width: min(310px, 88%);
  padding: 12px 14px;
  border: 1px solid var(--border-soft);
  border-radius: 7px;
  background: linear-gradient(145deg, rgba(25, 34, 42, .96), rgba(20, 27, 34, .96));
  box-shadow: 0 5px 18px rgba(0, 0, 0, .12);
}
.bubble.out { border-color: rgba(85, 184, 200, .25); background: linear-gradient(145deg, rgba(24, 43, 48, .95), rgba(19, 34, 39, .96)); }
.bubble:has(.draft-box) { width: min(720px, 86%); min-width: min(360px, 92%); border-color: rgba(230, 173, 98, .35); background: linear-gradient(145deg, rgba(45, 39, 30, .74), rgba(28, 31, 32, .98)); }
.meta-line { display: flex; align-items: center; gap: 7px; min-height: 20px; margin-bottom: 8px; flex-wrap: wrap; }
.platform-mark { width: 6px; height: 6px; border-radius: 50%; background: var(--text-muted); }
.platform-mark.wechat { background: var(--green); box-shadow: 0 0 8px rgba(85, 201, 138, .36); }
.platform-mark.dingtalk { background: var(--blue); box-shadow: 0 0 8px rgba(108, 169, 232, .36); }
.sender { color: #e2ecef; font-size: 12px; font-weight: 650; }
.chat-id { max-width: 210px; overflow: hidden; color: var(--text-muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.time { margin-left: auto; color: var(--text-muted); font-size: 10px; font-variant-numeric: tabular-nums; }
.content { color: #dce6e9; font-size: 13px; line-height: 1.65; word-break: break-word; white-space: pre-wrap; }
.draft-box { margin-top: 9px; padding: 11px; border: 1px solid rgba(230, 173, 98, .17); border-radius: 5px; background: rgba(9, 13, 16, .36); }
.cand-chips { display: flex; gap: 5px; margin-bottom: 9px; flex-wrap: wrap; }
.chip-btn { min-height: 26px; padding: 4px 8px; border-radius: 4px; background: var(--panel-2); color: var(--text-dim); font-size: 10px; }
.chip-btn.active { border-color: var(--accent); background: rgba(85, 184, 200, .12); color: var(--accent-strong); }
.draft { min-height: 72px; background: rgba(11, 15, 19, .72); border-color: #35434a; border-radius: 4px; color: #eff6f7; font-size: 13px; line-height: 1.55; padding: 9px 10px; resize: vertical; font-family: inherit; }
.draft:focus { border-color: var(--accent); }
.actions { display: flex; gap: 7px; margin-top: 9px; }
.actions .sm { min-height: 31px; padding: 6px 10px; font-size: 11px; }
.actions .success { margin-right: auto; }
.meta { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border-soft); font-size: 11px; }
.meta summary { color: var(--text-dim); cursor: pointer; }
.cands { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }
.cand { display: flex; align-items: baseline; gap: 7px; padding: 6px 8px; border-radius: 4px; background: rgba(11, 15, 19, .45); color: #cbd8dc; }
.cand.chosen { outline: 1px solid rgba(85, 184, 200, .45); }
.idx { color: var(--text-muted); font-variant-numeric: tabular-nums; }
.pass.ok { color: var(--green); }
.pass.bad { color: var(--red); }
.assess { margin-top: 8px; padding: 9px; border: 1px solid var(--border-soft); border-radius: 4px; background: rgba(11, 15, 19, .54); color: var(--text-dim); font-size: 10px; overflow-x: auto; }
.blocked-reason { margin-top: 7px; color: var(--red); font-size: 11px; }
.ignore-reason { margin-top: 5px; color: var(--text-muted); font-size: 10px; }
.ai-progress { margin-top: 12px; padding: 10px 12px; border: 1px solid rgba(85, 184, 200, .28); border-radius: 6px; background: rgba(85, 184, 200, .05); font-size: 12px; }
.progress-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; color: var(--accent-strong); }
.elapsed { margin-left: auto; color: var(--text-dim); font-size: 11px; font-variant-numeric: tabular-nums; }
.progress-hint { margin: 7px 0 0; color: var(--text-dim); line-height: 1.5; overflow-wrap: anywhere; }
.progress-details { margin-top: 8px; color: var(--text-dim); font-size: 11px; }
.progress-details summary { cursor: pointer; }
.progress-details ol { display: flex; flex-direction: column; gap: 6px; padding-left: 18px; max-height: 180px; overflow-y: auto; }
.progress-details time { margin-right: 8px; color: var(--text-muted); }
.progress-details li { overflow-wrap: anywhere; }
.live-preview, .reply-preview { margin-top: 8px; max-height: 160px; overflow-y: auto; white-space: pre-wrap; overflow-wrap: anywhere; color: #dce6e9; line-height: 1.6; }
.ai-progress.failed { border-color: rgba(233, 121, 112, .4); }
.ai-progress.failed .progress-head { color: var(--red); }
.spinner { flex: none; width: 12px; height: 12px; border: 2px solid rgba(85, 184, 200, .2); border-top-color: var(--accent-strong); border-radius: 50%; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }
@media (max-width: 700px) {
  .stream-wrap { padding: 0 10px 12px; }
  .stream { padding: 10px 7px; }
  .bubble, .bubble:has(.draft-box) { width: 100%; max-width: 100%; min-width: 0; }
  .time { margin-left: 0; }
  .meta-line { gap: 5px; }
}
</style>
