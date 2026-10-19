import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'
import { workbenchFixtures } from './scripts/fixture-plugin.mjs'

export default defineConfig({
  plugins: [vue({ template: { transformAssetUrls: { includeAbsolute: false } } }), ...(process.env.TREG_FIXTURES ? [workbenchFixtures()] : [])],
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
    proxy: {
      '^/(?!app/ui/)': { target: 'http://127.0.0.1:18790', changeOrigin: false },
    },
  },
})
