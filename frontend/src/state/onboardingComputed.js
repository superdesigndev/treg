import * as TregAgentSetup from '../agent-setup/data'
import { PERSONAL_MAIL } from './constants.js'

export default {
// first-run welcome: the agent picker (step 1) and the per-agent setup line (step 2)
    welcomeAgents(){ return TregAgentSetup.agents; },
welcomeMoreAgents(){ return TregAgentSetup.moreAgents; },
welcomeAgent(){ return this.welcomeAgents.concat(this.welcomeMoreAgents).find(a=>a.id===this.welcome.agent) || this.welcomeAgents[0]; },
welcomeIsMore(){ return this.welcomeMoreAgents.some(a=>a.id===this.welcome.agent); },
welcomeSetupCmd(){ return this.buildAgentPrompt('agent', true); },
// onboarding modal: the setup line + team/token as ONE copyable block (token masked until shown)
    welcomeSetupFull(){ return this.welcomeSetupCmd+'\n\nwith team '+(this.activeSlugNow||'<team-slug>')+' token: '+(this.myToken||'<YOUR_TOKEN>'); },
welcomeSetupMasked(){ const t=this.myToken?(this.startTokenShow?this.myToken:(this.myToken.slice(0,14)+'••••••••••••••••')):'<YOUR_TOKEN>';
      return this.welcomeSetupCmd+'\n\nwith team '+(this.activeSlugNow||'<team-slug>')+' token: '+t; },
tryExamples(){ return TregAgentSetup.examples; },
// "Picked for you": shown while the server builds (pending), asks (ask) or has plays (ready); hidden when off/failed
    forYou(){ const p=this.signupProfile; return p && ['pending','ask','ready'].includes(p.status) ? p : null; },
forYouFacts(){ const p=this.forYou||{}, c=p.company||{}, who=p.person||{};
      return [c.name?'':who.company, (c.industries||[])[0], c.employees?c.employees+' people':'', who.title?'You: '+who.title:''].filter(Boolean).join(' · '); },
personalEmail(){ const d=((this.me||'').split('@')[1]||'').toLowerCase(); return !d || PERSONAL_MAIL.includes(d.split('.')[0]); },
tryOauth(){ return TregAgentSetup.oauthGroups; }
}
