export default async function boot(){
    const lifecycle = new AbortController();
    let versionTimer;
    this.stopLifecycle = () => { lifecycle.abort(); clearInterval(versionTimer); };
    const listen = (target, event, handler, capture=false) => target.addEventListener(event, handler, { signal: lifecycle.signal, capture });
    document.documentElement.dataset.theme=this.theme;
    listen(document, 'click', ()=>{ if(this.mdMenu) this.mdMenu=false; if(this.keyMenu) this.keyMenu=null; });  // outside-click closes compact menus
    listen(document, 'keydown', e=>{
      // Escape closes any open modal/drawer (a universal expectation the modals didn't honour)
      if(e.key==='Escape'){ const orgWasOpen=this.orgMenu; this.closeOverlays(); if(orgWasOpen) this.elements.orgmain?.focus(); if(this.startAgentOpen){ this.startAgentOpen=false; this.elements.startAgentTrigger?.focus(); } else if(this.elements.accountMenu?.open){ this.elements.accountMenu.open=false; this.elements.accountMenu.querySelector('summary').focus(); } return; }
      // "/" focuses the search box (the "/" glyph in the box advertised a shortcut that didn't exist)
      const t=e.target, typing = t && (t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.tagName==='SELECT'||t.isContentEditable);
      if(e.key==='/' && !typing && this.authed && (this.view==='tools'||this.view==='resources'||this.view==='connections')){ e.preventDefault(); this.elements.search && this.elements.search.focus(); }
    });
    listen(document, 'click', e=>{  // the org dropdown didn't close on an outside click
      if(this.elements.accountMenu && !e.target.closest('.rd-account-menu')) this.elements.accountMenu.open=false;
      if(this.startAgentOpen && !e.target.closest('.rd-agent-picker')) this.startAgentOpen=false;
      if(this.orgMenu && !e.target.closest('.orgblock') && !e.target.closest('.dropdown')) this.orgMenu=false;
    });
    // the fixed-position dropdown must follow its trigger when anything scrolls or resizes
    listen(window, 'scroll', ()=>{ if(this.orgMenu) this.placeOrgMenu(); if(this.keyMenu)this.keyMenu=null; }, true);
    listen(window, 'resize', ()=>{ if(this.orgMenu) this.placeOrgMenu(); if(this.keyMenu)this.keyMenu=null; });
    listen(window, 'popstate', e=>{  // browser Back moves between views (was exiting the app)
      const mk=(e.state&&e.state.mk)||this.mkFromPath(location.pathname);
      if(mk){ this.openProvider(mk, true); return; }
      const pf=(e.state&&e.state.platform)||this.platformFromHash();
      if(pf){ this.openPlatform(pf, true); return; }
      const d=(e.state&&e.state.detail)||this.routeFromPath(location.pathname);
      if(d){ this.openDetail(d.kind, d.name, true); return; }
      let v=(e.state&&e.state.view)||(location.hash||'').replace('#','')||'tools';
      if(v==='billing'){ this.orgTab='billing'; v='orgs'; }
      if(['tools','orgs','activity','usage','admin','help','secrets','start','resources','connections','referrals'].includes(v)) this.go(v, true);
    });
    // Catalog data does not depend on the session, so a view that shows it starts fetching now,
    // alongside /meta and /auth/me, instead of after them (the shelves used to arrive last).
    const catalogShelf=this.catalogFromPath(location.pathname)?.slug || this.platformFromHash();
    if(this.catalogFromPath(location.pathname) || catalogShelf || location.hash==='#connections') this.loadPlatforms();
    if(catalogShelf) this.prefetchPlatform(catalogShelf);
    // /search needs no session to draw, so it does not wait for one: the page paints now, and the
    // session (the top bar's buttons, a result clicked before sign-in) follows when /auth/me answers.
    if(this.catalogFromPath(location.pathname)?.view==='find'){ this.publicCatalog=true; this.view='find'; this.bootReady=true; }
    this.meta = await fetch('/meta',{headers:{'ngrok-skip-browser-warning':'1'}}).then(r=>r.json()).catch(()=>this.meta);
    this.proxy = this.meta.public_url || location.origin;
    this.initAnalytics();
    // deploy detection: long-lived tabs learn about a new bundle on tab focus + a slow poll,
    // then offer a one-click refresh (index.html is no-cache, so a soft reload is enough)
    this.bootVersion = this.meta.app_version || '';
    listen(document, 'visibilitychange', ()=>{ if(!document.hidden) this.checkVersion(); });
    versionTimer = setInterval(()=>this.checkVersion(), 5*60*1000);
    // Invite-link params, parsed BEFORE the session check: ?invite_org= arrives freshly signed in
    // from the email link's POST-confirm; ?invite= is the legacy code path (prefilled login).
    const qs = new URLSearchParams(location.search);
    const linkOrg = qs.get('invite_org'), inv = qs.get('invite'), ref = qs.get('ref'), oauthSignin = qs.get('signin')==='oauth';
    this.oauthSignin=oauthSignin;
    if(ref){ try{ localStorage.setItem('treg-ref', ref); }catch(e){} }  // survives the sign-in reload so the welcome can preselect the agent the landing was about (see maybeOnboard)
    if(linkOrg || inv || qs.get('invite_expired') || ref || oauthSignin){ history.replaceState(null,'',location.pathname+location.hash); }  // strip one-shot params so reload/share doesn't replay them
    if(linkOrg){ this.inviteLinkOrg=parseInt(linkOrg,10)||null; }
    // A shared detail deep link (/app/skills/<x>, /app/tools/<x>) — from the URL itself, or stashed
    // before an OAuth hop (the callback always lands on /app, which would otherwise drop the path).
    let route=this.routeFromPath(location.pathname);
    let mkRoute=this.mkFromPath(location.pathname);
    // A public catalog URL renders the marketplace views with or without a session. Set BEFORE the
    // /auth/me check so the first paint is already in public mode, and drop the server-rendered
    // fallback (see `_spa_catalog_page`) now that the real UI is about to take over.
    const catRoute=this.catalogFromPath(location.pathname);
    if(catRoute){ this.publicCatalog=true; document.getElementById('prerender')?.remove(); }
    const stashed=localStorage.getItem('treg-next');
    if(stashed){ localStorage.removeItem('treg-next');
      if(!route && !mkRoute){
        route=this.routeFromPath(stashed); mkRoute=this.mkFromPath(stashed);
        if(route||mkRoute) history.replaceState(null,'',stashed);
      } }
    this._restoreAgent();
    const me = await fetch('/auth/me',{credentials:'include',headers:{'ngrok-skip-browser-warning':'1'}}).then(r=>r.ok?r.json():null).catch(()=>null);
    this.sessionChecked=true;
    if(me){ this.sessionMode=true; this.me=me.email; this.isAdmin=!!me.is_superadmin; this.onboarded=!!me.onboarded; this.icHash=me.intercom_user_hash||''; await this.loadAll(); this.analyticsIdentify(); this.initIntercom();
      // Share-born arrival (/app/skills/x?invite_org=N from the invite email): accept silently and
      // enter that team — the emailed "Sign in & accept" click was the consent. Otherwise the normal
      // first-run / invite-banner flow.
      // A result clicked on /search before sign-in: continue to it now.
      if(this.findResume()){ this.maybeOnboard(); return; }
      if(mkRoute){ this.maybeOnboard(); this.openProvider(mkRoute, true); return; }
      // Signed in on a /catalog URL: the same views, but as a member — so `publicCatalog` is
      // dropped and the shell comes back in full (vault, activity, try-it).
      // /search stays a public page for members too: it is a place to ask, not a dashboard view.
      if(catRoute){ this.publicCatalog=catRoute.view==='find'; if(catRoute.view!=='find') this.maybeOnboard();
        this.openCatalogRoute(catRoute);
        return; }
      const pfRoute=this.platformFromHash();
      if(pfRoute){ this.maybeOnboard(); this.openPlatform(pfRoute, true); return; }
      let landed=false;
      if(route) landed=await this.autoAcceptShare(route);
      if(!landed) this.maybeOnboard();
      if(route){ this.openDetail(route.kind, route.name, true);
        // a ?invite=<email> share link opened by a DIFFERENT signed-in account: say why it won't resolve
        if(!landed && inv && this.me && inv.toLowerCase()!==this.me.toLowerCase())
          this.detailNote='This share link was sent to '+inv+' — you’re signed in as '+this.me+'. Sign out (⏻) and sign in with that email to accept the invite.'; }
      else { const hv=this.viewFromHash(); if(hv) this.go(hv, true);
        else { this.go('start', true); history.replaceState({view:'start'},'','/app#start'); } }  // no deep link → Getting started is the default landing
      return; }  // GitHub/email session
    if(this.token){ await this.loadAll(); this.initIntercom();  // token mode: no email/hash → anonymous visitor chat
      // A token holder on a /catalog URL is a member, not a public visitor: same treatment as the
      // session branch. Without this the route falls through to viewFromHash() — which is null for
      // a path route — and a shelf link lands on Getting started instead.
      if(catRoute){ this.publicCatalog=catRoute.view==='find';
        this.openCatalogRoute(catRoute);
        return; }
      const pfTok=this.platformFromHash();
      if(mkRoute) this.openProvider(mkRoute, true); else if(pfTok) this.openPlatform(pfTok, true); else if(route) this.openDetail(route.kind, route.name, true);
      else { const hv=this.viewFromHash(); if(hv) this.go(hv, true);
        else { this.go('start', true); history.replaceState({view:'start'},'','/app#start'); } }
      return; }      // token-in-browser fallback
    // A marketplace link is only meaningful to a member, so a logged-out visitor gets the front
    // door rather than the share gate (which exists to accept skill/tool shares).
    // Logged out on a /catalog URL: render the catalog anyway. Its API is unauthenticated, so this
    // is the SAME marketplace UI a member sees, minus what needs a session — not a second build of
    // it. This is the branch that makes the catalog crawlable.
    if(catRoute){
      // loadConnections, not just loadPlatforms: /oauth/providers is an open endpoint, so the
      // Platform tab (the provider shelf) fills for a signed-out visitor too — only /connections
      // needs a session, and its failure is caught. Without this the tab reads "Platform 0" and
      // renders blank in an incognito window.
      if(catRoute.view==='find'){ this.view='find'; this.loadPlatforms(); } else if(catRoute.slug) this.openPlatform(catRoute.slug, true); else { this.view='connections'; this.loadConnections(); }
      return; }
    if(!inv && !linkOrg && !route && !qs.get('invite_expired') && !ref && !oauthSignin){ location.replace('/'); return; }  // logged-out plain visit → the marketing landing owns the front door. `ref` is a use-case page's CTA (/app?ref=p1), so keep that attribution while opening sign-in in place.
    if(route){ this.shareGate=route; this.demo.signin=true; }  // shared link while logged out: the focused gate (no sandbox mint, no tour); after sign-in the boot lands on it (email verify reloads in place; OAuth restores via the treg-next stash)
    else this.demo.signin=true;  // OAuth returns, use-case CTA arrivals (?ref=) and every other logged-out flow open sign-in; nothing mints a sandbox any more
    if(inv){ this.invitePrefill=inv; this.emailInput=inv; this.emailStage=false; this.demo.signin=true; }  // legacy code link while logged out: prefill + open sign-in; the invite auto-accepts after login (maybeOnboard)
  }
