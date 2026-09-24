
export default {
async loadUsage(){ if(!this.canAdmin || !this.activeOrgId) return; this.usage=null;
      try{ this.usage=await this.api('/orgs/'+this.activeOrgId+'/usage?days='+this.usageDays); }
      catch(e){ this.err='Failed to load usage: '+(e.detail||e.status); }
      await this.loadTagUsage(); },
async loadTagUsage(){ if(!this.canAdmin || !this.activeOrgId) return;
      this.tagUsage={};
      try{
        // What the team has actually SENT. Reporting works on any key, unlike enforcement, which
        // needs a declared one — so this is deliberately NOT the budgetable list, which hid
        // `feature=` and friends from the dashboard even though the API served them fine.
        const k=await this.api('/orgs/'+this.activeOrgId+'/tag-keys');
        const primary=k.primary;
        const keys=[...new Set([...(k.seen||[]), ...(k.budgetable||[])].filter(Boolean))];
        // Primary first — it is the one a reselling team bills on; the rest alphabetically.
        keys.sort((a,b)=> a===primary ? -1 : b===primary ? 1 : a.localeCompare(b));
        this.tagKeys=keys;
        // One request per key. Bounded by the 5-key cap on the header, and they run together.
        const got=await Promise.all(keys.map(key=>
          this.api('/orgs/'+this.activeOrgId+'/usage/by-tag?key='+encodeURIComponent(key)+
                   '&days='+this.usageDays).catch(()=>null)));
        const out={};
        keys.forEach((key,i)=>{ if(got[i]) out[key]=got[i]; });
        this.tagUsage=out;
      }catch(e){ this.tagUsage={}; this.tagKeys=[]; } },
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
async loadCalls(){ try{ if(!this.apiKeys.length)await this.loadApiKeys(); const q='?limit=100'+(this.activityKey?'&api_key_id='+encodeURIComponent(this.activityKey):''); const [calls,runs]=await Promise.all([this.api('/calls'+q), this.api('/runs'+q).catch(()=>[])]); this.calls=calls; this.runs=runs; }catch(e){ this.err='Failed to load activity.'; } }
}
