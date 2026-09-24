export default {
token(){ return this.cfg.active ? (this.cfg.orgs[this.cfg.active]||{}).token : null; },
authed(){ return this.sessionMode || !!this.token; },
activeSlugNow(){ return this.sessionMode ? this.activeSlug : this.cfg.active; },
activeOrg(){
      if(this.sessionMode){ return this.myOrgs.find(o=>o.slug===this.activeSlug) || this.myOrgs[0] || null; }
      const a=this.cfg.orgs[this.cfg.active]; return a ? {slug:this.cfg.active, name:a.name, role:a.role, org_id:a.org_id} : null;
    },
activeName(){ const a=this.activeOrg; return a ? (a.name||a.slug) : ''; },
renameDirty(){ const a=this.activeOrg||{}, n=this.renameName.trim(), s=this.renameSlug.trim();
      return !!((n && n!==a.name) || (s && s!==a.slug)); },
activeRole(){ const a=this.activeOrg; return a ? a.role : 'member'; },
initials(){ return (this.me||'?').slice(0,2).toUpperCase(); },
filteredTools(){ const q=this.q.toLowerCase(); return this.tools.filter(t=>!q||t.name.toLowerCase().includes(q)||t.base_url.toLowerCase().includes(q)); },
// Home is segregated into three groups. Endpoints = tools you added directly (no bundle); Skills =
    // tools that came from a skill package (a bundle, so they carry a recipe); Recipes = recipe-only
    // bundles (a knowledge skill with no callable tool).
    endpoints(){ const q=this.q.toLowerCase(); return this.tools.filter(t=>!t.bundle_id && (!q||t.name.toLowerCase().includes(q)||(t.base_url||'').toLowerCase().includes(q))); },
skillTools(){ const q=this.q.toLowerCase(); return this.tools.filter(t=>t.bundle_id && (!q||t.name.toLowerCase().includes(q)||(t.base_url||'').toLowerCase().includes(q))); },
recipes(){ const q=this.q.toLowerCase(); const withTool=new Set(this.tools.filter(t=>t.bundle_id!=null).map(t=>t.bundle_id));
      return this.bundles.filter(b=>!withTool.has(b.id) && (!q||b.name.toLowerCase().includes(q))); },
hasAnyTools(){ return this.endpoints.length||this.skillTools.length||this.recipes.length; },
staleConns(){ return (this.connections||[]).filter(c=>c.needs_reconnect); },
// dying credentials treg can't renew itself
    needSecondCred(){ return (this.connections||[]).filter(c=>c.extra_credential_note); },
// connected, but not yet callable (Google Ads' developer token)
    toolGroups(){ return [
      {key:'endpoints', label:'Endpoints/CLI', hint:'APIs and CLIs you registered directly', rows:this.endpoints},
      {key:'skills', label:'Integration Skills', hint:'tools from a skill package (carry a recipe)', rows:this.skillTools},
    ]; },
cliShowHtml(){
      const t = [
        '$ treg login',
        '✓ Signed in as you@team.dev  (org: team)',
        '',
        '$ treg add stripe --base-url https://api.stripe.com --secret STRIPE_KEY',
        "✓ Registered 'stripe' - the key is stored server-side, never on your machine",
        '',
        '$ treg call https://api.stripe.com/v1/balance',
        '{ "object": "balance", "available": [ { "amount": 4210, "currency": "usd" } ] }',
      ].join('\n');
      return this.tutHL(t,'cmd');
    },
activeOrgId(){ return this.activeOrg ? this.activeOrg.org_id : null; },
canAdmin(){ return ['admin','owner'].includes(this.activeRole); },
canRegister(){ return this.activeRole!=='viewer'; },
// members+ may create secrets/tools
    secretRowsReady(){ return this.secretRows.filter(r=>(r.name||'').trim()&&r.value).length; },
// The names the marketplace credential ladder actually matches: tier 2 finds an org secret
    // NAMED exactly for the provider (Secret.name == service), so these are the names worth
    // suggesting — a key called APOLLO_API_KEY works as a plain secret but the catalog never sees it.
    keyNameSuggestions(){ return (this.providers||[])
      .filter(p=>p.auth_kind==='key'||p.auth_kind==='token')
      .slice().sort((a,b)=>a.service.localeCompare(b.service)); },
// ---- detail pages ----
    accessNames(){  // the grantable universe: tool names + recipe-only skill names (an integration
      // skill is reachable through its tool's name; recipe-only bundles need their own entry)
      return [...new Set([...this.tools.map(t=>t.name), ...this.recipes.map(r=>r.name)])].sort(); }
}
