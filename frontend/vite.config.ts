import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  plugins: [vue({ template: { transformAssetUrls: { includeAbsolute: false } } })],
  base: '/app/ui/',
  build: {
    outDir: fileURLToPath(new URL('../src/treg/web/dashboard', import.meta.url)),
    emptyOutDir: true,
    rolldownOptions: {
      // The Dashboard, plus the standalone pages served by the API (`routers/web.page_entry`).
      input: {
        index: fileURLToPath(new URL('index.html', import.meta.url)),
        apps: fileURLToPath(new URL('apps.html', import.meta.url)),
        vibe: fileURLToPath(new URL('vibe.html', import.meta.url)),
      },
      output: { codeSplitting: { groups: [{ name: 'vue', test: /node_modules\/(?:@vue|vue)\// }] } },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '^/(?!app/ui/)': { target: 'http://127.0.0.1:18790', changeOrigin: false },
    },
  },
})
