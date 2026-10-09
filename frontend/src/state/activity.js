
export default {
// The Usage window as a query: a preset (`days=30`) or a custom range (`from=…&to=…`, UTC days).
usageQuery(){ const r=this.usageRange; return r.preset==='custom' ? 'from='+r.from+'&to='+r.to : 'days='+r.preset; },
// Every section loads on its own and shows itself when ready: the page never waits for the slowest
// one, and a failed section does not take the others with it.
async loadUsage(){ if(!this.canAdmin || !this.activeOrgId) return;
      this.usageToolPage=0; this.usageDayPage=0;
      await Promise.all([this.loadUsageCounts(), this.loadUsageSpend(), this.loadTagUsage()]); },
async loadUsageCounts(){ if(!this.canAdmin || !this.activeOrgId) return; this.usage=null; this.usageErr=''; const live=this.ticket('usage');
      try{ const usage=await this.api('/orgs/'+this.activeOrgId+'/usage?'+this.usageQuery()); if(live()) this.usage=usage; }
      catch(e){ if(live()) this.usageErr='Could not load usage counts: '+(e.detail||e.status); } },
// The spend chart keeps its last answer on screen (dimmed) while a filter change loads the next.
async loadUsageSpend(){ if(!this.canAdmin || !this.activeOrgId) return; const live=this.ticket('usageSpend'), f=this.spendFilter;
      if(!f.provider) f.stack='key';
      const q=this.usageQuery()+'&group='+f.group+'&stack='+f.stack+(f.key?'&key='+f.key:'')+(f.provider?'&provider='+encodeURIComponent(f.provider):'');
      this.usageSpendBusy=true; this.usageSpendErr=''; this.spendRankPage=0;
      try{ const spend=await this.api('/orgs/'+this.activeOrgId+'/usage/spend?'+q); if(live()) this.usageSpend=spend; }
      catch(e){ if(live()) this.usageSpendErr='Could not load spend: '+(e.detail||e.status); }
      finally{ if(live()) this.usageSpendBusy=false; } },
setUsageRange(value){ this.usageRange=value; this.loadUsage(); },
async loadTagUsage(){ if(!this.canAdmin || !this.activeOrgId) return;
      this.tagUsage={}; const live=this.ticket('tagUsage'), org=this.activeOrgId;
      try{
        // What the team has actually SENT. Reporting works on any key, unlike enforcement, which
        // needs a declared one — so this is deliberately NOT the budgetable list, which hid
        // `feature=` and friends from the dashboard even though the API served them fine.
        const k=await this.api('/orgs/'+org+'/tag-keys');
        if(!live()) return;
        const primary=k.primary;
        const keys=[...new Set([...(k.seen||[]), ...(k.budgetable||[])].filter(Boolean))];
        // Primary first — it is the one a reselling team bills on; the rest alphabetically.
        keys.sort((a,b)=> a===primary ? -1 : b===primary ? 1 : a.localeCompare(b));
        this.tagKeys=keys;
        // One request per key. Bounded by the 5-key cap on the header, and they run together.
        const got=await Promise.all(keys.map(key=>
          this.api('/orgs/'+org+'/usage/by-tag?key='+encodeURIComponent(key)+
                   '&'+this.usageQuery()).catch(()=>null)));
        if(!live()) return;
        const out={};
        keys.forEach((key,i)=>{ if(got[i]) out[key]=got[i]; });
        this.tagUsage=out;
      }catch(e){ if(live()){ this.tagUsage={}; this.tagKeys=[]; } } },
// micro-USD -> a money string, EXACT. Cents round a $0.0006 charge to $0.00 and a $9.999 balance
    // to "$10.00"; a fixed 4 decimals rounds $0.00015 to $0.0001 (float: 0.00015 is stored just
    // under). So: whole-cent amounts render as normal cents, everything else renders all 6 micro
    // digits with trailing zeros trimmed — a micro amount is always representable. Mirrors cli._usd.
    // Async generation tasks on the Activity feed: the state chip and the artifact link's tooltip.
    taskStateLabel(t){ return {pending:'generating…', settled:'done', released:'failed · refunded', timed_out:'timed out'}[t.status]||t.status; },
taskStateTitle(t){
      if(t.status==='pending') return 'the task is still running upstream; the hold settles or is refunded when it finishes';
      if(t.status==='settled') return 'the task succeeded; charged '+this.money(t.settled_micro)+(t.completed_at?' at '+new Date(t.completed_at+(/(Z|[+-]\d{2}:?\d{2})$/.test(t.completed_at)?'':'Z')).toLocaleString():'');
      if(t.status==='released') return 'the provider reported the task failed; the whole hold was refunded'+(t.error?' - '+t.error:'');
      if(t.status==='timed_out') return 'no terminal state within 24 hours; the whole hold was refunded and treg absorbed any upstream charge (flagged for review)';
      return t.status;
    },
taskArtifactTitle(t){ return 'the provider\'s download URL - time-limited'+(t.ttl_note?' (lifetime: '+t.ttl_note+')':'')+', download promptly'+(t.completed_at?'; generated '+this.when(t.completed_at):''); },
async openCall(a){  // Activity row → the drawer; details load from /calls/{id}/result
      this.callViewFull=false; this.callCopied='';
      // `task` rides along from the row: a generation's artifact (the provider's time-limited URL)
      // lives on the async task, not in the archived submission body, and the drawer shows it inline.
      this.callView={id:a.id, tool:a.tool, endpoint_id:a.endpoint_id, call_ref:a.call_ref, created_at:a.created_at, status_code:a.status, credential_tier:a.tier, cached:a.cached, task:a.task||null, loading:true};
      try{ const d=await this.api('/calls/'+a.id+'/result'); this.callView={...this.callView, ...d, loading:false}; }
      catch(e){ this.callView={...this.callView, loading:false, error:'Could not load this call.'}; }
    },
pretty(text){ try{ return JSON.stringify(JSON.parse(text), null, 2); }catch(e){ return text; } },
isVideoUrl(u){ try{ return /\.(mp4|webm|mov|m4v)$/i.test(new URL(u).pathname); }catch(e){ return /\.(mp4|webm|mov|m4v)(\?|$)/i.test(u||''); } },
fmtBytes(n){ if(n==null) return '—'; if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB'; return (n/1048576).toFixed(2)+' MB'; },
async copyCallBody(){ const t=this.callView&&this.callView.response&&this.callView.response.body_text; if(!t) return; if(await this.toClipboard(t)){ this.callCopied='Copied'; setTimeout(()=>{ this.callCopied=''; },1400); } },
async loadCalls(){ const live=this.ticket('calls');
      // A reload supersedes an older page in flight, and its cursor belongs to the rows being replaced.
      this.ticket('activityOlder'); this.activityOlderBusy=false; this.activityNext=null;
      try{ const [,page]=await Promise.all([this.apiKeys.length?null:this.loadApiKeys(), this.api('/activity'+this.activityQuery())]);
        if(!live()) return; this.calls=[]; this.runs=[]; this.takeActivity(page); }
      catch(e){ if(live()){ this.calls=[]; this.runs=[]; this.err='Failed to load activity.'; } }
      finally{ if(live()) this.callsLoaded=true; } },  // the empty-state line waits for this, not for the page's global `loading`
activityQuery(before){ return '?limit=100'+(this.activityKey?'&api_key_id='+encodeURIComponent(this.activityKey):'')+(before?'&before='+encodeURIComponent(before):''); },
takeActivity(page){  // one newest-first page of the server's merged feed; `next` is the cursor past it
      this.calls=this.calls.concat(page.rows.filter(r=>r.source==='call'));
      this.runs=this.runs.concat(page.rows.filter(r=>r.source==='run'));
      this.activityNext=page.next; },
async loadOlderActivity(){
      if(this.activityOlderBusy||!this.activityNext) return; const live=this.ticket('activityOlder'); this.activityOlderBusy=true;
      try{ const page=await this.api('/activity'+this.activityQuery(this.activityNext)); if(live()) this.takeActivity(page); }
      catch(e){ if(live()) this.err='Failed to load older activity.'; }
      finally{ if(live()) this.activityOlderBusy=false; } }
}
