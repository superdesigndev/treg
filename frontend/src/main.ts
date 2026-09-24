import { createApp } from 'vue'
import App from './App.vue'
import './styles/base.css'
import '../../src/treg/web/media/redesign/dashboard.css'

const app = createApp(App)
const setup = (window as unknown as { TregAgentSetup: Record<string, object> }).TregAgentSetup
app.component('TregTryItOut', setup.TryItOut!)
app.component('TregAgentPicker', setup.AgentPicker!)
app.component('TregSetupInstructions', setup.SetupInstructions!)
app.mount('#app')
