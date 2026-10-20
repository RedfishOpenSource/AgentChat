import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时把 /api 与 /ws 代理到后端
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8318', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8318', ws: true },
    },
  },
})
