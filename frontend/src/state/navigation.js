
export default {
switchTo(o){ this.switchOrg(o); },
// dropdown "Switch" button - just switch, stay where you are
    orgSettings(o){ this.orgMenu=false;  // dropdown ⚙ - go INTO that team, then open its settings
      if(o.slug!==this.activeSlugNow){ if(!this.sessionMode && !this.connected(o.slug)){ this.addOrg=true; return; } this.switchOrg(o); }
      this.go('orgs'); },
resetConfirms(){ this.confirmDelTool=null; this.confirmDelSecret=null; this.confirmDelBundle=null; this.confirmRemove=null; this.confirmAgent=null; this.keyMenu=null; this.keyConfirm=null; this.confirmLeave=false; this.confirmDel=''; this.confirmAdmUser=null; this.confirmAdmOrg=null; },
go(v, fromPop){ this.resetConfirms(); this.mobileNav=false;  // stale inline "Confirm" states must not survive a view switch (accidental-delete risk)
      // `usage` is a TAB of the activity page now, not a view of its own — keep the old route
      // working so an existing /app#usage link, and the balance card's deep link, still land right.
      if(v==='usage'){ this.actTab='usage'; v='activity'; this.loadUsage(); }
      else if(v==='activity'){ this.actTab='feed'; }
      this.detail=null; this.view=v; if(v==='activity')this.loadCalls(); if(v==='admin')this.loadAdmin(); if(v==='orgs'){this.loadOrgAdmin(); this.loadMyUsage(); this.loadBilling();} if(v==='usage')this.loadUsage(); if(v==='secrets'){this.loadSecrets(); if(!this.providers.length)this.loadConnections();} if(v==='resources')this.loadTeamResources(); if(v==='connections')this.loadConnections(); if(v==='referrals')this.loadReferrals(); if(v==='hub')this.loadHub();
      // push history so browser Back navigates BETWEEN views instead of leaving the app; the '/app'
      // pathname also walks back from a /app/skills/<x> detail URL so reload doesn't reopen the detail
      if(!fromPop) history.pushState({view:v}, '',
        this.publicCatalog && v==='connections' ? '/catalog' : '/app#'+v);
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
// "Bring your own key", from anywhere: land on the Catalog's Platform tab — the shelf of every
    // integration a key can be pasted into — rather than a single provider's detail page. With a
    // service, the shelf scrolls to that provider's row and flashes it; the flash clears itself so
    // a later visit to the tab doesn't replay a stale highlight.
    // A signed-out visitor who opens a provider goes to its PUBLIC page (/tools/<service>) — a
    // real navigation, as a method because Vue template expressions cannot reach the `location`
    // global (it is not on the template-expression allowlist, so an inline use fails silently).
    goPublicTool(service){ location.href='/tools/'+encodeURIComponent(service); },
goByok(service){
      this.mkTab='platform'; this.mkCat='';
      this.byokFocus=service||null; this.closeEpTry();
      this.go('connections');
      if(!service) return;
      this.$nextTick(()=>{ const el=document.getElementById('prov-'+service);
        if(el) el.scrollIntoView({block:'center', behavior:'smooth'}); });
      setTimeout(()=>{ if(this.byokFocus===service) this.byokFocus=null; }, 4000);
    },
openProvider(service, fromPop){ this.resetConfirms();
      this.detail=null; this.mkService=service; this.view='provider';
      if(!fromPop) history.pushState({mk:service}, '', '/app/marketplace/'+encodeURIComponent(service));
      // The consent popup can return before /connections has been re-read, and a deep link may
      // arrive before the first load — either way the page needs the data it renders from.
      if(!this.connections.length || !this.providers.length) this.loadConnections();
      this.loadPlatforms();
      window.scrollTo(0,0); }
}
