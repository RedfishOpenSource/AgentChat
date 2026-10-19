<script setup>
import { computed, reactive, ref, watch } from 'vue'

const props = defineProps({
  messages: { type: Array, default: () => [] },
  loadingOlder: { type: Boolean, default: false },
  hasOlder: { type: Boolean, default: true },
})
const emit = defineEmits(['action', 'select-candidate', 'load-older'])

const filter = ref('all')
const contact = ref('')
const drafts = reactive({})  // 待审核回复的草稿编辑框内容（填入输入框，可修改后发送）
const selected = reactive({}) // 每条回复当前选中的候选下标
const platformNames = { wechat: '微信', dingtalk: '钉钉' }

watch(filter, () => { contact.value = '' })

const platformScoped = computed(() =>
  ['wechat', 'dingtalk'].includes(filter.value) ? filter.value : '')

const contactOptions = computed(() => {
  const out = []
  for (const m of props.messages) {
    if (platformScoped.value && m.platform !== platformScoped.value) continue
    if (m.chat_id && !out.includes(m.chat_id)) out.push(m.chat_id)
  }
  return out
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
    (filter.value === 'all'
      || (filter.value === 'pending' && ['pending', 'blocked', 'failed', 'unknown', 'sending'].includes(m.status))
      || m.platform === filter.value)
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
      <button :class="{ active: filter === 'all' }" @click="filter = 'all'">全部</button>
      <button :class="{ active: filter === 'pending' }" @click="filter = 'pending'">待处理</button>
      <button :class="{ active: filter === 'wechat' }" @click="filter = 'wechat'">微信</button>
      <button :class="{ active: filter === 'dingtalk' }" @click="filter = 'dingtalk'">钉钉</button>
      <select v-if="platformScoped && contactOptions.length" v-model="contact" class="contact-select">
        <option value="">全部联系人（{{ contactOptions.length }}）</option>
        <option v-for="c in contactOptions" :key="c" :value="c">{{ c }}</option>
      </select>
    </div>

    <div class="stream">
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
            <span class="time">{{ fmt(m.ts) }}</span>
            <span v-if="statusNames[m.status]" class="tag" :class="m.status">{{ statusNames[m.status] }}</span>
          </div>
          <div class="content" v-if="!['pending', 'blocked', 'failed', 'unknown', 'sending'].includes(m.status)">{{ m.content }}</div>
          <div v-if="m.status === 'ignored' && m.meta?.ignore_reason" class="ignore-reason">
            ⏭ {{ m.meta.ignore_reason }}
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

          <!-- 候选与 JEV 信息 -->
          <details v-if="m.meta && (m.meta.candidates?.length || m.meta.assessment)" class="meta">
            <summary>生成详情（候选 / JEV 判断）</summary>
            <div v-if="m.meta.candidates?.length" class="cands">
              <div v-for="(c, i) in m.meta.candidates" :key="i" class="cand" :class="{ chosen: c.content === m.content }">
                <span class="idx">{{ i + 1 }}</span>
                <span>{{ c.content }}</span>
                <span v-if="c.passed !== null && c.passed !== undefined" class="pass" :class="{ ok: c.passed, bad: !c.passed }">
                  {{ c.passed ? '✓' : '✗' }}{{ c.reason ? ' ' + c.reason : '' }}
                </span>
              </div>
            </div>
            <pre v-if="m.meta.assessment" class="assess">{{ JSON.stringify(m.meta.assessment, null, 2) }}</pre>
            <div v-if="m.meta.blocked_reason" class="blocked-reason">🚫 {{ m.meta.blocked_reason }}</div>
          </details>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.stream-wrap { display: flex; flex: 1; min-height: 0; flex-direction: column; overflow: hidden; padding: 0 20px 18px; }
.filters { display: flex; align-items: center; gap: 5px; padding: 4px 0 12px; }
.filters button { min-height: 29px; padding: 5px 10px; border-color: transparent; background: transparent; color: var(--text-dim); font-size: 11px; }
.filters button.active { border-color: rgba(85, 184, 200, .2); background: rgba(85, 184, 200, .09); color: var(--accent-strong); }
.contact-select { width: auto; min-height: 29px; padding: 4px 8px; font-size: 11px; margin-left: auto; }
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
@media (max-width: 700px) {
  .stream-wrap { padding: 0 10px 12px; }
  .stream { padding: 10px 7px; }
  .bubble, .bubble:has(.draft-box) { width: 100%; max-width: 100%; min-width: 0; }
  .time { margin-left: 0; }
  .meta-line { gap: 5px; }
}
</style>
