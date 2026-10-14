import { storageSet } from './storage.js'
import { provideDashboard } from './context'
import resources from './resources.js'
import resourcesComputed from './resourcesComputed.js'
import data from './data.js'
import boot from './boot.js'
import session from './session.js'
import team from './team.js'
import keys from './keys.js'
import agents from './agents.js'
import projects from './projects.js'
import governance from './governance.js'
import activity from './activity.js'
import billing from './billing.js'
import referrals from './referrals.js'
import secrets from './secrets.js'
import tools from './tools.js'
import skills from './skills.js'
import format from './format.js'
import onboarding from './onboarding.js'
import analytics from './analytics.js'
import help from './help.js'
import connections from './connections.js'
import sharing from './sharing.js'
import navigation from './navigation.js'
import catalog from './catalog.js'
import catalogEvents from './catalogEvents.js'
import details from './details.js'
import admin from './admin.js'
import snippets from './snippets.js'
import tryTool from './tryTool.js'
import find from './find.js'
import findComputed from './findComputed.js'
import lifecycle from './lifecycle.js'
import hub from './hub.js'
import tickets from './tickets.js'
import credentialIssues, { credentialIssueComputed } from './credentialIssues.js'
import billingComputed from './billingComputed.js'
import connectionsComputed from './connectionsComputed.js'
import catalogComputed from './catalogComputed.js'
import sessionComputed from './sessionComputed.js'
import agentsComputed from './agentsComputed.js'
import onboardingComputed from './onboardingComputed.js'
import detailsComputed from './detailsComputed.js'
export default {
 data,
 computed: {...resourcesComputed, ...billingComputed, ...connectionsComputed, ...catalogComputed, ...sessionComputed, ...agentsComputed, ...onboardingComputed, ...detailsComputed, ...findComputed, ...credentialIssueComputed},
 methods: {...resources, setElement(name, element) { this.elements[name] = element }, ...session, ...team, ...keys, ...agents, ...projects, ...governance, ...activity, ...billing, ...referrals, ...secrets, ...tools, ...skills, ...format, ...onboarding, ...analytics, ...help, ...connections, ...sharing, ...navigation, ...catalog, ...catalogEvents, ...details, ...admin, ...snippets, ...tryTool, ...find, ...lifecycle, ...hub, ...tickets, ...credentialIssues},
 watch:{
    // Dialog focus (in on open, trapped, back to the trigger on close) and Escape: v-dialog (dialogs/dialog.ts)
    'welcome.agent'(v){ storageSet('treg-agent', v); },  // see _restoreAgent
    activeOrgId(_, previous){ this.parkCredentialIssue(previous); this.ticket('agentIssue'); this.ticket('keyAction');
      this.snipAgent=null;
      this.agentBusy=false; this.keyBusy=false; this.resetRenameForm(); this.resumeCredentialIssue(); },
    agentConnectionTarget(){ this.stopAgentPoll(); this.resumeAgentPoll(); },
    // Editing the box after a find starts a new question: the answer to the old one goes away
    // and the shelves go back to filtering by name.
    q(v){ if(this.findActive && this.view==='catalog' && v.trim()!==this.find.q) this.findExit(); },
  },
 provide() { return provideDashboard(this) },
 async mounted() {
   try { await boot.call(this) }
   catch (error) { this.bootFailed = true; console.error('Dashboard initialization failed', error) }
   finally { this.bootReady = true }
 },
 beforeUnmount() { this.stopLifecycle?.(); this.clearCredentialIssues() },
}
