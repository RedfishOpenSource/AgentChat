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
.conn {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 8px;
  background: var(--panel-2);
}
.row { display: flex; align-items: center; gap: 8px; }
.row + .row { margin-top: 8px; }
.mode { font-size: 12px; color: var(--text-dim); }
.spacer { flex: 1; }
.dot { width: 9px; height: 9px; border-radius: 50%; margin-left: auto; }
.dot.running { background: var(--green); box-shadow: 0 0 6px var(--green); }
.dot.stopped { background: var(--text-dim); }
.dot.error { background: var(--red); box-shadow: 0 0 6px var(--red); }
.status-text { font-size: 12px; color: var(--text-dim); }
.status-text.error { color: var(--red); }
.sm { padding: 3px 10px; font-size: 12px; }
.err {
  margin-top: 8px;
  font-size: 11px;
  color: var(--red);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dim { color: var(--text-dim); font-size: 12px; }
</style>
