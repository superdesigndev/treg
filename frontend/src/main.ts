import { createApp } from 'vue'
import App from './App.vue'
import { vDialog } from './dialogs/dialog'
import { preloadInitialView, prefetchAfterBoot } from './views'
// The two brand faces, from pinned npm packages (OFL-1.1) and bundled same-origin: Geist Pixel for
// page titles, DM Mono for figures and code. Body text uses the system font (DESIGN.md).
import '@fontsource/geist-pixel/latin-400.css'
import '@fontsource/dm-mono/latin-400.css'
import '@fontsource/dm-mono/latin-500.css'
import '@fontsource/dm-mono/latin-400-italic.css'
import './styles/base.css'
import '../../src/treg/web/media/redesign/dashboard.css'

preloadInitialView()
prefetchAfterBoot(createApp(App).directive('dialog', vDialog).mount('#app'))
