import { isJobQuery } from './find.js'
export default {
mkProvider(){ return this.providers.find(p=>p.service===this.mkService)||null; },
mkConns(){ return this.connections.filter(c=>c.provider===this.mkService); },
mkNeedsCred(){ return this.mkConns.filter(c=>c.extra_credential_note); },
// Which capabilities ANY account here already holds — a page-level "you have this" summary,
    // since the permission list describes the integration, not one account.
    mkGranted(){ const s=new Set(); for(const c of this.mkConns) for(const cap of (c.capabilities||[])) s.add(cap); return s; },
// ---- endpoint catalog ----
    // Platform tiles grouped by the category the catalog assigns each platform. "Other" (the
    // taxonomy's bucket for things like `account`) gets no tile — those capabilities are only
    // meaningful inside a platform page, not as a destination of their own.
    // The canonical reading order. It is only an ORDER, not the list: the categories themselves come
    // from the data, so a category the catalog invents tomorrow still gets a shelf and a tab (sorted
    // to the end) instead of vanishing. A hint is optional for the same reason.
    platCategories(){
      // The founder's reading order. The answer engines (ChatGPT, Perplexity, Gemini…) used to be
      // their own shelf; they are SEO now, unfeatured, so they surface inside its "see more" row
      // rather than as a category of six. The list is a lookup, not a schema — a category the
      // catalog invents tomorrow still gets a shelf, sorted to the end.
      const order=['Enrichment','SEO/AEO','Social','Advertising','E-commerce','Reviews & Apps','AI generation','Community'];
      const hints={
        'AI generation':'video, image and voice models, the same model over several routes priced side by side',
        'SEO/AEO':'rankings, keywords and backlinks — what search engines know, and what the answer engines say',
        'Social':'posts, profiles and comments, straight from the feeds',
        'Enrichment':'people and company records, resolved from an email or a domain',
        'Advertising':'ad libraries and creator marketplaces — what is being promoted, and for how much',
        'E-commerce':'listings, prices and sellers across the marketplaces',
        'Reviews & Apps':'app stores and review sites — what people rate, and what they say',
        'Community':'forums and chat, where people answer each other',
      };
      const by={};
      for(const pl of this.plats.list){ const c=pl.category||'Other'; if(c==='Other') continue; (by[c]=by[c]||[]).push(pl); }
      const known=order.filter(c=>by[c]);
      const rest=Object.keys(by).filter(c=>!order.includes(c)).sort();
      return known.concat(rest).map(c=>({category:c, hint:hints[c]||'', items:by[c]}));
    },
// The catalog's size as a headline ("3,300+"), from the platform shelves already loaded: rounded
    // down to the hundred so it never overstates, and empty until the list arrives so no stale
    // number is ever shown.
    toolCountText(){
      const n=this.plats.list.reduce((a,p)=>a+(p.endpoints||0),0);
      return n>=100 ? (Math.floor(n/100)*100).toLocaleString('en-US')+'+' : '';
    },
// The platform-name filter the Catalog search box applies, lowercased; '' for none. A sentence is
    // a job, not a name (the finder answers it), and a find answer lights shelves instead of
    // filtering them, so neither filters anything.
    platNameQuery(){ return this.findActive || isJobQuery(this.q) ? '' : this.q.trim().toLowerCase(); },
mkTabs(){
      // With a name filter typed, each tab counts what it would SHOW; a tab reading "Social 33" over
      // an empty result made the filter look broken.
      const q=this.platNameQuery;
      const n=g=>q ? g.items.filter(p=>this.platNameHit(p, q)).length : g.items.length;
      const out=[{key:'all', label:'All', n:this.platCategories.reduce((a,g)=>a+n(g),0)}];
      for(const g of this.platCategories) out.push({key:g.category, label:g.category, n:n(g)});
      out.push({key:'platform', label:'Platform', n:this.providers.length});
      return out;
    },
// A build without /catalog has no tiles to show, so it falls back to the integration shelves
    // rather than opening on an empty tab.
    mkTabActive(){ return this.platCategories.length ? this.mkTab : 'platform'; },
// Shelves, with the long ones cut down to their featured tiles. A category of 14 platforms is a
    // wall you scroll past rather than read, so past PLAT_SHELF_MAX only the catalog's `featured`
    // ranks get a full tile and the tail collapses into one "See X, Y, and N more" row. Rank first,
    // then endpoint count — the tail sorts by size alone, which is the only signal it has left.
    platCatGroups(){
      const groups = this.mkTabActive==='all' ? this.platCategories
        : this.platCategories.filter(g=>g.category===this.mkTabActive);
      // The top-nav search reaches here too: with a query, every match shows (no featured collapse —
      // a hit hidden behind "N more" reads as no hit) and empty shelves drop away.
      // A find answer (state/find.js) owns the box while it is showing: the sentence is not a name
      // to filter by, so every shelf stays, platforms the answer landed on first and uncollapsed.
      if(this.findActive){
        const hits=this.findHits;
        if(!Object.keys(hits).length) return groups.map(g=>({...g, rest:[], total:g.items.length}));
        return groups.map(g=>{ const items=[...g.items].sort((a,b)=>(hits[b.slug]||0)-(hits[a.slug]||0) || (b.endpoints||0)-(a.endpoints||0));
          return {...g, items, rest:[], total:items.length, hits:items.filter(p=>hits[p.slug]).length}; })
          .sort((a,b)=>b.hits-a.hits);
      }
      const q=this.platNameQuery;
      if(q){
        const hit=p=>this.platNameHit(p, q);
        return groups.map(g=>{ const items=g.items.filter(hit)
            .sort((a,b)=>(b.endpoints||0)-(a.endpoints||0));
          return {...g, items, rest:[], total:items.length}; }).filter(g=>g.items.length);
      }
      return groups.map(g=>{
        const items=[...g.items].sort((a,b)=>
          (a.featured==null?1e9:a.featured)-(b.featured==null?1e9:b.featured) || (b.endpoints||0)-(a.endpoints||0));
        const feat=items.filter(p=>p.featured!=null);
        // Nothing ranked means nothing to feature — showing an empty shelf over a "more" row would
        // hide the whole category behind a click.
        // `total` stays the whole category so the shelf's count matches its tab — a header reading
        // "SEO 5" under a tab reading "SEO 10" looks like tiles went missing.
        if(items.length<=8 || !feat.length || this.platShelfOpen[g.category]) return {...g, items, rest:[], total:items.length};
        return {...g, items:feat, rest:items.filter(p=>p.featured==null), total:items.length};
      });
    },
mkPlatforms(){ return this.plats.list.filter(pl=>(pl.providers||[]).includes(this.mkService)); },
platRow(){ return this.plats.list.find(pl=>pl.slug===this.platSlug)||null; },
platLabel(){ return (this.platData&&this.platData.platform&&this.platData.platform.label)
      || (this.platRow&&this.platRow.label) || this.platSlug || 'Platform'; },
platProviders(){  // providers with endpoints here, in catalog order
      const seen=[]; for(const g of (this.platData&&this.platData.capabilities||[])) for(const e of (g.endpoints||[])) if(!seen.includes(e.provider)) seen.push(e.provider);
      for(const e of (this.platData&&this.platData.extended||[])) if(!seen.includes(e.provider)) seen.push(e.provider);
      // Navigation is a fact of THIS platform response, not of the separately-loaded connection
      // registry. Depending on that async registry made the entire row flicker away locally. `treg`
      // is the synthetic router, not a provider page a person can open.
      return seen.filter(s=>s!=='treg'); },
// ---- the ledger ----
    // Sections, their order and the merged/single split are all decided by the server (see
    // catalog_store.domain_rows) so the CLI, the API and this page can't disagree about what the
    // platform contains. Everything below is presentation the server has no business knowing:
    // the price label, whether the row is callable TODAY (which depends on who is logged in), and
    // the haystack the filter box searches.
    platRowsAll(){
      if(!this.platData) return [];
      const out=[];
      for(const sec of (this.platData.domains||[])) for(const r of (sec.rows||[])){
        const eps=r.endpoints||[]; if(!eps.length) continue;
        const cheapest=this.capCheapest(eps);
        const provs=[...new Set(eps.map(e=>e.provider_display||e.provider))];
        const pills=this.provPills(eps);
        out.push({...r, domain:sec.domain, endpoints:eps,
          // What the row SHOWS. The server already picked `name` over `summary` where a curated
          // name exists; this is the guard for the rows where one doesn't yet — a DataForSEO
          // summary is documentation prose and would render a paragraph in a table cell. The full
          // text is never lost: the expansion shows it whole.
          title:this.clip(r.description, 90),
          // Who serves it, as plain text — the collapsed row names the providers and prices none
          // of them. Three names is what fits on one line; the rest become a count, with the whole
          // list in the title attribute.
          // Three pills and a +N. Never four, never a wrap: the strip is what decides whether a
          // merged row is one line, and one line is the rule.
          pills:pills.slice(0,3), pillsMore:Math.max(0, pills.length-3),
          pillsMoreTitle:pills.slice(3).map(x=>x.name).join(', '),
          // Providers AND endpoints, always both — one provider can offer the same job three ways,
          // so the two numbers differ and the endpoint count standing in for the provider count read
          // "6 providers" on a 3-provider row. Both live in the cell's tooltip rather than on a
          // second line, because a second line is exactly what made these rows look broken.
          provN:provs.length,
          provTitle:provs.length+' provider'+(provs.length===1?'':'s')+' · '+eps.length+
                    ' endpoint'+(eps.length===1?'':'s')+' — '+provs.join(', '),
          // the capability is the join key when there is one; an unmapped row is its endpoint
          key:(r.capability||'')+'|'+eps[0].id,
          // a "management" row is one whose every endpoint is plumbing (account/utility) — those
          // fold behind a per-section expander instead of sitting in the main ledger.
          mgmt: eps.every(e=>e.kind==='account'||e.kind==='utility'),
          // "from $0.001" on a merged row, the flat label on a single one — and never "from free",
          // which reads as a hedge on the one price that needs none.
          price: cheapest ? ((r.kind==='merged'&&eps.length>1&&cheapest.n>0?'from ':'')+cheapest.label)
                 // No dollar figure anywhere, but a cost note: the credit-metered providers
                 // (Apollo, PDL, Hunter…) whose price IS documented, in their own units.
                 : (eps.some(e=>e.cost&&e.cost.note) ? 'see provider' : '—'),
          // the provider's own figure, so nobody has to wonder whether we invented the converted one
          priceNative: cheapest ? cheapest.native : '',
          priceTitle: r.kind==='merged'
            ? 'The cheapest of the '+eps.length+' providers on this row — open it for each one'
            : this.costTitle(eps[0].cost),
          verified: eps.some(e=>!!e.verified),
          ready: eps.some(e=>this.catEndpointConnected(e)),
          hay: (r.description+' '+r.domain+' '+(r.capability||'')+' '+eps.map(e=>
                 e.provider+' '+(e.provider_display||'')+' '+(e.name||'')+' '+e.path+' '+e.summary).join(' ')).toLowerCase()});
      }
      return out; },
// The text box and the verified checkbox narrow the row list; the domain chips then narrow it
    // again. Splitting it here is what lets each chip carry the count it would actually show.
    platRowsPreDomain(){
      const q=this.platQ.trim().toLowerCase();
      return this.platRowsAll.filter(r=>(!this.platVerifiedOnly||r.verified) && (!q||r.hay.includes(q))); },
platDomainTabs(){
      // Only the browse surface counts on the chips: management rows (account/utility) live behind
      // a per-section expander, not in the domain tally.
      const n={}; for(const r of this.platRowsPreDomain) if(!r.mgmt) n[r.domain]=(n[r.domain]||0)+1;
      // The server's order (busiest first, "other" last) is the one the chips keep — a chip bar
      // that reshuffles as you type is unusable.
      return (this.platData&&this.platData.domains||[]).filter(s=>n[s.domain]).map(s=>({domain:s.domain, n:n[s.domain]})); },
platLedger(){
      // Browse rows (data/action) make the domain sections; a section with no visible row does not
      // render AT ALL. Filing management rows per-domain conjured sections that existed only
      // because a hidden endpoint carried that capability id — CAMPAIGNS 0, SCHOOL 0, TITLE 0.
      const vis={};
      for(const r of this.platRowsPreDomain){
        if(r.mgmt || (this.platDomain && r.domain!==this.platDomain)) continue;
        (vis[r.domain]=vis[r.domain]||[]).push(r); }
      const out=(this.platData&&this.platData.domains||[]).filter(s=>vis[s.domain])
        .map(s=>({domain:s.domain, rows:vis[s.domain]}));
      // ...and every management endpoint on the platform lands in ONE collapsed section at the
      // bottom, its domain ignored: account/utility routes are the provider's own plumbing, and
      // which capability id they happen to carry is not a fact worth a heading.
      const acts=this.platActionRows;
      if(acts.length) out.push({domain:'Actions', actions:true, count:acts.length,
                                rows:this.platActionsOpen?acts:[]});
      return out; },
// What "All" counts: the same population the domain chips add up to. Counting the management
    // rows here made the All chip disagree with both the chips beside it and the stat line under it.
    platBrowseCount(){ return this.platRowsPreDomain.filter(r=>!r.mgmt).length; },
// Platform-wide, so a domain chip (which is a browse axis) hides it rather than filtering it.
    platActionRows(){
      return this.platDomain ? [] : this.platRowsPreDomain.filter(r=>r.mgmt); },
// Two numbers, because a merged row stands for several endpoints — the browse surface only, so
    // the header count never jumps when the management expander is opened.
    platStats(){
      let rows=0, eps=0;
      for(const r of this.platRowsPreDomain){ if(r.mgmt || (this.platDomain && r.domain!==this.platDomain)) continue;
        rows++; eps+=r.endpoints.length; }
      return {rows, eps}; },
catalogDeny(){ const c=this.tForm.cli; return (c && this.catalogClis && this.catalogClis[c.bin]) || []; },
catalogExtra(){  // catalog patterns not already in the own list — the union dedupes at run time, so showing both would double up
      const own=new Set((this.tForm.cli&&this.tForm.cli.deny||[]).map(p=>p.trim())); return this.catalogDeny.filter(p=>!own.has(p)); },
sortedInvites(){  // the clicked email link's team first, then newest-first (the API's order)
      return [...this.pendingInvites].sort((a,b)=>(b.org_id===this.inviteLinkOrg?1:0)-(a.org_id===this.inviteLinkOrg?1:0)); },
selectedInvites(){ return this.sortedInvites.filter(i=>this.inviteSel[i.id]); },
inviteFirstRun(){ return !this.myOrgs.some(o=>!this.isPersonal(o)); },
// no real team yet → decline offers create-team
    activityRows(){  // proxy calls + server CLI runs, one time-sorted feed (ISO strings compare fine)
      // Local runs now arrive via /runs (where:'local'); drop them from the calls feed so they aren't double-counted.
      // Cost precedence: what was CHARGED (settle amount; 0 on a release — the estimate alone would
      // over-report a refunded call as spend), else observed, else the estimate (old rows / own-key).
      // A metered async task (video/image generation) is the one row whose charge is decided AFTER
      // the call returned: the server nulls the charge while the task is pending, so show the hold.
      const calls=(this.calls||[]).filter(c=>c.kind!=='local_run').map(c=>({kind:'call', id:c.id, created_at:c.created_at, user_email:c.user_email, client:c.client, tool:c.tool_name, action:c.method, status:c.status_code, ok:c.status_code<400,
        task:c.async_task||null, held:!!(c.async_task&&c.async_task.status==='pending'),
        cost:(c.async_task&&c.async_task.status==='pending')?c.async_task.reserved_micro:(c.cost_charged_micro!=null?c.cost_charged_micro:(c.cost_observed_micro!=null?c.cost_observed_micro:c.cost_estimated_micro)), tier:c.credential_tier, tags:c.tags,
        has_result:!!c.has_result, endpoint_id:c.endpoint_id, call_ref:c.call_ref, path:c.path, cached:c.cached, api_key_id:c.api_key_id, api_key_name:c.api_key_name, api_key_prefix:c.api_key_prefix}));
      const runs=(this.runs||[]).map(r=>({kind:'run', where:r.where, id:r.id, created_at:r.created_at, user_email:r.user_email, client:r.client, tool:r.tool, action:(r.argv||[]).join(' ').slice(0,48)||'-', status:(r.where==='local'?'local run':'exit '+r.exit_code), ok:(r.where==='local'?true:r.exit_code===0), api_key_id:r.api_key_id, api_key_name:r.api_key_name, api_key_prefix:r.api_key_prefix}));
      return calls.concat(runs).sort((a,b)=>a.created_at<b.created_at?1:-1);
    },
activityCallCount(){ return this.activityRows.filter(a=>a.kind==='call').length; },
activityCachedCount(){ return this.activityRows.filter(a=>a.kind==='call' && a.cached).length; },
apiKeyGroups(){
      const groups=[], humanByIdentity={}, standaloneAgents={};
      for(const row of this.apiKeys||[]){
        if(row.assigned_type!=='human') continue;
        const identity=(row.identity||'').toLowerCase();
        let group=humanByIdentity[identity];
        if(!group){ group={identity:row.identity,name:row.assigned_name||row.identity,type:'human',rows:[]}; humanByIdentity[identity]=group; groups.push(group); }
        group.rows.push(row);
      }
      for(const row of this.apiKeys||[]){
        if(row.assigned_type!=='agent') continue;
        const owner=humanByIdentity[(row.created_by||'').toLowerCase()];
        if(owner){ owner.rows.push(row); continue; }
        let group=standaloneAgents[row.identity];
        if(!group){ group={identity:row.identity,name:row.assigned_name||row.identity,type:'agent',rows:[]}; standaloneAgents[row.identity]=group; groups.push(group); }
        group.rows.push(row);
      }
      return groups;
    },
activityShown(){ return this.actOkOnly ? this.activityRows.filter(a=>a.ok) : this.activityRows; },
callBodyPretty(){ const t=this.callView&&this.callView.response&&this.callView.response.body_text; return t?this.pretty(t):''; },
// JSON pretty-printed when it parses (a 2 MB parse is ~20 ms)
    callBodyTruncated(){ return !this.callViewFull && this.callBodyPretty.length>262144; },
// cap the RENDERED text at 256 KB unless asked for all of it
    callBodyShown(){ const t=this.callBodyPretty; return this.callBodyTruncated ? t.slice(0,262144)+'\n… (truncated)' : t; },
anyTagged(){  // hide the column entirely for teams that never send X-Treg-Meta
      return (this.calls||[]).some(c=>c.tags && Object.keys(c.tags).length);
    },
// Try-it drawer: the filled query string + the three "how to run it" recipes (agent / CLI / API)
    epTryAuthMethods(){ if(!this.epTry) return [];
      return [...new Set([this.epTry.authorization_method, ...(this.epTry.authorization_methods||[]),
        ...Object.keys(this.epTry.authorization_paths||{})].filter(Boolean))]; },
epTryConnectedMethods(){ return this.epTryAuthMethods.filter(m=>{
      const a=this.epTryAccessByMethod[m]; return a && (a.tier==='tool'||a.tier==='credential'); }); },
epTryShowAuthSelector(){ return this.epTryAuthMethods.length>1 && this.epTryConnectedMethods.length>1; },
epTryDisplayPath(){ if(!this.epTry) return '';
      if(this.epTry.id==='fishaudio.voices.list') return '/orgs/{org_id}/provider-resources?provider=fishaudio&kind=voice';
      return (this.epTry.authorization_paths||{})[this.epTryAuthMethod]||this.epTry.path; },
epTryVisibleParams(){
      if(this.epTry&&this.epTry.id==='fishaudio.voices.list') return [];
      return (this.epTryParams||[]).filter(p=>this.epTryParamAllowed(p));
    },
epTryQuery(){ return this.epTryVisibleParams.filter(p=>['query','path'].includes(p.location) && p.value!=='' && p.value!=null)
      .map(p=>encodeURIComponent(p.name)+'='+encodeURIComponent(p.value)).join('&'); },
epTryShellBody(){ return "'"+String(this.epTryBody).replace(/'/g,"'\"'\"'")+"'"; },
epTryCliCall(){ if(!this.epTry) return '';
      if(this.epTry.id==='fishaudio.voices.list') return 'treg resources list --provider fishaudio --kind voice';
      const args=this.epTryVisibleParams.filter(p=>['query','path'].includes(p.location) && p.value!=='' && p.value!=null)
        .map(p=>`--query ${p.name}=${/\s/.test(String(p.value))?JSON.stringify(String(p.value)):p.value}`).join(' ');
      const method=(this.epTry.method||'GET').toUpperCase();
      let s=`treg call ${this.epTry.id}${args?' '+args:''}`;
      if(method!=='GET') s+=` --method ${method}`;
      if(this.epTryAuthMethod) s+=` --authorization-method ${this.epTryAuthMethod}`;
      for(const p of this.epTryVisibleParams.filter(p=>p.location==='header'&&String(p.value)!==''))
        s+=` --header ${p.name}=${JSON.stringify(String(p.value))}`;
      if(this.epTryBodyType==='multipart'){
        for(const p of this.epTryMultipart.filter(p=>!String(p.type).includes('file')&&String(p.value)!==''))
          s+=` --form ${p.name}=${JSON.stringify(String(p.value))}`;
        for(const p of this.epTryMultipart.filter(p=>String(p.type).includes('file')))
          s+=` --upload ${p.name}=@${p.name==='voices'?'<reference-audio>':'<file>'}`;
      }else if(method!=='GET' && this.epTryBody.trim()) s+=` --data ${this.epTryShellBody}`;
      if(this.epTry.id==='fishaudio.tts.s2-1-pro') s+=' > speech.mp3';
      return s; },
epTryCurl(){ if(!this.epTry) return '';
      if(this.epTry.id==='fishaudio.voices.list'){
        const tok=this.myToken||'$TREG_TOKEN', org=this.activeOrgId||'$TREG_ORG_ID';
        let s=`curl "${this.proxy}/orgs/${org}/provider-resources?provider=fishaudio&kind=voice" \\\n+  -H "X-Treg-Token: ${tok}"`;
        if(this.sessionMode && this.activeSlugNow) s+=` \\\n+  -H "X-Treg-Org: ${this.activeSlugNow}"`;
        return s;
      }
      const q=this.epTryQuery; const url=`${this.proxy}/call/${this.epTry.id}${q?'?'+q:''}`;
      const tok=this.myToken||'$TREG_TOKEN', method=(this.epTry.method||'GET').toUpperCase();
      let s=`curl -X ${method} "${url}" \\\n  -H "X-Treg-Token: ${tok}"`;
      if(this.sessionMode && this.activeSlugNow) s+=` \\\n  -H "X-Treg-Org: ${this.activeSlugNow}"`;  // minted identity token needs the org header
      if(this.epTryAuthMethod) s+=` \\\n  -H "X-Treg-Authorization-Method: ${this.epTryAuthMethod}"`;
      for(const p of this.epTryVisibleParams.filter(p=>p.location==='header'&&String(p.value)!=='')) s+=` \\\n  -H "${p.name}: ${String(p.value).replace(/"/g,'\\"')}"`;
      if(this.epTryBodyType==='multipart'){
        for(const p of this.epTryMultipart.filter(p=>!String(p.type).includes('file')&&String(p.value)!=='')) s+=` \\\n  -F "${p.name}=${String(p.value).replace(/"/g,'\\"')}"`;
        for(const p of this.epTryMultipart.filter(p=>String(p.type).includes('file'))) s+=` \\\n  -F "${p.name}=@${p.name==='voices'?'<reference-audio>':'<file>'}"`;
      }else if(method!=='GET' && this.epTryBody.trim()) s+=` \\\n  -H "Content-Type: application/json" \\\n  --data ${this.epTryShellBody}`;
      if(this.epTry.id==='fishaudio.tts.s2-1-pro') s+=' \\\n  --output speech.mp3';
      return s; },
// token + team embedded HERE ONLY (a copy-and-run-now context) — the setup line elsewhere stays clean
    epTrySetupLine(){ const S=this.activeSlugNow||'<team-slug>', T=this.myToken||'<YOUR_TOKEN>';
      return `set up treg — ${this.proxy}/llms.txt with team ${S} token: ${T}`; },
epTryAgentUse(){ if(!this.epTry) return '';
      if(this.epTry.id==='fishaudio.voices.list') return 'Use treg resources_list with provider=fishaudio and kind=voice. It returns the connected Fish account when BYOK exists, otherwise this team’s platform-created voices.';
      const what=this.epTry.summary ? this.epTry.summary.replace(/\.$/,'') : this.epTry.id;
      const auth=this.epTryAuthMethod ? ` Use authorization_method=${this.epTryAuthMethod}.` : '';
      return `Use treg to call ${this.epTry.id} — ${what}.${auth}`; }
}
