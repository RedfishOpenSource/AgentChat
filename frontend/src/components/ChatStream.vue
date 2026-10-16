<script setup>
import { computed, reactive, ref } from 'vue'

const props = defineProps({
  messages: { type: Array, default: () => [] },
})
const emit = defineEmits(['action'])

const filter = ref('all')
const drafts = reactive({})  // 待审核回复的草稿编辑框内容（填入输入框，可修改后发送）
const selected = reactive({}) // 每条回复当前选中的候选下标
const platformNames = { wechat: '微信', dingtalk: '钉钉' }
const statusNames = { pending: '草稿·待确认', sent: '已发送', blocked: '已拦截', rejected: '已驳回', ignored: '已忽略', received: '' }

function sendDraft(m) {
  emit('action', { id: m.id, action: 'approve', content: drafts[m.id] ?? m.content })
}

function activeIndex(m) {
  if (m.id in selected) return selected[m.id]
  const cands = m.meta?.candidates || []
  return cands.findIndex((c) => c.content === (drafts[m.id] ?? m.content))
}

function pickCandidate(m, i) {
  selected[m.id] = i
  drafts[m.id] = m.meta.candidates[i].content
}

const filtered = computed(() =>
  props.messages.filter((m) => filter.value === 'all' || m.platform === filter.value)
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
      <button :class="{ active: filter === 'wechat' }" @click="filter = 'wechat'">微信</button>
      <button :class="{ active: filter === 'dingtalk' }" @click="filter = 'dingtalk'">钉钉</button>
    </div>

    <div class="stream">
      <div v-if="!filtered.length" class="empty">
        <p>暂无消息</p>
        <p class="hint">在左侧“模拟新消息”面板注入一条微信/钉钉消息，即可看到自动回复流程。</p>
      </div>

      <div
        v-for="m in filtered"
        :key="m.id"
        class="bubble-row"
        :class="m.direction === 'in' ? 'left' : 'right'"
      >
        <div class="bubble" :class="m.direction">
          <div class="meta-line">
            <span class="tag" :class="m.platform">{{ platformNames[m.platform] || m.platform }}</span>
            <span class="sender">{{ m.sender }}</span>
            <span v-if="m.chat_id" class="chat-id">会话: {{ shortChat(m.chat_id) }}</span>
            <span class="time">{{ fmt(m.ts) }}</span>
            <span v-if="statusNames[m.status]" class="tag" :class="m.status">{{ statusNames[m.status] }}</span>
          </div>
          <div class="content" v-if="m.status !== 'pending' && m.status !== 'blocked'">{{ m.content }}</div>
          <div v-if="m.status === 'ignored' && m.meta?.ignore_reason" class="ignore-reason">
            ⏭ {{ m.meta.ignore_reason }}
          </div>

          <!-- 待审核/已拦截 → 草稿输入框：生成内容填入，可编辑，人工确认后才发送 -->
          <div v-if="m.status === 'pending' || m.status === 'blocked'" class="draft-box">
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
                {{ m.status === 'blocked' ? '忽略拦截，确认发送' : '✓ 确认发送' }}
              </button>
              <button class="danger sm" @click="emit('action', { id: m.id, action: 'reject' })">✕ 驳回</button>
            </div>
            <div v-if="m.meta?.blocked_reason" class="blocked-reason">🚫 {{ m.meta.blocked_reason }}</div>
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
.stream-wrap { display: flex; flex-direction: column; flex: 1; overflow: hidden; padding: 0 16px 16px; }
.filters { display: flex; gap: 6px; padding: 8px 0; }
.filters button { padding: 4px 14px; font-size: 12px; border-radius: 14px; background: transparent; color: var(--text-dim); }
.filters button.active { background: var(--panel-2); color: var(--text); }
.stream { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding: 4px 2px; }
.empty { margin: auto; text-align: center; color: var(--text-dim); }
.empty .hint { font-size: 12px; margin-top: 6px; }
.bubble-row { display: flex; }
.bubble-row.left { justify-content: flex-start; }
.bubble-row.right { justify-content: flex-end; }
.bubble {
  max-width: 72%;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 12px;
}
.bubble.out { background: #1c2a3f; border-color: #2b3f5e; }
.meta-line { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.sender { font-size: 12px; font-weight: 600; }
.chat-id { font-size: 11px; color: var(--text-dim); }
.time { font-size: 11px; color: var(--text-dim); margin-left: auto; }
.content { line-height: 1.6; word-break: break-word; white-space: pre-wrap; }
.draft-box { margin-top: 8px; }
.cand-chips { display: flex; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
.chip-btn {
  padding: 2px 10px;
  font-size: 11px;
  border-radius: 10px;
  background: var(--panel-2);
  color: var(--text-dim);
}
.chip-btn.active { border-color: var(--accent); color: var(--accent); }
.draft {
  width: 100%;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 7px;
  color: var(--text);
  font-size: 13px;
  line-height: 1.5;
  padding: 7px 9px;
  resize: vertical;
  font-family: inherit;
}
.draft:focus { outline: none; border-color: var(--accent); }
.actions { display: flex; gap: 8px; margin-top: 8px; }
.sm { padding: 4px 12px; font-size: 12px; }
.meta { margin-top: 8px; font-size: 12px; }
.meta summary { cursor: pointer; color: var(--text-dim); }
.cands { margin-top: 6px; display: flex; flex-direction: column; gap: 4px; }
.cand {
  display: flex; gap: 6px; align-items: baseline;
  padding: 4px 8px; border-radius: 6px; background: var(--panel-2);
}
.cand.chosen { outline: 1px solid var(--accent); }
.idx { color: var(--text-dim); }
.pass.ok { color: var(--green); }
.pass.bad { color: var(--red); }
.assess {
  margin-top: 6px; padding: 8px; border-radius: 6px;
  background: var(--panel-2); color: var(--text-dim);
  font-size: 11px; overflow-x: auto;
}
.blocked-reason { margin-top: 6px; color: var(--red); font-size: 12px; }
.ignore-reason { margin-top: 4px; color: var(--text-dim); font-size: 11px; opacity: 0.8; }
</style>
