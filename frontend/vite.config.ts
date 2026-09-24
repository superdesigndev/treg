import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  plugins: [vue({ template: { transformAssetUrls: { includeAbsolute: false } } })],
  base: '/app/ui/',
  // Shared onboarding components also serve unbundled public pages and still use templates.
  resolve: { alias: { vue: 'vue/dist/vue.esm-bundler.js' } },
  build: {
    outDir: fileURLToPath(new URL('../src/treg/web/dashboard', import.meta.url)),
    emptyOutDir: true,
    rolldownOptions: { output: { codeSplitting: { groups: [{ name: 'vue', test: /node_modules\/(?:@vue|vue)\// }] } } },
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
