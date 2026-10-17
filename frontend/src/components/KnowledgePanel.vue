<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const emit = defineEmits(['notify'])

const stats = ref(null) // {enabled, path, files, chunks, last_scan, last_error}
const showPicker = ref(false)
const picker = ref({ path: '', parent: null, dirs: [] })
const trail = ref([]) // 目录栈（面包屑用）
const loading = ref(false)
const scanning = ref(false)

const statusText = computed(() => {
  if (!stats.value) return '加载中...'
  if (!stats.value.enabled) return '未配置 —— 选择一个本地文件夹作为知识库（.md / .txt），回复时自动检索参考资料'
  return `${stats.value.path}（${stats.value.files} 个文件 / ${stats.value.chunks} 块）`
})

async function refresh() {
  try { stats.value = await api.knowledge() } catch (e) { emit('notify', `知识库状态获取失败: ${e.message}`) }
}

async function openPicker() {
  showPicker.value = true
  trail.value = []
  await browse('')
}

async function browse(path) {
  loading.value = true
  try {
    picker.value = await api.listDirs(path)
  } catch (e) {
    emit('notify', `读取目录失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

async function enterDir(dir) {
  trail.value.push(picker.value.path)
  await browse(dir)
}

async function goBack() {
  if (!trail.value.length) return
  await browse(trail.value.pop())
}

function crumbLabel(p, i) {
  if (i === 0) return p === '' ? '此电脑' : p
  return p.split(/[\\/]/).filter(Boolean).pop() || p
}

function fmtTime(ms) {
  return ms ? new Date(ms).toLocaleString() : '-'
}

async function chooseCurrent() {
  const path = picker.value.path
  if (!path) { emit('notify', '请先进入一个文件夹'); return }
  await applyPath(path)
}

async function applyPath(path) {
  loading.value = true
  try {
    const data = await api.updateConfig({ knowledge_dir: path, persist: true })
    if (!data.changed.includes('knowledge_dir')) throw new Error('保存未生效')
    showPicker.value = false
    await refresh()
    emit('notify', `知识库已设置: ${path || '（已停用）'}`)
  } catch (e) {
    emit('notify', `设置失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

async function disable() {
  await applyPath('')
}

async function rescan() {
  scanning.value = true
  try {
    stats.value = await api.rescanKnowledge()
    emit('notify', `已重扫：${stats.value.files} 个文件 / ${stats.value.chunks} 块`)
  } catch (e) {
    emit('notify', `重扫失败: ${e.message}`)
  } finally {
    scanning.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <div class="card">
    <h3>知识库（RAG）</h3>
    <p class="status" :class="{ off: stats && !stats.enabled }">{{ statusText }}</p>
    <p v-if="stats && stats.enabled" class="meta">
      最近扫描: {{ fmtTime(stats.last_scan) }}
      <span v-if="stats.last_error" class="err">（{{ stats.last_error }}）</span>
    </p>
    <div class="btns">
      <button class="primary" @click="openPicker">
        {{ stats && stats.enabled ? '更换目录' : '选择目录' }}
      </button>
      <button v-if="stats && stats.enabled" :disabled="scanning" @click="rescan">
        {{ scanning ? '扫描中...' : '重新扫描' }}
      </button>
      <button v-if="stats && stats.enabled" class="danger" @click="disable">停用</button>
    </div>

    <div v-if="showPicker" class="mask" @click.self="showPicker = false">
      <div class="modal">
        <div class="modal-head">
          <strong>选择知识库文件夹</strong>
          <button class="x" @click="showPicker = false">✕</button>
        </div>
        <div class="crumbs">
          <template v-for="(p, i) in ['', ...trail, picker.path]" :key="i">
            <span v-if="i > 0" class="sep">›</span>
            <a @click.prevent="browse(p); trail = trail.slice(0, i - 1 < 0 ? 0 : i - 1)">{{ crumbLabel(p, i) }}</a>
          </template>
        </div>
        <div class="dirlist">
          <p v-if="loading" class="hint">加载中...</p>
          <p v-else-if="!picker.dirs.length" class="hint">没有子文件夹。可直接「选择此目录」。</p>
          <a v-for="d in picker.dirs" v-else :key="d" class="dir" @click.prevent="enterDir(d)">
            📁 {{ d.split(/[\\/]/).filter(Boolean).pop() }}
          </a>
        </div>
        <div class="modal-foot">
          <button v-if="trail.length || picker.path" @click="goBack">← 上级</button>
          <button class="primary" :disabled="!picker.path || loading" @click="chooseCurrent">
            选择此目录
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.status { font-size: 12px; color: var(--text-dim); margin: 8px 0 4px; line-height: 1.6; word-break: break-all; }
.status.off { color: #b45309; }
.meta { font-size: 11px; color: var(--text-dim); margin: 0 0 8px; }
.err { color: #dc2626; }
.btns { display: flex; gap: 6px; flex-wrap: wrap; }
.btns button { flex: 1; }
.danger { color: #dc2626; }
.mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { background: var(--card-bg, #fff); border-radius: 8px; width: min(520px, 92vw); max-height: 70vh; display: flex; flex-direction: column; padding: 14px; }
.modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.x { border: none; background: none; cursor: pointer; font-size: 14px; }
.crumbs { font-size: 12px; color: var(--text-dim); margin-bottom: 8px; word-break: break-all; }
.crumbs a { cursor: pointer; color: #2563eb; }
.crumbs .sep { margin: 0 4px; }
.dirlist { overflow-y: auto; flex: 1; min-height: 160px; border: 1px solid rgba(128,128,128,.25); border-radius: 6px; padding: 6px; }
.dir { display: block; padding: 5px 6px; border-radius: 4px; cursor: pointer; font-size: 13px; word-break: break-all; }
.dir:hover { background: rgba(37,99,235,.08); }
.modal-foot { display: flex; justify-content: space-between; gap: 8px; margin-top: 10px; }
.modal-foot button { flex: 1; }
.hint { font-size: 12px; color: var(--text-dim); margin: 6px; }
</style>
