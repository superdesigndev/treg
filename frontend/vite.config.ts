import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  plugins: [vue({ template: { transformAssetUrls: { includeAbsolute: false } } })],
  base: '/app/ui/',
  build: {
    outDir: fileURLToPath(new URL('../src/treg/web/dashboard', import.meta.url)),
    emptyOutDir: true,
    rolldownOptions: { output: { codeSplitting: { groups: [{ name: 'vue', test: /node_modules\/(?:@vue|vue)\// }] } } },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    // The dashboard page is served by the API on :18790; without this, imported assets resolve
    // against that origin and 404.
    origin: 'http://localhost:5173',
    proxy: {
      '^/(?!app/ui/)': { target: 'http://127.0.0.1:18790', changeOrigin: false },
    },
  },
})
