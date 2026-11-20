import { storageGet, storageSet, storageRemove } from './storage.js'
// "Find tools for a job" (GET /catalog/find): the Catalog page's search box answers a described job,
// and /search is the same answer on a public page. The route streams two NDJSON events -
// `candidates` (the lexical recall, at once) and `judged` (the relevance judge's kept rows) - and
// both pages draw the wait on the first one. State lives in `find` (data.js); the in-flight request's
// AbortController lives in `elements` because it is a handle, not state to render.
// The state of no search; `high` is the server's strong cut and arrives with each answer.
// `reason` says why a `none` is empty (gap: the catalog lacks it; not_task; v2 only).
export const FIND_EMPTY = {q:'', scope:'', phase:'idle', candidates:[], rows:[], verdict:'', named:'', read:0, high:1, error:'', auto:false,
  reason:''}
const FIND_OPEN = 'treg-find-open'
// The Catalog box searches by itself once typing pauses this long: people did not discover Enter.
const FIND_DEBOUNCE_MS = 700
const FIND_MIN_CHARS = 2

// A short query is a NAME ("tiktok") and keeps the instant platform filter; a sentence is a JOB.
export function isJobQuery(text){
  const t=String(text||'').trim();
  return t.split(/\s+/).filter(Boolean).length>=4 || /\?$/.test(t);
}

// Group items under a key, into each group's `field` list; a group's fit is its best member's, and
// groups sort best first. Unjudged rows (the keyword page, a bare name's answer) carry no fit, so
// the stable sort keeps the server's order for them.
export function groupBest(items, keyOf, make, field){
  const by=new Map();
  for(const it of items){
    const key=keyOf(it);
    let g=by.get(key);
    if(!g){ g={...make(it, key), p:null, [field]:[]}; by.set(key, g); }
    g[field].push(it);
    if(it.p!=null && (g.p==null || it.p>g.p)) g.p=it.p;
  }
  return [...by.values()].sort((a,b)=>(b.p||0)-(a.p||0));
}

// Rows grouped by job: one group per capability on a platform (an uncatalogued endpoint is its own
// job), its providers in the server's order. Shared by the Catalog list and the /search cards.
// `hidden` counts the providers the server folded away (a job under the strong cut shows one row
// for each of its first five); `fitFrom` says whether the group's fit is the job's or one vendor's
// own (`fit_from`).
export function jobGroups(rows){
  return groupBest(rows, r=>(r.capability||r.id)+'|'+r.platform,
    (r, key)=>({key, label:r.capability_description||r.name, platform:r.platform, platform_label:r.platform_label}), 'rows')
    .map(g=>{
      const best=g.rows.reduce((b, r)=>r.p!=null && (b==null || r.p>b.p) ? r : b, null);
      return {...g, hidden:g.rows.reduce((n, r)=>n+(r.children_hidden||0), 0), fitFrom:best?.fit_from||''};
    });
}

// Why an answer came back empty, in the person's terms: a gap is something treg does not carry yet;
// anything else did not read as a job.
export function findNoneText(reason){
  return reason==='gap'
    ? 'treg does not have this kind of data or action yet. We have noted it.'
    : 'This does not read as a job. Try describing the data you want or the action to take.';
}

async function* ndjson(res){
  const reader=res.body.getReader(), dec=new TextDecoder();
  let buf='';
  for(;;){
    const {done, value}=await reader.read();
    if(value) buf+=dec.decode(value, {stream:true});
    let nl;
    while((nl=buf.indexOf('\n'))>=0){ const line=buf.slice(0,nl).trim(); buf=buf.slice(nl+1); if(line) yield JSON.parse(line); }
    if(done){ if(buf.trim()) yield JSON.parse(buf); return; }
  }
}

export default {
  findIsJob(text){ return isJobQuery(text); },

  // Typing in the Catalog box: an answer for older text gives way at once (so a name filters the
  // shelves as you type), and the finder runs when typing pauses on two characters or more. Its
  // answer is `auto`: it sits above the still-filtered shelves instead of replacing them; Enter asks
  // for the full answer. `findSoon` is true while one is scheduled, so the page does not call a
  // half-typed job "no platform". `scope` is a platform slug when the box is a shelf's own: the
  // finder then reads that shelf only.
  findSchedule(text, scope=''){
    this.findUnschedule();
    const q=String(text||'').trim();
    if(!q){ this.findExit(); return; }
    if(this.findActive && (q!==this.find.q || scope!==this.find.scope)) this.findExit();
    if(q.length<FIND_MIN_CHARS || (q===this.find.q && scope===this.find.scope)) return;
    this.findSoon=true;
    this.elements.findTimer=setTimeout(()=>this.findRun(q, {auto:true, scope}), FIND_DEBOUNCE_MS);
  },

  findUnschedule(){
    clearTimeout(this.elements.findTimer);
    this.elements.findTimer=null;
    this.findSoon=false;
  },

  async findRun(text, {auto=false, scope=''}={}){
    this.findUnschedule();
    const q=String(text||'').trim();
    if(!q) return;
    this.elements.findAbort?.abort?.();
    const ctl=new AbortController();
    this.elements.findAbort=ctl;
    this.find={...FIND_EMPTY, q, scope, phase:'recall', auto};
    this.loadPlatforms();
    this.track('catalog_find', {surface:this.findSurface(), words:q.split(/\s+/).length, auto, ...(scope ? {platform:scope} : {})});
    try{
      const res=await fetch('/catalog/find?q='+encodeURIComponent(q)+(scope ? '&platform='+encodeURIComponent(scope) : ''), {signal:ctl.signal, credentials:'include',
        headers:{'accept':'application/x-ndjson','ngrok-skip-browser-warning':'1'}});
      if(!res.ok){
        let detail=''; try{ detail=(await res.json()).detail||''; }catch(e){}
        this.find={...this.find, phase:'error', error: res.status===429
          ? 'That is a lot of searches from here this hour. Try again later, or browse the platforms below.'
          : res.status===503 ? 'Finding tools by description is not available on this server.'
          : (detail||'The search did not go through ('+res.status+').')};
        return;
      }
      for await (const ev of ndjson(res)){
        if(ctl.signal.aborted) return;
        if(ev.event==='candidates') this.find={...this.find, phase:'reading', candidates:ev.candidates||[]};
        else if(ev.event==='judged'){
          this.find={...this.find, phase:'done', rows:ev.rows||[], verdict:ev.verdict, named:ev.named||'', read:ev.read||0, high:ev.high??1,
            reason:ev.reason||''};
          this.track('search_answered', {surface:this.findSurface(), verdict:ev.verdict, results:this.find.rows.length,
            providers:new Set(this.find.rows.map(r=>r.provider)).size, top_fit:this.find.rows[0]?.p ?? null, auto,
            engine:ev.engine||'v1', reason:ev.reason||null, platform_choice:ev.platform?.choice ?? null});
        }
      }
      if(this.find.phase!=='done' && !ctl.signal.aborted) this.find={...this.find, phase:'error', error:'The answer was cut off. Try again.'};
    }catch(e){
      if(ctl.signal.aborted) return;
      this.find={...this.find, phase:'error', error:'Could not reach the catalog. Check your connection and try again.'};
    }
  },

  // From a shelf's answer to the whole catalog: the Catalog page, its box holding the same words,
  // asked again unscoped. A shelf's find read that shelf only; this is how its reader asks everywhere.
  findEverywhere(){
    const q=this.find.q;
    this.findExit();
    this.q=q;
    this.go('catalog');
    this.findRun(q, {scope:''});
  },

  findExit(){
    this.findUnschedule();
    this.elements.findAbort?.abort?.();
    this.elements.findAbort=null;
    this.find={...FIND_EMPTY};
  },

  // What an agent needs to act on the answer: the job in the person's words, the endpoint ids that
  // fit (best first, one per capability unless asked for a whole group), and where treg starts.
  findAgentPrompt(groups){
    const lines=groups.flatMap(g=>g.rows.slice(0,3).map(r=>{
      const price=this.capCheapest([r])?.label;
      return '- '+r.id+' ('+(r.provider_display||r.provider)+(price ? ', '+price : '')+'): '+(r.capability_description||r.name);
    }));
    return 'Use treg to do this: '+this.find.q+'\n\nTools in the treg catalog that fit the job:\n'+lines.join('\n')
      +'\n\nIf treg is not set up yet, read '+(this.proxy||location.origin)+'/llms.txt first. Check each tool with `treg catalog get <id>` before calling it.';
  },

  // What "Copy for your agent" hands over for the whole answer: every strong fit, topped up to
  // five jobs with the next best, so a single strong row still comes with its useful neighbours.
  findCopyAll(){
    const g=this.findGroups;
    const n=Math.max(this.findStrong.length, Math.min(5, g.length));
    return this.findCopy(g.slice(0,n), 'all');
  },

  findCopy(groups, key){ return this.findCopyText(this.findAgentPrompt(groups), key); },
  findShare(){ return this.findCopyText(location.origin+'/search?q='+encodeURIComponent(this.find.q), 'share'); },

  // `findCopied` names the button that just copied, for its "Copied" label. It lives outside `find`
  // so a label change does not replace the answer (and re-land its tiles on /search).
  async findCopyText(text, key){
    if(!(await this.toClipboard(text))) return;
    this.track('search_copied', {surface:this.findSurface(), scope:key==='all' || key==='share' ? key : 'job', verdict:this.find.verdict});
    this.findCopied=key;
    setTimeout(()=>{ if(this.findCopied===key) this.findCopied=''; }, 1600);
  },

  // A judged job under the server's strong cut draws lighter; unjudged rows carry no fit to judge.
  findWeak(g){ return g.p!=null && g.p<this.find.high; },

  // The distinct vendors selling one job.
  findProviders(g){ return [...new Set(g.rows.map(r=>r.provider))]; },

  // "5 providers", or "5 of 23 providers" when the server folded the rest away.
  findProvidersText(g){
    const n=this.findProviders(g).length, all=n+(g.hidden||0);
    return (g.hidden ? n+' of '+all : n)+' provider'+(all===1?'':'s');
  },

  // The fit's hover text: the job's fit, or one vendor's own words judged on their own.
  findFitTitle(g){
    if(g.p==null) return '';
    const pct=Math.round(g.p*100)+'%';
    return g.fitFrom==='endpoint' ? 'Fit of one provider\'s own tool: '+pct : 'Fit for this job: '+pct;
  },

  findNoneText(){ return findNoneText(this.find.reason); },

  // The cheapest line of a job, priced the way every other catalog price is (`capCheapest`).
  findPrice(g){ return this.capCheapest(g.rows)?.label || ''; },

  // Analytics: which page a find ran on. /search is the public page; the Catalog box is the other.
  findSurface(){ return this.view==='find' ? 'search' : this.view==='platform' ? 'platform' : 'catalog'; },

  // One answer row, card or pile tile followed out of a find (`search_result_clicked`): what it was
  // (`from`: card | job | tile), its platform and vendor, and its place in the answer.
  findTrackClick(from, platform, extra={}){
    this.track('search_result_clicked', {surface:this.findSurface(), from, platform, verdict:this.find.verdict,
      signed_in:!!this.authed, ...extra});
  },

  // Open the job a row is: its comparison on its platform when that shelf compares it (several
  // providers), else the tool itself in the drawer. From another page the shelf is opened first and
  // the job chosen once its data is in, the comparison replacing the shelf's history entry so Back
  // returns to where the answer was. The shelf's own box is never filled with the row's name.
  async findOpen(group, rank){
    this.findTrackClick('job', group.platform, {provider:group.rows[0]?.provider, rank});
    const slug=group.platform, cap=group.rows[0]?.capability, id=group.rows[0]?.id;
    const here=this.view==='platform' && slug===this.platSlug;
    if(!here){
      await this.openPlatform(slug);
      if(this.view!=='platform' || this.platSlug!==slug) return;     // the person moved on meanwhile
    }
    const job=cap && this.platComparisons.find(j=>j.key===cap);
    if(!job){ this.openTool(id); return; }
    if(here){ this.openComparison(job.slug); return; }
    this.openPlatform(slug, true, job.slug);
    history.replaceState({platform:slug}, '', this.platUrl(slug, job.slug));
  },

  // From /search into the dashboard: the platform's page. Signed
  // out, sign-in comes first; the destination waits in localStorage and boot (`findResume`)
  // continues there whichever way sign-in returns (email reloads in place, OAuth lands on /app).
  findGoDashboard(slug){
    storageSet(FIND_OPEN, JSON.stringify({slug, t:Date.now()}));
    if(this.authed) location.href='/app#platform/'+encodeURIComponent(slug);
    else this.openSignin();
  },

  // Called by boot once a session exists. Returns true when it navigated away.
  findResume(){
    let open=null;
    try{ open=JSON.parse(storageGet(FIND_OPEN)||'null'); }catch(e){}
    if(!open || Date.now()-open.t>10*60*1000){ storageRemove(FIND_OPEN); return false; }
    if(this.platformFromHash()!==open.slug){ location.replace('/app#platform/'+encodeURIComponent(open.slug)); return true; }
    storageRemove(FIND_OPEN);
    this.openPlatform(open.slug, true);
    return true;
  },

  findRequestTool(){
    this.openToolRequest();
    this.reqForm.capability=this.find.q;
  },
}
