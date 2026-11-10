import * as TregAgentSetup from '../agent-setup/data'

export default {
// Getting started: the agent picker and the per-agent setup line
    welcomeAgents(){ return TregAgentSetup.agents; },
welcomeMoreAgents(){ return TregAgentSetup.moreAgents; },
welcomeAgent(){ return this.welcomeAgents.concat(this.welcomeMoreAgents).find(a=>a.id===this.welcome.agent) || this.welcomeAgents[0]; },
welcomeIsMore(){ return this.welcomeMoreAgents.some(a=>a.id===this.welcome.agent); },
welcomeSetupCmd(){ return this.buildAgentPrompt('agent', true); },
// Getting started copies the prompt whole; an agent with the directive prompt carries the token in it.
    welcomeSetupCopy(){ return this.welcomeAgent.directiveSetup ? TregAgentSetup.directiveSetupText(this.proxy||location.origin, this.activeSlugNow, this.myToken) : this.welcomeSetupCmd; },
welcomeSetupShown(){ return this.welcomeAgent.directiveSetup ? TregAgentSetup.directiveSetupText(this.proxy||location.origin, this.activeSlugNow, this.myToken, !this.startTokenShow) : this.welcomeSetupCmd; },
// onboarding modal: the setup line + team/token as ONE copyable block (token masked until shown)
    welcomeSetupFull(){ return this.welcomeSetupCmd+'\n\nwith team '+(this.activeSlugNow||'<team-slug>')+' token: '+(this.myToken||'<YOUR_TOKEN>'); },
welcomeSetupMasked(){ const t=this.myToken?(this.startTokenShow?this.myToken:(this.myToken.slice(0,14)+'••••••••••••••••')):'<YOUR_TOKEN>';
      return this.welcomeSetupCmd+'\n\nwith team '+(this.activeSlugNow||'<team-slug>')+' token: '+t; },
tryExamples(){ return TregAgentSetup.examples; },
tryOauth(){ return TregAgentSetup.listedOauthGroups(this.providers); }
}
