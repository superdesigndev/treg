// The tool hub (docs/context/architecture/hub.md): the maker's view of the team's hub tools and the
// run page. Moved from the legacy dashboard with its behavior unchanged. `hubOn` comes from the
// server: /hub/tools/mine answers 404 when the hub is off, or on but not for this team
// (TREG_HUB_TEAMS), so the Hub entry follows the ACTIVE team and is probed again on a team switch.
export default {
async probeHub(){ try{ const r=await fetch('/hub/tools/mine',{credentials:'include', headers:this.headers()}); this.hubOn=r.status!==404; }catch(e){ this.hubOn=false; }
      if(!this.hubOn && (this.view==='hub' || this.view==='run')) this.go('start'); },
runFromPath(path){ const m=/^\/app\/runs\/([A-Za-z0-9_:-]+)$/.exec(path||''); return m ? m[1] : null; },
async loadHub(){ if(!this.authed) return; this.hub={...this.hub, loading:true, err:''};
  try{ const tools=await this.api('/hub/tools/mine'); this.hub={...this.hub, tools, loading:false};
       if(this.hub.tool){ const nt=tools.find(x=>x.tool_id===this.hub.tool.tool_id); if(nt) this.hub.tool=nt; } }
  catch(e){ this.hub={...this.hub, loading:false, err:this.hubErr(e)}; } },
hubNewest(){ const seen={}; return (this.hub.tools||[]).filter(t=>{ if(seen[t.tool_id]) return false; seen[t.tool_id]=true; return true; }); },
hubVersions(id){ return (this.hub.tools||[]).filter(t=>t.tool_id===id); },
hubLive(){ return this.hubNewest().filter(t=>t.status==='live').length; },
hubEarned30(){ return this.hubNewest().reduce((a,t)=>a+(t.earned_30d_micro||0),0); },
hubRuns30(){ return this.hubNewest().reduce((a,t)=>a+(t.runs_30d||0),0); },
hubFailing(){ return this.hubNewest().filter(t=>t.health==='failing').length; },
async openHubTool(t, tab){ this.hub={...this.hub, tool:t, tab:tab||'overview', earnings:null, health:null, runOpen:null};
  await Promise.all([this.loadHubEarnings(), this.loadHubHealth()]); },
closeHubTool(){ this.hub={...this.hub, tool:null, runOpen:null}; },
async loadHubEarnings(){ if(!this.hub.tool) return; try{ this.hub.earnings=await this.api('/hub/tools/'+encodeURIComponent(this.hub.tool.tool_id)+'/earnings?days=90'); }catch(e){ this.hub.earnings=null; } },
async loadHubHealth(){ if(!this.hub.tool) return; try{ this.hub.health=await this.api('/hub/tools/'+encodeURIComponent(this.hub.tool.tool_id)+'/health'); }catch(e){ this.hub.health=null; } },
hubRunCost(t){ const lo=t.price_low_micro, hi=t.price_high_micro;
  if(lo==null||hi==null) return '—';
  return lo===hi ? this.money(lo) : this.money(lo)+'–'+this.money(hi); },
hubPriceExplain(t){ const p=t.pricing||{};
  if(p.mode==='charge') return p.max_price_usd ? 'Your script sets each run\'s price with ctx.charge(usd, note), at most $'+p.max_price_usd+' a run. A failed run charges nothing.'
    : 'Free: the script declares no max_price_usd, so ctx.charge is refused. Callers pay only the provider fees.';
  return Number(p.price_usd||0) ? 'You earn $'+p.price_usd+' on each successful run.' : 'Free: callers pay only the provider fees.'; },
hubPricePrompt(t){ return 'Change the price of my treg hub tool '+t.tool_id+' (now: '+t.price_label+').\n'
  +'New price: <describe it, e.g. "$0.05 per call", "$0.01 per result, at most $0.50 a run", "15% on top of the provider fees">.\n\n'
  +(t.kind==='script'
    ? 'It is a script: set `"pricing": {"max_price_usd": N}` in recipe.json (the most one run may charge) and bill in run.js with ctx.charge(usd, note): a fee, per result (rows.length * 0.01), or a margin (r.cost_usd * 0.15 on a ctx.call result). '
    : 'It is a JSON steps recipe: set `"pricing": {"price_usd": N}` in recipe.json, a fixed price per successful run. A variable price needs a script and ctx.charge. ')
  +'Work in the folder it was published from, then run `treg hub publish <folder>` and confirm the new version is live (`treg hub ls`).'; },
async setHubFlag(field, value){ if(!this.hub.tool) return; this.hub.flagSaving=true; this.hub.err='';
  try{ const d=await this.api('/hub/tools/'+encodeURIComponent(this.hub.tool.tool_id), {method:'PATCH', headers:{'content-type':'application/json'}, body:JSON.stringify({[field]:!!value})});
       this.hub.tool={...this.hub.tool, ...(field==='listed'?{listed:d.listed, listing:d.listing}:{[field]:d[field]})}; await this.loadHub();
       this.hub.note=field==='listed'?this.hubListingWords({listing:d.listing})
                                     :(d.public_log?'Public run log on.':'Public run log off.');
       setTimeout(()=>{ this.hub.note=''; }, 2500); }
  catch(e){ this.hub.err=this.hubErr(e); await this.loadHub(); }
  this.hub.flagSaving=false; },
async retireHubTool(){ if(!this.hub.tool) return;
  if(this.hub.confirmRetire!==this.hub.tool.tool_id){ this.hub.confirmRetire=this.hub.tool.tool_id; return; }
  this.hub.confirmRetire=null;
  try{ await this.api('/hub/tools/'+encodeURIComponent(this.hub.tool.tool_id), {method:'DELETE'}); this.closeHubTool(); await this.loadHub(); this.hub.note='Retired: off the call road; history kept.'; }
  catch(e){ this.hub.err=this.hubErr(e); } },
hubCallLine(t){ const ex={}; Object.entries(t.inputs||{}).forEach(([k,v])=>{ if(v.example!==undefined) ex[k]=v.example; }); return 'treg call '+t.tool_id+" --data '"+JSON.stringify(ex)+"'"; },
hubPage(t){ return (this.proxy||location.origin)+'/hub/'+t.tool_id; },
async copyText(s, what){ try{ await navigator.clipboard.writeText(s); this.hub.note='Copied '+(what||'')+'.'; setTimeout(()=>{ this.hub.note=''; }, 1600); }catch(e){} },
async toggleHubRun(r){ if(this.hub.runOpen===r.run_id){ this.hub.runOpen=null; return; } this.hub.runOpen=r.run_id;
  if(!r.detail){ try{ r.detail=await this.api('/hub/runs/'+encodeURIComponent(r.run_id)); }catch(e){ r.detail={error:{message:this.hubErr(e)}}; } } },
async openRun(id, fromPop){ this.resetConfirms(); this.detail=null; this.view='run'; this.run={loading:true, data:null, err:''};
  if(!fromPop) history.pushState({run:id}, '', '/app/runs/'+encodeURIComponent(id));
  try{ this.run={loading:false, data:await this.api('/hub/runs/'+encodeURIComponent(id)), err:''}; }
  catch(e){ this.run={loading:false, data:null, err:this.hubErr(e, 'No such run for the team you are in ('+this.activeName+'). A run is readable by the team that called it and the team that made the tool: switch team and reload.')}; } },
// api() throws Error('http') with .status and .detail: say the detail, or a plain sentence per status
hubListing(t){ return ((t&&t.listing)||{}).state||'none'; },
hubListingWords(t){ return {none:'Not in search.', requested:'Requested: it appears in search once treg approves it.',
  approved:'Approved: it is in catalog search.', rejected:'Rejected: not in search.',
  unlisted:'Unlisted: out of search. Callers who have the id keep the approved version, and every change still waits for review.'}[this.hubListing(t)]; },
hubErr(e, on404){ if(e&&e.status===404&&on404) return on404; if(e&&e.status===401) return 'Sign in to see this.';
  const d=e&&e.detail; if(d&&typeof d==='object') return (d.error?d.error+': ':'')+(d.message||d.rule||JSON.stringify(d)); return d||String((e&&e.message)||e); },
fmtMs(ms){ return ms>=1000 ? (ms/1000).toFixed(1)+' s' : (ms||0)+' ms'; },
}
