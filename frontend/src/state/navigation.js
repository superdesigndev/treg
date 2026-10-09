// The tab slugs an address may carry, per view: internal tab name → what the address says.
const TAB_SLUGS = {
  activity: { usage: 'usage', feed: 'calls' },
  orgs: { members: 'members', keys: 'keys', projects: 'projects', policy: 'policy', billing: 'billing', danger: 'settings' },
}


// A plain function, not only a method: the page preloader (views.ts) reads the address before the
// dashboard exists, through viewFromHash called on an empty object.
export function parseTabHash(hash){ const m=/^#?(activity|orgs)(?:\/([a-z]+))?(?:\?(.*))?$/.exec(hash||'');
  if(!m) return null;
  const tab=m[2] ? Object.entries(TAB_SLUGS[m[1]]).find(([,slug])=>slug===m[2])?.[0] : undefined;
  const key=new URLSearchParams(m[3]||'').get('key');
  return { view:m[1], tab, key:/^\d+$/.test(key||'') ? key : '' };
}

export default {
switchTo(o){ this.switchOrg(o); },
// dropdown "Switch" button - just switch, stay where you are
    orgSettings(o){ this.orgMenu=false;  // dropdown ⚙ - go INTO that team, then open its settings
      if(o.slug!==this.activeSlugNow){ if(!this.sessionMode && !this.connected(o.slug)){ this.addOrg=true; return; } this.switchOrg(o); }
      this.go('orgs'); },
resetConfirms(){ this.confirmDelTool=null; this.confirmDelSecret=null; this.confirmDelBundle=null; this.confirmRemove=null; this.confirmAgent=null; this.keyMenu=null; this.keyConfirm=null; this.confirmLeave=false; this.confirmDel=''; this.confirmAdmUser=null; this.confirmAdmOrg=null; },
// ---- tab addresses ----
// Only Activity and Team carry their tab in the address, and Activity also a key filter:
// '#activity/usage', '#activity/calls?key=7', '#orgs/billing'. Every other view keeps its plain
// '#view' address, and nothing here reads one: '#platform/<slug>' and the rest never reach it.
// The key is its id: names repeat ("Default key") and change on rename; an id does neither.
parseTabHash,
// Admins land on Usage; a member, who cannot see it, on Calls.
defaultActTab(){ return this.canAdmin ? 'usage' : 'feed'; },
tabUrl(view){
      if(view==='activity'){ const key=this.actTab==='usage' ? this.spendFilter.key : this.activityKey;
        return '/app#activity/'+TAB_SLUGS.activity[this.actTab]+(key ? '?key='+key : ''); }
      if(view==='orgs') return '/app#orgs/'+(TAB_SLUGS.orgs[this.orgTab]||'members');
      return '/app#'+view; },
// A tab or key change rewrites the current entry: Back leaves the page, it does not replay tabs.
syncTabUrl(){ if(!['activity','orgs'].includes(this.view) || this.publicCatalog) return;
      const url=this.tabUrl(this.view);
      if(location.pathname+location.hash!==url) history.replaceState({view:this.view}, '', url); },
go(v, fromPop){ this.resetConfirms(); this.mobileNav=false; this.drawerTool=null;  // stale inline "Confirm" states must not survive a view switch (accidental-delete risk)
      // A tab address (see parseTabHash) is consumed once, by the go() it was read for.
      const route=this._tabRoute; this._tabRoute=null;
      // `usage` is a TAB of the activity page now, not a view of its own — keep the old route
      // working so an existing /app#usage link, and the balance card's deep link, still land right.
      if(v==='usage'){ this.actTab='usage'; v='activity'; }
      else if(v==='activity'){ this.actTab=(route&&route.tab)||this.defaultActTab(); }
      if(v==='activity' && this.actTab==='usage' && !this.canAdmin) this.actTab='feed';
      if(v==='activity' && route && route.key){ if(this.actTab==='usage') this.spendFilter.key=route.key; else this.activityKey=route.key; }
      if(v==='orgs' && route && route.tab) this.orgTab=route.tab;
      if(v==='activity' && this.actTab==='usage') this.loadUsage();
      this.detail=null; this.view=v; if(v==='activity')this.loadCalls(); if(v==='admin')this.loadAdmin(); if(v==='orgs'){this.loadOrgAdmin(); this.loadMyUsage(); this.loadBilling();} if(v==='usage')this.loadUsage(); if(v==='secrets'){this.loadSecrets(); if(!this.providers.length)this.loadConnections();} if(v==='resources')this.loadTeamResources(); if(v==='catalog')this.loadConnections(); if(v==='connections')this.loadConnections(); if(v==='referrals')this.loadReferrals(); if(v==='hub')this.loadHub();
      // push history so browser Back navigates BETWEEN views instead of leaving the app; the '/app'
      // pathname also walks back from a /app/skills/<x> detail URL so reload doesn't reopen the detail
      if(!fromPop) history.pushState({view:v}, '',
        this.publicCatalog && v==='catalog' ? '/catalog' : this.tabUrl(v));
      else if(v==='activity' || v==='orgs') this.syncTabUrl();  // e.g. '#usage' or '#billing' landed: name the tab
      if(!fromPop) window.scrollTo(0,0);
      this.startAgentOpen=false; this.orgMenu=false;
      if(this.elements.accountMenu) this.elements.accountMenu.open=false; },
// ---- detail pages (shareable deep links) ----
    routeFromPath(path){ const m=/^\/app\/(skills|tools)\/(.+)$/.exec(path||'');
      return m ? {kind:m[1]==='skills'?'skill':'tool', name:decodeURIComponent(m[2])} : null; },
// Marketplace deep links are their own route: they resolve against `providers` (already in
    // memory) rather than fetching a detail payload, so they can't share openDetail's loader.
    mkFromPath(path){ const m=/^\/app\/marketplace\/([^/]+)$/.exec(path||'');
      return m ? decodeURIComponent(m[1]) : null; },
// "Bring your own key", from anywhere: land on Connections, the page of every account and key the
    // team holds and every provider one can be added for. With a service, the page scrolls to that
    // provider's card and flashes it; the flash clears itself so a later visit doesn't replay it.
    // A signed-out visitor who opens a provider goes to its PUBLIC page (/tools/<service>) — a
    // real navigation, as a method because Vue template expressions cannot reach the `location`
    // global (it is not on the template-expression allowlist, so an inline use fails silently).
    goPublicTool(service){ location.href='/tools/'+encodeURIComponent(service); },
goByok(service){
      this.connQ=''; this.connKind='';
      this.byokFocus=service||null; this.closeEpTry();
      this.go('connections');  // the page scrolls to the card once it is there (ConnectionsPage.vue)
      if(!service) return;
      setTimeout(()=>{ if(this.byokFocus===service) this.byokFocus=null; }, 4000);
    },
openProvider(service, fromPop){ this.resetConfirms();
      this.detail=null; this.mkService=service; this.view='provider'; this.drawerTool=null;
      this.loadProviderTools(service);
      if(!fromPop) history.pushState({mk:service}, '', '/app/marketplace/'+encodeURIComponent(service));
      // The consent popup can return before /connections has been re-read, and a deep link may
      // arrive before the first load — either way the page needs the data it renders from.
      this.loadConnections();  // its accounts, a key saved under its name included
      this.loadPlatforms();
      window.scrollTo(0,0); }
}
