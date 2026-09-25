import { createApp } from 'vue'
import App from './App.vue'
// The two brand faces, from pinned npm packages (OFL-1.1) and bundled same-origin: Geist Pixel for
// page titles, DM Mono for figures and code. Body text uses the system font (DESIGN.md).
import '@fontsource/geist-pixel/latin-400.css'
import '@fontsource/dm-mono/latin-400.css'
import '@fontsource/dm-mono/latin-500.css'
import '@fontsource/dm-mono/latin-400-italic.css'
import './styles/base.css'
import '../../src/treg/web/media/redesign/dashboard.css'

const app = createApp(App)
const setup = (window as unknown as { TregAgentSetup: Record<string, object> }).TregAgentSetup
app.component('TregTryItOut', setup.TryItOut!)
app.component('TregAgentPicker', setup.AgentPicker!)
app.component('TregSetupInstructions', setup.SetupInstructions!)
app.mount('#app')
