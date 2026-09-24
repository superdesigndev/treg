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
import details from './details.js'
import admin from './admin.js'
import snippets from './snippets.js'
import tryTool from './tryTool.js'
import find from './find.js'
import findComputed from './findComputed.js'
import lifecycle from './lifecycle.js'
import billingComputed from './billingComputed.js'
import catalogComputed from './catalogComputed.js'
import sessionComputed from './sessionComputed.js'
import agentsComputed from './agentsComputed.js'
import onboardingComputed from './onboardingComputed.js'
import detailsComputed from './detailsComputed.js'
export default {
 data,
 computed: {...resourcesComputed, ...billingComputed, ...catalogComputed, ...sessionComputed, ...agentsComputed, ...onboardingComputed, ...detailsComputed, ...findComputed},
 methods: {...resources, setElement(name, element) { this.elements[name] = element }, ...session, ...team, ...keys, ...agents, ...projects, ...governance, ...activity, ...billing, ...referrals, ...secrets, ...tools, ...skills, ...format, ...onboarding, ...analytics, ...help, ...connections, ...sharing, ...navigation, ...catalog, ...details, ...admin, ...snippets, ...tryTool, ...find, ...lifecycle},
 watch:{
    // a11y (WCAG 2.4.3): when a dialog/drawer opens, move focus INTO it (was left on the trigger)
    newTool(v){ this.focusOverlay(v); }, newSkill(v){ this.focusOverlay(v); }, newOrg(v){ this.focusOverlay(v); },
    showJoin(v){ this.focusOverlay(v); }, addOrg(v){ this.focusOverlay(v); }, copyTool(v){ this.focusOverlay(v); },
    tryTool(v){ this.focusOverlay(v); }, 'welcome.on'(v){ this.focusOverlay(v); }, reqAsk(v){ this.focusOverlay(v); },
    'welcome.agent'(v){ try{ localStorage.setItem('treg-agent', v); }catch(e){} },  // see _restoreAgent
    activeOrgId(){ this.resetRenameForm(); },  // team switch or first load: prefill the rename form
    // Editing the box after a find starts a new question: the answer to the old one goes away
    // and the shelves go back to filtering by name.
    q(v){ if(this.findActive && this.view==='connections' && v.trim()!==this.find.q) this.findExit(); },
  },
 provide() { return provideDashboard(this) },
 async mounted() {
   try { await boot.call(this) }
   catch (error) { this.bootFailed = true; console.error('Dashboard initialization failed', error) }
   finally { this.bootReady = true }
 },
 beforeUnmount() { this.stopLifecycle?.() },
}
