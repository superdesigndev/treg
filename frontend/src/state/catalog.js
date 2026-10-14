import { markRaw } from 'vue'

// Agents' verdicts read the way Steam reads user reviews: the positive share (a partly useful verdict
// counts half) named by its band, and the stronger words only once enough teams stand behind it.
// `teams` is already a band (5, 10, 25, 50), which is exactly where Very and Overwhelmingly start.
export function reviewSummary(r){
  const s=r.share.useful+r.share.partly/2, n=r.teams;
  const [tone, label] = s>=.8 ? ['pos', s>=.95 && n>=50 ? 'Overwhelmingly positive' : n>=25 ? 'Very positive' : 'Positive']
    : s>=.7 ? ['pos', 'Mostly positive'] : s>=.4 ? ['mixed', 'Mixed'] : s>=.2 ? ['neg', 'Mostly negative']
    : ['neg', n>=50 ? 'Overwhelmingly negative' : n>=25 ? 'Very negative' : 'Negative'];
  // `rank` orders the labels (Very positive above Positive: as good, and more teams say so); sorts
  // go by it first, then by the share.
  return {tone, label, pct:Math.round(s*100), n, rank:REVIEW_LABELS.indexOf(label)};
}
const REVIEW_LABELS = ['Overwhelmingly negative', 'Very negative', 'Negative', 'Mostly negative', 'Mixed',
  'Mostly positive', 'Positive', 'Very positive', 'Overwhelmingly positive']

export default {
// ---- endpoint catalog (/catalog/*) ----
    // The catalog is additive: every failure here leaves the marketplace exactly as it was, so a
    // server that predates these routes shows no platform shelf rather than an error.
    async loadPlatforms(){
      if(this.plats.loaded || this.plats.loading) return;
      // Public catalog pages carry the same body inline (routers/web.py `_spa_catalog_page`), so
      // their first render already has the shelves; read it once and fall back to the request.
      const inline=document.getElementById('catalog-platforms');
      if(inline){ inline.remove();
        try{ const d=JSON.parse(inline.textContent); this.plats.list=d.platforms||[]; this.plats.providers=d.providers||{}; this.plats.loaded=this.plats.settled=true; return; }
        catch(e){} }
      this.plats.loading=true;
      try{ const d=await this.api('/catalog/platforms'); this.plats.list=(d&&d.platforms)||[]; this.plats.providers=(d&&d.providers)||{}; this.plats.loaded=true; }
      catch(e){ this.plats.list=[]; }
      finally{ this.plats.loading=false; this.plats.settled=true; } },
// Platform pages are hash routes (/app#platform/<slug>): unlike /app/marketplace/<service> there
    // is no server route to serve the SPA on a hard reload of a /app/platforms/<slug> path.
    platformFromHash(){ const m=/^#platform\/([^/]+)/.exec(location.hash||''); return m?decodeURIComponent(m[1]):null; },
// The compared capability a platform URL names, if any: `/catalog/<slug>/<key>` publicly,
    // `#platform/<slug>/<key>` in the app. `<key>` is what the server puts on its row (`compare`: the
    // capability id without its platform prefix); `platComparison` resolves it once the shelf has loaded.
    platCapFromLocation(){
      const m=/^#platform\/[^/]+\/(.+)$/.exec(location.hash||'') || /^\/catalog\/[^/]+\/([^/]+)\/?$/.exec(location.pathname||'');
      return m?decodeURIComponent(m[1]):null; },
platUrl(slug, cap){ const tail=cap ? '/'+encodeURIComponent(cap) : '';
      return this.publicCatalog ? '/catalog/'+encodeURIComponent(slug)+tail : '/app#platform/'+encodeURIComponent(slug)+tail; },
// The PUBLIC catalog lives at real paths (/catalog, /catalog/<slug>), not hash routes, because
    // a hash is never a distinct URL to a crawler and the whole catalog was therefore unindexable.
    // Same Vue views as the signed-in marketplace — this is one UI, not a second implementation.
    catalogFromPath(p){
      if(p==='/catalog' || p==='/catalog/') return {view:'catalog', slug:null};
      if(p==='/search' || p==='/search/') return {view:'find', slug:null};
      const m=/^\/catalog\/([^/]+)(?:\/[^/]+)?\/?$/.exec(p||'');
      return m ? {view:'platform', slug:decodeURIComponent(m[1])} : null;
    },
// A plain view hash (#usage, #orgs, …) so deep links land on the right pane on a FRESH load,
    // not only via back/forward. '#billing' is the name 402 bodies and emails use for "add funds";
    // billing lives on the Team pane's Billing tab, so it aliases there.
    viewFromHash(){ let v=(location.hash||'').replace('#','');
      if(v==='billing'){ this.orgTab='billing'; v='orgs'; }
      return ['tools','orgs','activity','usage','admin','help','secrets','start','resources','catalog','connections','referrals','hub'].includes(v)?v:null; },
// Land on a public catalog URL (see catalogFromPath): the finder page, a platform shelf, or the index.
    openCatalogRoute(r){
      if(r.view==='find'){ this.view='find'; this.loadPlatforms(); return; }
      if(r.slug) this.openPlatform(r.slug, true); else this.go('catalog', true); },
openPlatform(slug, fromPop, cap){ this.resetConfirms();
      if(cap===undefined) cap = fromPop ? this.platCapFromLocation() : null;
      // Moving between a shelf and one of its comparisons keeps the loaded shelf: the page is the same
      // payload read another way, so only the address and the view state change.
      const same = this.view==='platform' && this.platSlug===slug && (this.platData || this.platLoading);
      if(this.platSlug!==slug) this.platSetupOpen=false;
      this.detail=null; this.platSlug=slug; this.view='platform'; this.platCap=cap||null; this.drawerTool=null;
      this.epTab={}; this.platCopied='';
      if(!same){ this.platEx={}; this.platQ=''; this.epInfo={};
        if(this.find.scope) this.findExit(); }     // a shelf's answer belongs to that shelf
      this.platComparisonSort={key:'price', dir:'asc'};
      this.catalogTrack(cap ? 'catalog_comparison_viewed' : 'catalog_platform_viewed', cap ? {compare:cap} : {});
      // A public visitor stays on the indexable /catalog/<slug> URL; a signed-in one keeps the
      // in-app hash route. Same view either way — only the address bar differs.
      if(!fromPop) history.pushState({platform:slug}, '', this.platUrl(slug, cap));
      if(this.platCap) this.loadComparisonInfo();
      window.scrollTo(0,0);
      if(same) return;
      this.loadPlatforms();                                   // the header's provider links need the list
      // Connected/not-connected is a member fact and the endpoint needs a session; a public
      // visitor has none, so skip it rather than fire a guaranteed 401 on every shelf view.
      if(!this.publicCatalog && !this.providers.length) this.loadConnections();
      return this.loadPlatform(); },     // the shelf's data, for a caller that acts on it once loaded
// Where a provider name leads: the public provider page signed out, the provider's page in the app
    // signed in. Both list every tool the provider serves.
    provUrl(service){ return this.publicCatalog ? '/tools/'+encodeURIComponent(service) : '/app/marketplace/'+encodeURIComponent(service); },
goProvider(service){ if(this.publicCatalog) this.goPublicTool(service); else this.openProvider(service); },
openComparisonOn(slug, key){ this.openPlatform(slug, false, key); },
async loadProviderTools(service){
      if(this.mkTools && this.mkTools.service===service && (this.mkTools.data || this.mkTools.loading)) return;
      this.mkTools={service, loading:true, err:'', data:null};
      const slot=this.mkTools;
      try{ const d=await this.api('/catalog/providers/'+encodeURIComponent(service)); if(slot.service===this.mkService) slot.data=markRaw(d); }
      catch(e){ slot.err = e.status===404 ? '' : 'Could not load this provider\'s tools.'; }
      finally{ slot.loading=false; } },
openComparison(key){ this.openPlatform(this.platSlug, false, key); },
closeComparison(){ this.openPlatform(this.platSlug, false, null); },
// What the calls treg served say about a comparison's providers, and which inputs each accepts: one
    // `/catalog/endpoints/<id>` read returns the endpoint and every sibling of its capability with
    // `observed`, plus the routing plan's `accepts` when the capability has a routed tool. Cached per id.
    // The server's observation cache never waits on the database: an id it has not read yet comes
    // back empty while it is read in the background, and the answer says so (`observed_pending`).
    // Then the detail is asked for once more, shortly after, and the numbers fill in.
    async loadEndpointInfo(id, retried){
      if(!id || (this.epInfo[id] && !retried)) return;
      if(!retried) this.epInfo[id]={loading:true, data:null};
      const slot=this.epInfo[id];      // the reactive copy: writes to the literal would not render
      try{ const d=await this.api('/catalog/endpoints/'+encodeURIComponent(id));
        slot.data=markRaw(d);
        if(d.observed_pending && !retried) setTimeout(()=>this.loadEndpointInfo(id, true), 1500); }
      catch(e){ if(!retried) slot.data=null; }
      finally{ slot.loading=false; } },
async loadComparisonInfo(){
      if(!this.platData){ return; }      // loadPlatform calls back once the shelf arrives
      const row=this.platComparison && this.platComparison.row; if(!row) return;
      const lead=(row.endpoints.find(e=>e.kind==='routed')||row.endpoints[0]).id;
      this.platComparisonLead=lead;
      await this.loadEndpointInfo(lead); },
openTool(id, via='click'){ this.drawerTool=id; this.loadEndpointInfo(id);
      const e=this.drawerEp; if(e) this.catalogToolEvent('catalog_tool_opened', e, {via}); },
closeTool(){ this.drawerTool=null; },
// Up and down walk the list the drawer was opened from, so a comparison reads row after row
    // without closing anything.
    stepTool(d){ const ids=this.drawerIds; const i=ids.indexOf(this.drawerTool);
      if(i<0) return; const next=ids[Math.min(ids.length-1, Math.max(0, i+d))];
      if(next && next!==this.drawerTool) this.openTool(next, 'step'); },
drawerKeys(ev){
      if(!this.drawerTool || this.epTry || ev.metaKey || ev.ctrlKey || ev.altKey) return;
      const t=ev.target; if(t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      if(ev.key==='Escape'){ ev.preventDefault(); this.closeTool(); }
      else if(ev.key==='ArrowDown' || ev.key==='j'){ ev.preventDefault(); this.stepTool(1); }
      else if(ev.key==='ArrowUp' || ev.key==='k'){ ev.preventDefault(); this.stepTool(-1); } },
// Start a shelf's request before boot has resolved the session, so it is already in flight when
    // the view opens. `loadPlatform` takes it over instead of asking again.
    prefetchPlatform(slug){
      const request=this.api('/catalog/platforms/'+encodeURIComponent(slug)+'?include_hidden=1');
      request.catch(()=>{});   // loadPlatform reports the failure; an unclaimed prefetch just drops it
      this.platPrefetch=markRaw({slug, request}); },
async loadPlatform(){ if(!this.platSlug) return;
      this.platErr=''; this.platLoading=true; this.platData=null; const live=this.ticket('platform', false);
      const pre=this.platPrefetch; this.platPrefetch=null;
      // include_hidden=1: pull the account/utility endpoints too. They render behind a per-section
      // "Account and setup" section rather than among the tools; the page decides that, client-side,
      // off each endpoint's `kind` (see platRowsAll).
      try{ const data=await (pre && pre.slug===this.platSlug ? pre.request
        : this.api('/catalog/platforms/'+encodeURIComponent(this.platSlug)+'?include_hidden=1'));
        if(live()){ this.platData=data; if(this.platCap) this.loadComparisonInfo(); } }
      catch(e){ if(live()) this.platErr = e.status===404
        ? 'No catalog for this platform on this server yet.'
        : 'Could not load the endpoint catalog'+(e.detail?': '+e.detail:'.'); }
      finally{ if(live()) this.platLoading=false; } },
// Tile furniture. Catalog labels carry a parenthetical or an em-dash gloss ("Google Search
    // (SERPs, keyword data)") that reads as noise under a logo — the tile shows the name, the
    // title attribute keeps the whole thing.
    // The name filter behind the Catalog search box, shared by the shelves and the tab counts.
    platNameHit(p, q){
      const providers=(p.providers||[]).flatMap(s=>[s, this.plats.providers[s]||'']);
      const names=[p.label||'', p.slug||'', ...providers].map(s=>s.toLowerCase());
      const compact=q.replace(/[^a-z0-9]/g,'');
      return names.some(name=>name.includes(q) || (compact && name.replace(/[^a-z0-9]/g,'').includes(compact))); },
platShort(label){ return String(label||'').split(' — ')[0].split(' (')[0].trim(); },
platInitial(pl){ return (this.platShort(pl.label)||pl.slug||'?').slice(0,1).toUpperCase(); },
// Deterministic hue from the slug: an undrawn platform keeps the same colour across reloads
    // and differs from its neighbours, with no colour table to maintain.
    platTileBg(slug){ let h=0; const s=String(slug||'');
      for(let i=0;i<s.length;i++) h=(h*31+s.charCodeAt(i))%360;
      return 'hsl('+h+' 44% 42%)'; },
// The catalog bills in several currencies, so every price the marketplace SHOWS is the server's
    // computed `usd` — one unit, so two numbers on the same screen can actually be compared. The
    // native amount rides along as a muted suffix wherever the provider bills in something else, so
    // nobody has to wonder whether we invented the figure.
    // Sub-cent rates get two significant figures ($0.015, $0.00015); a dollar or more gets cents.
    usdNum(n){
      n=Number(n);
      if(n>=1) return n.toFixed(2).replace(/0+$/,'').replace(/\.$/,'');
      const s=n.toPrecision(2);
      if(s.indexOf('e')>=0) return n.toFixed(8).replace(/0+$/,'').replace(/\.$/,'');  // < 1e-6
      return s.replace(/0+$/,'').replace(/\.$/,''); },
priceUnit(type){ return {per_call:'call', per_success:'success', per_result:'result', quota_rows:'row'}[type]
                        || String(type||'').replace(/_/g,' '); },
// "¥0.10" — the provider's own figure, shown next to the converted one, never instead of it.
    nativeAmount(c){
      if(!c || c.currency==='USD' || c.value==null) return '';
      const n=Number(c.value);
      // Counts read as counts ("5 credits", "40 API units"), money keeps its cents (¥0.10).
      if(c.currency==='credit' || c.currency==='unit'){
        const count=Number.isInteger(n)?String(n):n.toFixed(2);
        let u = c.currency==='credit' ? 'credit' : (c.unit||'unit').replace(/_/g,' ').replace(/\bapi\b/,'API');
        if(n!==1 && !/s$/.test(u)) u+='s';
        return count+' '+u; }
      const sym={CNY:'¥'}[c.currency];
      const num=n>=0.01 ? n.toFixed(2) : n.toFixed(6).replace(/0+$/,'').replace(/\.$/,'');
      return sym ? sym+num : num+' '+c.currency; },
costNative(c){ return c && c.display_unit ? '' : this.nativeAmount(c); },
// The card's starting price. `price_from` arrives as null OR as an empty object, and an empty
    // one must read as "no price on the row" — otherwise it short-circuits the OAuth-free branch
    // below and an OAuth-only platform silently loses its "free with your account".
    platPrice(pl){
      const raw=pl && pl.price_from;
      const pf=(raw && Object.keys(raw).length) ? raw : null;
      const paid=(pf && typeof pf.usd==='number')
        ? '$'+this.usdNum(typeof pf.display_usd==='number' ? pf.display_usd : pf.usd)+' / '+
          (pf.display_unit || this.priceUnit(pf.type))
        : null;
      if(pf && pf.type==='free') return {free:true, text:'free with your account'};
      // An OAuth integration among the providers means the floor price is $0: the account you
      // connect IS the licence. Metered providers may serve the same platform (that rate moves to
      // the tooltip) — but "from" is a floor, and the floor is free.
      const provs=(pl && pl.providers)||[];
      const hasOauth=provs.some(s=>{
        const p=this.providers.find(x=>x.service===s);
        return p && p.auth_kind==='oauth' && !p.metered; });
      if(hasOauth) return {free:true, text:'free with your account', paid};
      if(paid) return {free:false, text:paid, native:this.nativeAmount(pf)};
      return null; },
// priced with no published number, or key-auth with no rate — unknown, not free
    platPriceTitle(pl){
      const p=this.platPrice(pl); if(!p) return '';
      if(p.free) return 'Connect the account and the calls cost nothing extra'+
        (p.paid ? ' — without it, metered providers serve this from '+p.paid : '');
      const pf=pl.price_from||{};
      return ['The cheapest published rate across this platform’s endpoints',
              p.native ? 'billed as '+p.native+' / '+this.priceUnit(pf.type)+', converted at the catalog’s FX rate' : '',
              pf.note].filter(Boolean).join(' — '); },
// What agents said after using an endpoint's result. `reviews` carries a `share` only past the
    // server's team threshold (comparable between providers of one capability); below it the quotes
    // alone, as early reviews.
    verdictPct(r, v){ return Math.round(r.share[v]*100)+'% '+this.verdictLabel(v).toLowerCase(); },
verdictLabel(v){ return ({useful:'Useful', partly:'Partly useful', not_useful:'Not useful'})[v]||v; },
verdictClient(c){ return ({'claude-code':'Claude Code', codex:'Codex', cursor:'Cursor', 'claude-connector':'Claude', cli:'treg CLI', pi:'Pi'})[c]||''; },
verdictDate(d){ try{ const m=String(d).length===7;   // a review carries only its month
      return new Date(d+(m?'-01':'')+'T00:00:00Z').toLocaleDateString('en-US', m ? {month:'short', year:'numeric', timeZone:'UTC'} : {month:'short', day:'numeric', timeZone:'UTC'}); }catch(err){ return d; } },
// How a provider authorizes: the member's connection registry when signed in, else the open
    // platform payload, so a signed-out visitor is told the same thing.
    provAuthKind(service){ const p=this.providers.find(p=>p.service===service)
      || (this.platData&&this.platData.providers||{})[service]; return (p&&p.auth_kind)||''; },
endpointAccessLabel(e){
      if(e.kind==='routed') return 'Routed platform call';
      if(e.id==='fishaudio.voices.list') return 'Team voices + BYOK';
      const p=this.providers.find(p=>p.service===e.provider)
        || (this.platData&&this.platData.providers||{})[e.provider] || {};
      if(p.auth_kind==='oauth') return p.metered ? 'OAuth · metered' : 'OAuth connection';
      if(p.auth_kind==='key') return e.platform_eligible ? 'Platform + BYOK' : 'BYOK only';
      return e.platform_eligible ? 'Platform access' : 'Own connection required';
    },
catConnected(service){ return !!this.connCount[service]; },
endpointAuthMethods(e){ return [...new Set([e&&e.authorization_method,
      ...((e&&e.authorization_methods)||[]), ...Object.keys((e&&e.authorization_paths)||{})].filter(Boolean))]; },
endpointMethodSpec(e){
      const methods=this.endpointAuthMethods(e); if(methods.length!==1) return null;
      const p=this.providers.find(x=>x.service===e.provider);
      return p && (p.authorization_methods||[]).find(m=>m.name===methods[0]);
    },
catEndpointConnected(e){
      const methods=this.endpointAuthMethods(e);
      if(!methods.length) return this.catConnected(e.provider);
      return (this.connections||[]).some(c=>c.provider===e.provider && methods.includes(c.authorization_method));
    },
endpointConnectLabel(e){
      const method=this.endpointMethodSpec(e);
      return (method&&method.action_label)||('Connect '+(e.provider_display||e.provider));
    },
// Whether a REGISTRY connection to this provider is still metered. Connecting usually ends the
    // billing question — the account you connect is the licence — but a `metered` provider bills
    // treg's own app per use (X since Feb 2026), so those calls are debited from the team balance
    // no matter whose account made them. `/providers` carries the flag, and the deployment's
    // TREG_OAUTH_BILLED_PROVIDERS decides whether it is set, so the price a browser shows follows
    // the kill switch instead of hard-coding today's answer.
    catMetered(service){
      const p=this.providers.find(x=>x.service===service); return !!(p && p.metered); },
// A platform is callable today if ANY provider serving it is connected — the card is browsing,
    // not routing, so which one it is stays a question for the platform page.
    // The platform's providers the team holds its own credential for.
    // A provider this deployment paused (/meta `paused_providers`): its name and message, or null.
    providerPaused(service){ return ((this.meta&&this.meta.paused_providers)||{})[service]||null; },
// A platform every provider of which is paused: its tile and shelf say so instead of selling it.
    platPaused(pl){ const ps=(pl&&pl.providers)||[]; return ps.length>0 && ps.every(s=>this.providerPaused(s)); },
    platOwn(pl){ return ((pl&&pl.providers)||[]).filter(s=>this.catConnected(s)); },
// What a platform's personal mark means when the team holds a credential for one of its providers:
    // not that the platform works (every platform works, on treg's key) but that calls there use yours.
    platConnTitle(pl){ const own=this.platOwn(pl);
      const what=own.some(s=>!this.pastedCredential(this.providerIndex.get(s))) ? 'your account' : 'your key';
      return 'Calls to '+own.map(s=>this.provName(s)).join(', ')+' use '+what+', unmetered. The rest run on treg\'s key.'; },
// The endpoint's inputs, grouped by where they go. Query first, then path, then body: the order
    // you fill them in for the common GET, and the order the provider's own docs tend to use.
    paramSections(e){
      const i=(e&&e.input)||{}, out=[];
      const add=(key,label,map,type)=>{
        const names=Object.keys(map||{});
        if(names.length) out.push({key, label, type, rows:names.map(n=>({name:n, ...map[n]}))}); };
      add('query','Query', i.queryParams);
      add('path','Path', i.pathParams);
      add('headers','Headers', i.headers);
      add('body','Body', i.body, i.bodyType);
      return out; },
// Examples arrive as real JSON values, so an array or object has to be stringified rather than
    // rendered as "[object Object]".
    fmtExample(v){ return (typeof v==='object') ? JSON.stringify(v) : String(v); },
// The comparable price, in USD, straight from the server's computed `usd` — the FX table lives
    // in the catalog so a rate refresh re-prices every screen at once, and the dashboard cannot
    // drift from the CLI by carrying its own constant. Anything with no published number, and a row
    // quota (which is not a price at all), can never win "cheapest": showing "from —" would be
    // worse than showing the cheapest thing we do know the price of.
    costUsd(c){
      if(!c || !c.type) return null;
      if(c.type==='free') return 0;
      if(c.type==='quota_rows') return null;
      return typeof c.usd==='number' ? c.usd : null; },
// An OAuth endpoint you have already connected costs nothing MORE to call — the account is the
    // licence — so a connected own_account row is the free path, and usually the right answer.
    capFree(e){ return this.catEndpointConnected(e) && !this.catMetered(e.provider)
      && (e.scope==='own_account' || (e.cost&&e.cost.type==='free')); },
capCheapest(eps){
      let best=null;
      for(const e of (eps||[])){
        const free=this.capFree(e), n=free?0:this.costUsd(e.cost);
        if(n==null) continue;
        if(best===null || n<best.n)
          best={n, label:(free||n===0)?'free':this.costLabel(e.cost), native:(free||n===0||e.cost.display_unit)?'':this.nativeAmount(e.cost)};
      }
      return best; },
// "See Zhihu, Toutiao, and 6 more" — two names so the row says what KIND of thing is hiding,
    // then a count, because eight more names is the wall the shelf exists to avoid.
    moreLabel(rest){
      const names=rest.slice(0,2).map(p=>this.platShort(p.label));
      if(rest.length<=2) return 'See '+names.join(' and ');
      return 'See '+names.join(', ')+', and '+(rest.length-2)+' more'; },
// Cut at the last word boundary before the limit, so a clipped line ends on a word rather than
    // mid-token. A curated `name` is short by construction, so this is a no-op on those rows.
    clip(text, n){ const s=String(text||'').trim(); if(s.length<=n) return s;
      const cut=s.slice(0,n); const sp=cut.lastIndexOf(' ');
      return (sp>n*0.6 ? cut.slice(0,sp) : cut).replace(/[\s,;:.—-]+$/,'')+'…'; },
// "free – $0.38": the spread of a capability's published prices. Units differ between providers,
    // so the card states the range, never a "from" that reads as one price.
    priceRange(eps){ const ns=eps.map(e=>e.platform_eligible===false ? null : this.costUsd(e.cost)).filter(n=>n!=null);
      if(!ns.length) return ''; const lo=Math.min(...ns), hi=Math.max(...ns);
      const f=n=>n===0 ? 'free' : '$'+this.usdNum(n);
      return lo===hi ? f(lo) : f(lo)+' – '+f(hi); },
// What a provider takes, from the routing plan's `accepts` (alternatives of required inputs).
    // Absent when the plan does not cover the endpoint: unknown, never "incompatible".
    takesLabel(acc){ if(!acc||!acc.length) return [];
      const n={domain:'domain', website:'website', name:'name', email:'email', linkedin_url:'LinkedIn URL',
               linkedin_handle:'LinkedIn handle', query:'query'};
      return acc.map(a=>a.map(x=>n[x]||x.replace(/_/g,' ')).join(' + ')); },
// Calls an endpoint served in the last 30 days (`observed.samples` on the platform response).
    callsOf(e){ return (e && e.observed && e.observed.samples) || 0; },
// 141059 → "141k": a count beside a percentage is its denominator, not a figure to read digit by digit.
    // Volumes are shown by band, never exact: how much traffic one provider gets through treg, or how
    // many teams rated it, is not something a comparison needs to the unit.
    approxCalls(n){ return (n>=1e5 ? '100k+' : n>=1e4 ? '10k+' : n>=1e3 ? '1k+' : n>=100 ? '100+' : n>=50 ? '50+' : '20+')+' calls'; },
approxTeams(n){ return (n>=50 ? '50+' : n>=25 ? '25+' : n>=10 ? '10+' : n>=5 ? '5+' : 'under 5')+' teams'; },
worksTitle(r){ return r.works ? r.works.pct+'% of '+this.approxCalls(r.works.n)+' in the last 30 days ended without a provider error' : 'Fewer than 20 calls in the last 30 days'; },
usefulTitle(r){ const s=r.useful, v=s && r.e.reviews;
      return v ? s.label+': '+s.pct+'% positive ('+this.verdictKinds.map(k=>this.verdictPct(v, k)).join(', ')+') from '
                 +this.approxTeams(v.teams)+'\' agents after using the result, last 90 days'
               : r.e.reviews ? 'Early reviews: fewer than 5 teams have rated it, so no score yet; open the tool to read what their agents said'
               : 'No team has rated it yet'; },
// The two measured numbers, one rule each wherever they show: success once 20 calls are decided,
    // agents' verdict once 5 teams have rated. Below that the server sends the quotes with no
    // `share`: early reviews, never a score.
    worksOf(o){ return o && o.decided>=20 && o.ok_rate!=null ? {pct:Math.round(o.ok_rate*100), n:o.decided} : null; },
usefulOf(e){ return e.reviews && e.reviews.share ? reviewSummary(e.reviews) : null; },
// A tool's price on a card or a row: a tool only your own key or account can call has no treg price.
    toolPrice(e){ return e.platform_eligible!==false ? this.costShort(e.cost)
      : this.provAuthKind(e.provider)==='oauth' ? 'your account' : 'your key only'; },
// A price small enough for a collapsed line: "$0.024/call", "2 rows", "free", "credit-priced".
    // The long form ("per success · price in provider dashboard") is true but belongs in the
    // expanded detail — inline it wraps a row onto three lines, which is what broke the merged rows.
    costShort(c){
      if(!c || !c.type) return '—';
      if(c.type==='free') return 'free';
      // A price table has no scalar value but does have a range (and, for video, a per-second
      // rate); only a truly unpublished number is "credit-priced".
      if(c.value==null && !(c.table && typeof c.usd==='number')) return 'credit-priced';
      return this.costLabel(c); },
// `providers` needs a session. Publicly, "is this a provider treg knows" is answered by the
    // open catalog response instead — otherwise the whole action chain collapses and a signed-out
    // reader sees no way to bring their own key. `mkOauth` stays false without the registry, so the
    // public branch offers BYOK (which is true for every provider) rather than guessing Connect.
    mkKnown(service){ return this.providers.some(p=>p.service===service)
      || (this.publicCatalog && !!((this.platData&&this.platData.providers||{})[service])); },
mkOauth(service){ const p=this.providers.find(x=>x.service===service); return !!p && p.auth_kind==='oauth'; },
// `providers` comes from /connections, which needs a session. On a public catalog URL there is
    // none, so fall back to the display name the OPEN catalog response already carries — otherwise
    // every provider on a public shelf would render as its bare slug.
    provName(service){ const p=this.providerIndex.get(service); if(p) return p.display_name;
      const c=(this.platData&&this.platData.providers||{})[service];
      return (c&&c.display_name) || this.plats.providers[service] || service; },
// Provider-wide facts, served once per provider on the platform response rather than copied
    // onto every row.
    provFact(service, key){ const p=(this.platData&&this.platData.providers||{})[service]; return (p&&p[key])||''; },
// The sentences an expanded row needs and a table cell can't hold: how it meters, what it
    // rate-limits, where the rate card is. Only ones we actually have — an empty list hides the box.
    epFacts(e){
      const out=[], c=e.cost;
      // Where a credit-metered price actually lives. The chip can only say "credit-priced"; this is
      // the sentence that tells you the unit and where to read the rate.
      if(c && c.type!=='free' && c.value==null)
        out.push('Billed per '+this.priceUnit(c.type)+' — the provider does not publish the rate, so '
                 +'the number is in your plan on their dashboard.');
      if(c && c.note) out.push(c.note);
      const limits=this.provFact(e.provider,'limits'); if(limits) out.push('Limits: '+limits);
      const pricing=this.provFact(e.provider,'pricing_url'); if(pricing) out.push('Rate card: '+pricing);
      return out; },
async copyCall(e){
      if(await this.toClipboard(e.call_template||'')){
        this.platCopied=e.id; setTimeout(()=>{ if(this.platCopied===e.id) this.platCopied=''; },1500); } },
// Which pane of an endpoint's detail is showing. What you SEND and what comes BACK are two
    // documents; stacking them made the expansion a page you scrolled rather than read.
    epTabOf(e){ const t=this.epTab[e.id];
      return ((t==='res' && !e.has_example) || (t==='rev' && !e.reviews)) ? 'req' : (t || 'req'); },
setEpTab(e, tab){
      if(tab==='res' && !e.has_example) return;      // no such tab; nothing to show
      if(tab==='rev' && !e.reviews) return;
      this.epTab[e.id]=tab;
      if(tab==='res') this.loadExample(e); },
// Fetched when the response tab is FIRST opened, never with the page: a platform can carry
    // hundreds of endpoints and the captured responses are the heaviest thing in the catalog.
    async loadExample(e){
      if(this.platEx[e.id]) return;                  // already loaded, loading, or failed
      const info=this.epInfo[e.id] && this.epInfo[e.id].data;   // the drawer's detail carries it
      if(info && info.example_response!=null){ this.platEx[e.id]={loading:false, err:'', text:JSON.stringify(info.example_response,null,2)}; return; }
      this.platEx[e.id]={loading:true, err:'', text:''};
      const slot=this.platEx[e.id];
      try{ const d=await this.api('/catalog/examples/'+encodeURIComponent(e.id)); slot.text=JSON.stringify(d,null,2); }
      catch(err){ slot.err = err.status===404 ? 'No example was captured for this endpoint.' : 'Could not load the example response.'; }
      finally{ slot.loading=false; } },
// Price, compact enough to sit in a table cell, and always in USD: "$0.015/success", "1 row",
    // "free", "—". The provider's own figure follows as a muted suffix (see `costNative`).
    costLabel(c){
      if(!c || !c.type) return '—';
      if(c.display_unit && typeof c.display_usd==='number') return (c.display_prefix||'')+'$'+this.usdNum(c.display_usd)+(c.display_suffix||'')+'/'+c.display_unit;
      if(c.type==='free') return 'free';
      // Known billing unit, unpublished number: say which, and where the number lives. A bare
      // "per success" reads as free, and "—" hides that we do know how it's metered.
      if(c.value==null && !c.table){  // a price table has no scalar `value`; its range renders below
        const per={per_call:'call', per_success:'success', per_result:'result'}[c.type];
        return per ? 'per '+per+' · price in provider dashboard' : '—'; }
      // Rows convert like any meter once fx carries a rate — dollars first, native as the fallback.
      if(c.type==='quota_rows' && typeof c.usd!=='number'){
        const n=Number(c.value); return n+' row'+(n===1?'':'s')+'/call'; }
      // A published number the FX table can't convert (an unknown currency) still beats silence —
      // fall back to the native figure rather than dropping the price entirely.
      if(typeof c.usd!=='number') return this.nativeAmount(c)+'/'+this.priceUnit(c.type);
      const unit=(c.type==='quota_rows'?'call':this.priceUnit(c.type));
      // A duration-priced table (video models) is quoted per second, the way the model is sold.
      if(c.rate_unit && typeof c.rate_usd==='number'){
        const lo=c.rate_usd_min, hi=c.rate_usd;
        return (typeof lo==='number' && lo<hi ? '$'+this.usdNum(lo)+'-\u200b$'+this.usdNum(hi) : '$'+this.usdNum(hi))+'/'+c.rate_unit; }
      // Any other price table (image models) is a range: cheapest row up to the validated ceiling.
      // The zero-width space after the dash is the range's only break point: "$0.0066-$0.013/success"
      // is otherwise one word, and a phone-width price cell cannot hold it.
      if(typeof c.usd_min==='number' && c.usd_min<c.usd) return '$'+this.usdNum(c.usd_min)+'-\u200b$'+this.usdNum(c.usd)+'/'+unit;
      return '$'+this.usdNum(c.usd)+'/'+unit; },
}
