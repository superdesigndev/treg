// The hub app page (`/apps/<team>/<name>`): a standalone entry beside the Dashboard, same Vue and build.
import { createApp } from 'vue'
import '@fontsource/geist-pixel/latin-400.css'
import '@fontsource/dm-mono/latin-400.css'
import '@fontsource/dm-mono/latin-500.css'
import '../standalone/standalone.css'
import AppPage from './AppPage.vue'

try { document.documentElement.dataset.theme = localStorage.getItem('treg-theme') || 'light' } catch { /* light */ }
createApp(AppPage).mount('#app')
