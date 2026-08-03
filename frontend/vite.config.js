import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// GitHub Pages base path; defaults to "/firefly-studio/" matching the repo name.
// Override via env in CI: VITE_PAGES_BASE=/your-fork/
const pagesBase = (process.env.VITE_PAGES_BASE || '/firefly-studio/').replace(/\/?$/, '/')

export default defineConfig({
  plugins: [react()],
  base: pagesBase,
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    // 允许 LAN / 任意 host 访问, 方便手机调试
    allowedHosts: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:7860',
        changeOrigin: true,
      },
      '/outputs': {
        target: 'http://127.0.0.1:7860',
        changeOrigin: true,
      }
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
})
