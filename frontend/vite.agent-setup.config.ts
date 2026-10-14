import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

// `/agent-setup.js` for Enrich Arena, a standalone page that runs on Vue's global build rather than
// the Dashboard bundle. The source is `src/agent-setup/`, which the Dashboard imports directly; this
// build compiles the same components to render functions, keeps `vue` as the page's global `Vue`,
// and exposes the module as `window.TregAgentSetup`. Runs after the Dashboard build, into its
// output directory, so the one generated tree holds every compiled browser asset.
export default defineConfig({
  plugins: [vue({ template: { transformAssetUrls: { includeAbsolute: false } } })],
  publicDir: false,
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    outDir: fileURLToPath(new URL('../src/treg/web/dashboard', import.meta.url)),
    emptyOutDir: false,
    lib: {
      entry: fileURLToPath(new URL('src/agent-setup/index.ts', import.meta.url)),
      formats: ['iife'],
      name: 'TregAgentSetup',
      fileName: () => 'agent-setup.js',
    },
    rolldownOptions: { external: ['vue'], output: { globals: { vue: 'Vue' } } },
  },
})
