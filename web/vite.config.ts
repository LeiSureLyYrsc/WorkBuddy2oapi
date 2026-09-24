import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 构建产物输出到 web/dist，由 FastAPI 静态文件路由托管。
// dev 模式下把 /api 代理到本地后端（7863 端口），前后端可分别热更新。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: './dist',
    emptyOutDir: true,
    // 单页应用，不需要 sourcemap 进产物（体积更小）。
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.WB2API_BACKEND || process.env.WBGUI_BACKEND || 'http://127.0.0.1:7863',
        changeOrigin: true,
      },
    },
  },
})
