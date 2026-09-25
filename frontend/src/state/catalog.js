
export default {
// ---- endpoint catalog (/catalog/*) ----
    // The catalog is additive: every failure here leaves the marketplace exactly as it was, so a
    // server that predates these routes shows no platform shelf rather than an error.
    async loadPlatforms(){
      if(this.plats.loaded || this.plats.loading) return;
      this.plats.loading=true;
      try{ const d=await this.api('/catalog/platforms'); this.plats.list=(d&&d.platforms)||[]; this.plats.providers=(d&&d.providers)||{}; this.plats.loaded=true; }
      catch(e){ this.plats.list=[]; }
      finally{ this.plats.loading=false; } },
// Platform pages are hash routes (/app#platform/<slug>): unlike /app/marketplace/<service> there
    // is no server route to serve the SPA on a hard reload of a /app/platforms/<slug> path.
    platformFromHash(){ const m=/^#platform\/(.+)$/.exec(location.hash||''); return m?decodeURIComponent(m[1]):null; },
// The PUBLIC catalog lives at real paths (/catalog, /catalog/<slug>), not hash routes, because
    // a hash is never a distinct URL to a crawler and the whole catalog was therefore unindexable.
    // Same Vue views as the signed-in marketplace — this is one UI, not a second implementation.
    catalogFromPath(p){
      if(p==='/catalog' || p==='/catalog/') return {view:'connections', slug:null};
      if(p==='/search' || p==='/search/') return {view:'find', slug:null};
      const m=/^\/catalog\/([^/]+)\/?$/.exec(p||'');
      return m ? {view:'platform', slug:decodeURIComponent(m[1])} : null;
    },
// A plain view hash (#usage, #orgs, …) so deep links land on the right pane on a FRESH load,
    // not only via back/forward. '#billing' is the name 402 bodies and emails use for "add funds";
    // billing lives on the Team pane's Billing tab, so it aliases there.
    viewFromHash(){ let v=(location.hash||'').replace('#','');
      if(v==='billing'){ this.orgTab='billing'; v='orgs'; }
      return ['tools','orgs','activity','usage','admin','help','secrets','start','resources','connections','referrals'].includes(v)?v:null; },
// Land on a public catalog URL (see catalogFromPath): the finder page, a platform shelf, or the index.
    openCatalogRoute(r){
      if(r.view==='find'){ this.view='find'; this.loadPlatforms(); return; }
      if(r.slug) this.openPlatform(r.slug, true); else this.go('connections', true); },
openPlatform(slug, fromPop){ this.resetConfirms();
      this.detail=null; this.platSlug=slug; this.view='platform'; this.platOpen={}; this.epOpen={}; this.epTab={}; this.platEx={}; this.platActionsOpen=false;
      this.platClearFilters(); this.platCopied='';
      // A public visitor stays on the indexable /catalog/<slug> URL; a signed-in one keeps the
      // in-app hash route. Same view either way — only the address bar differs.
      if(!fromPop) history.pushState({platform:slug}, '', this.publicCatalog
        ? '/catalog/'+encodeURIComponent(slug)
        : '/app#platform/'+encodeURIComponent(slug));
      this.loadPlatforms();                                   // the header's provider links need the list
      // Connected/not-connected is a member fact and the endpoint needs a session; a public
      // visitor has none, so skip it rather than fire a guaranteed 401 on every shelf view.
      if(!this.publicCatalog && !this.providers.length) this.loadConnections();
      this.loadPlatform();
      window.scrollTo(0,0); },
async loadPlatform(){ if(!this.platSlug) return;
      this.platErr=''; this.platLoading=true; this.platData=null;
      // include_hidden=1: pull the account/utility endpoints too. They render behind a per-section
      // "N management endpoints" expander rather than in the main ledger — the page decides that,
      // client-side, off each endpoint's `kind` (see platRowsAll / platLedger).
      try{ this.platData=await this.api('/catalog/platforms/'+encodeURIComponent(this.platSlug)+'?include_hidden=1'); }
      catch(e){ this.platErr = e.status===404
        ? 'No catalog for this platform on this server yet.'
        : 'Could not load the endpoint catalog'+(e.detail?': '+e.detail:'.'); }
      finally{ this.platLoading=false; } },
// Tile furniture. Catalog labels carry a parenthetical or an em-dash gloss ("Google Search
    // (SERPs, keyword data)") that reads as noise under a logo — the tile shows the name, the
    // title attribute keeps the whole thing.
    // The name filter behind the Catalog search box, shared by the shelves and the tab counts.
    platNameHit(p, q){ return ((p.label||'')+' '+(p.slug||'')+' '+(p.providers||[]).join(' ')).toLowerCase().includes(q); },
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
    platConnected(pl){ return ((pl&&pl.providers)||[]).some(s=>this.catConnected(s)); },
platConnNames(pl){ return ((pl&&pl.providers)||[]).filter(s=>this.catConnected(s)).map(s=>this.provName(s)).join(', '); },
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
platClearFilters(){ this.platDomain=''; this.platQ=''; this.platVerifiedOnly=false; },
// Every ledger row expands, merged or not: the row says what it does, the expansion says how to
    // call it, and which of the two a visitor needs is not something the row shape can decide.
    toggleRow(r){ this.platOpen[r.key] = !this.platOpen[r.key]; },
// The per-section "N management endpoints" expander: reveals the account/utility rows folded
    // out of the browse ledger (see platLedger).
    // Level two: a provider sub-row under a merged row opens its own instruction. Single rows have
    // nothing to compare and skip this level entirely.
    toggleEp(e){ this.epOpen[e.id] = !this.epOpen[e.id]; },
// One pill per provider on a merged row, carrying that provider's CHEAPEST priced endpoint —
    // the number a comparison turns on — and a ✓ if any of its endpoints is verified.
    provPills(eps){
      const by=new Map();
      for(const e of eps){
        const cur=by.get(e.provider), n=this.costUsd(e.cost);
        if(!cur) by.set(e.provider, {name:e.provider_display||e.provider, cost:e.cost, n, endpoint:e, verified:!!e.verified});
        else { cur.verified = cur.verified || !!e.verified;
               if(n!=null && (cur.n==null || n<cur.n)){ cur.cost=e.cost; cur.n=n; cur.endpoint=e; } }
      }
      // Cheapest first, then verified: only three of these are ever shown, so the three that
      // survive have to be the ones worth seeing. Unpriced providers sort to the tail — they are
      // exactly the ones whose pill would say nothing but a name.
      return [...by.values()]
        .sort((a,b)=>(a.n==null)-(b.n==null) || (a.n-b.n) || (b.verified-a.verified))
        .map(p=>({name:p.name, verified:p.verified, price:p.endpoint.platform_eligible ? this.pillPrice(p.cost) : this.endpointAccessLabel(p.endpoint)})); },
// A pill prices an endpoint only when there IS a price: a published number, or "free". A
    // credit-metered or dashboard-only rate has no number to show, and saying so at length is what
    // broke the row — the pill just drops it, and the sub-row below carries the full story.
    pillPrice(c){
      if(!c || !c.type) return '';
      if(c.type==='free') return 'free';
      if(c.type==='quota_rows'){ const l=this.costLabel(c); return l.length<=8 ? l : ''; }
      return typeof c.usd==='number' ? this.costLabel(c) : ''; },
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
    provName(service){ const p=this.providers.find(x=>x.service===service); if(p) return p.display_name;
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
      return (t==='res' && !e.has_example) ? 'req' : (t || 'req'); },
setEpTab(e, tab){
      if(tab==='res' && !e.has_example) return;      // no such tab; nothing to show
      this.epTab[e.id]=tab;
      if(tab==='res') this.loadExample(e); },
// Fetched when the response tab is FIRST opened, never with the page: a platform can carry
    // hundreds of endpoints and the captured responses are the heaviest thing in the catalog.
    async loadExample(e){
      if(this.platEx[e.id]) return;                  // already loaded, loading, or failed
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
        return (typeof lo==='number' && lo<hi ? '$'+this.usdNum(lo)+'-$'+this.usdNum(hi) : '$'+this.usdNum(hi))+'/'+c.rate_unit; }
      // Any other price table (image models) is a range: cheapest row up to the validated ceiling.
      if(typeof c.usd_min==='number' && c.usd_min<c.usd) return '$'+this.usdNum(c.usd_min)+'-$'+this.usdNum(c.usd)+'/'+unit;
      return '$'+this.usdNum(c.usd)+'/'+unit; },
costTitle(c){ if(!c) return 'The catalog has no price for this endpoint';
      if(c.display_unit) return this.costLabel(c)+(c.note ? ' — '+c.note : '');
      const nat=this.nativeAmount(c);
      return [c.note,
              nat ? 'billed as '+nat+'/'+this.priceUnit(c.type)+', converted at the catalog’s FX rate' : '',
              c.value==null&&c.type!=='free' ? 'billed '+c.type.replace(/_/g,' ')+' — the provider does not publish the rate, check your plan in their dashboard' : ''].filter(Boolean).join(' — ')
        || 'What this endpoint costs at the provider'; }
}
