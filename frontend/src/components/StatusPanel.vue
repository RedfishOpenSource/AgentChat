<script setup>
defineProps({
  status: { type: Object, default: null },
})
const emit = defineEmits(['action'])

const platformNames = { wechat: '微信', dingtalk: '钉钉' }

function modeLabel(mode) {
  return { mock: '模拟', wxauto: 'wxauto', cli: 'CLI' }[mode] || mode
}
</script>

<template>
  <div class="card">
    <h3>连接器状态</h3>
    <div v-if="!status" class="dim">加载中...</div>
    <div v-for="(conn, platform) in status?.connectors || {}" :key="platform" class="conn">
      <div class="row">
        <span class="tag" :class="platform">{{ platformNames[platform] || platform }}</span>
        <span class="mode">{{ modeLabel(conn.mode) }} · {{ conn.poll_interval }}s</span>
        <span class="dot" :class="conn.status" :title="conn.last_error"></span>
      </div>
      <div class="row">
        <span class="status-text" :class="conn.status">
          {{ { running: '运行中', stopped: '已停止', error: '异常' }[conn.status] || conn.status }}
        </span>
        <span class="spacer"></span>
        <button v-if="conn.status !== 'running'" class="primary sm" @click="emit('action', { platform, action: 'start' })">
          启动
        </button>
        <button v-else class="sm" @click="emit('action', { platform, action: 'stop' })">停止</button>
      </div>
      <div v-if="conn.last_error" class="err" :title="conn.last_error">{{ conn.last_error }}</div>
    </div>
  </div>
</template>

<style scoped>
.conn { margin: 0; padding: 10px 0; border: 0; border-top: 1px solid var(--border-soft); border-radius: 0; background: transparent; }
.row { display: flex; align-items: center; gap: 8px; }
.row + .row { margin-top: 8px; }
.mode { color: var(--text-muted); font-size: 10px; }
.spacer { flex: 1; }
.dot { width: 7px; height: 7px; margin-left: auto; border-radius: 50%; }
.dot.running { background: var(--green); box-shadow: 0 0 8px rgba(85, 201, 138, .4); }
.dot.stopped { background: var(--text-muted); }
.dot.error { background: var(--red); box-shadow: 0 0 8px rgba(233, 121, 112, .4); }
.status-text { color: var(--text-dim); font-size: 11px; }
.status-text.error { color: var(--red); }
.sm { min-height: 28px; padding: 4px 9px; font-size: 10px; }
.err { margin-top: 7px; overflow: hidden; color: var(--red); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.dim { color: var(--text-dim); font-size: 11px; }
</style>
