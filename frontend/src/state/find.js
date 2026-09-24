// "Find tools for a job" (GET /catalog/find): the Catalog page's search box answers a described job,
// and /search is the same answer on a public page. The route streams two NDJSON events -
// `candidates` (the lexical recall, at once) and `judged` (the relevance judge's kept rows) - and
// both pages draw the wait on the first one. State lives in `find` (data.js); the in-flight request's
// AbortController lives in `elements` because it is a handle, not state to render.
// The state of no search; `high` is the server's strong cut and arrives with each answer.
export const FIND_EMPTY = {q:'', phase:'idle', candidates:[], rows:[], verdict:'', read:0, high:1, error:''}
const FIND_OPEN = 'treg-find-open'

// A short query is a NAME ("tiktok") and keeps the instant platform filter; a sentence is a JOB.
export function isJobQuery(text){
  const t=String(text||'').trim();
  return t.split(/\s+/).filter(Boolean).length>=4 || /\?$/.test(t);
}

// Group items under a key, into each group's `field` list; a group's fit is its best member's, and groups sort best first unless
// the answer is unjudged (keyword order is the server's). Shared by the Catalog list (per job) and
// the /search cards (per platform).
export function groupBest(items, verdict, keyOf, make, field){
  const by=new Map();
  for(const it of items){
    const key=keyOf(it);
    let g=by.get(key);
    if(!g){ g={...make(it, key), p:null, [field]:[]}; by.set(key, g); }
    g[field].push(it);
    if(it.p!=null && (g.p==null || it.p>g.p)) g.p=it.p;
  }
  const out=[...by.values()];
  if(verdict!=='keyword') out.sort((a,b)=>(b.p||0)-(a.p||0));
  return out;
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

  async findRun(text){
    const q=String(text||'').trim();
    if(!q) return;
    this.elements.findAbort?.abort?.();
    const ctl=new AbortController();
    this.elements.findAbort=ctl;
    this.find={...FIND_EMPTY, q, phase:'recall'};
    this.loadPlatforms();
    this.track('catalog_find', {surface:this.view==='find'?'search':'catalog', words:q.split(/\s+/).length});
    try{
      const res=await fetch('/catalog/find?q='+encodeURIComponent(q), {signal:ctl.signal, credentials:'include',
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
        else if(ev.event==='judged') this.find={...this.find, phase:'done', rows:ev.rows||[], verdict:ev.verdict,
          read:ev.read||0, high:ev.high??1};
      }
      if(this.find.phase!=='done' && !ctl.signal.aborted) this.find={...this.find, phase:'error', error:'The answer was cut off. Try again.'};
    }catch(e){
      if(ctl.signal.aborted) return;
      this.find={...this.find, phase:'error', error:'Could not reach the catalog. Check your connection and try again.'};
    }
  },

  findExit(){
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
    this.findCopied=key;
    setTimeout(()=>{ if(this.findCopied===key) this.findCopied=''; }, 1600);
  },

  // A judged job under the server's strong cut draws lighter; keyword rows carry no fit to judge.
  findWeak(g){ return this.find.verdict!=='keyword' && (g.p==null || g.p<this.find.high); },

  // The cheapest line of a job, priced the way every other catalog price is (`capCheapest`).
  findPrice(g){ return this.capCheapest(g.rows)?.label || ''; },

  // Open the platform shelf the row lives on, with its ledger filtered to this job's capability.
  findOpen(group){
    this.openPlatform(group.platform);
    this.platQ=group.rows[0]?.name || group.label;
  },

  // From /search into the dashboard: the platform's page. Signed
  // out, sign-in comes first; the destination waits in localStorage and boot (`findResume`)
  // continues there whichever way sign-in returns (email reloads in place, OAuth lands on /app).
  findGoDashboard(slug){
    try{ localStorage.setItem(FIND_OPEN, JSON.stringify({slug, t:Date.now()})); }catch(e){}
    if(this.authed) location.href='/app#platform/'+encodeURIComponent(slug);
    else this.openSignin();
  },

  // Called by boot once a session exists. Returns true when it navigated away.
  findResume(){
    let open=null;
    try{ open=JSON.parse(localStorage.getItem(FIND_OPEN)||'null'); }catch(e){}
    if(!open || Date.now()-open.t>10*60*1000){ try{ localStorage.removeItem(FIND_OPEN); }catch(e){} return false; }
    if(this.platformFromHash()!==open.slug){ location.replace('/app#platform/'+encodeURIComponent(open.slug)); return true; }
    try{ localStorage.removeItem(FIND_OPEN); }catch(e){}
    this.openPlatform(open.slug, true);
    return true;
  },

  findRequestTool(){
    this.openToolRequest();
    this.reqForm.capability=this.find.q;
  },
}
