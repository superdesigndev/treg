export default {
topupUsd(){ if(!this.billing) return 0; if(this.topupPick==='other'){ const v=Number(this.topupOther); return Number.isInteger(v)&&v>0?v:0; } return this.topupPick; },
topupValid(){ if(!this.billing||!this.topupUsd) return false; const t=this.billing.topup; return this.topupUsd>=t.min_usd&&this.topupUsd<=t.max_usd; },
topupBonusMicro(){ return this.tierBonus(this.topupUsd)+this.refPresetBonus(this.topupUsd); },
maxBonusPct(){ const t=this.billing&&this.billing.topup.bonus_tiers; return t?Math.max(0,...Object.values(t)):0; },
// The monthly cap is a server-side runaway guardrail, not something the payer is asked to pick:
    // the modal sets it to the single-top-up ceiling (effectively unlimited) so a big payer is never
    // locked out mid-month by a default sized for $10 refills. The manage panel below can lower it.
    autoCapUsd(){ return this.billing?this.billing.topup.max_usd:0; },
// Marketplace shelves. /oauth/providers already returns providers grouped-then-alphabetical, so
    // this walks the list once and starts a shelf whenever the category changes — re-sorting here
    // would just be a second place to keep the order in step with the registry.
    providerGroups(){
      const hints={
        'SEO/AEO':'search visibility and site analytics — treg holds the approved app',
        'Advertising':'read spend and performance, or manage campaigns',
        'Social media':'publish and read back as the connected account',
        'Community':'bring your own workspace bot',
      };
      const out=[];
      for(const p of this.providers){
        const cat=p.category||'Other';
        if(!out.length || out[out.length-1].category!==cat) out.push({category:cat, hint:hints[cat]||'', items:[]});
        out[out.length-1].items.push(p);
      }
      return out;
    },
// Filter chips narrow the shelves rather than flattening them — with one category picked the
    // single remaining shelf header still says which, so the page never loses its place.
    shownGroups(){ const gs=this.mkCat ? this.providerGroups.filter(g=>g.category===this.mkCat) : this.providerGroups;
      const q=this.q.trim().toLowerCase();
      if(!q) return gs;
      const hit=p=>((p.name||'')+' '+(p.service||'')+' '+(p.summary||'')).toLowerCase().includes(q);
      return gs.map(g=>({...g, items:g.items.filter(hit)})).filter(g=>g.items.length); },
connCount(){ const m={}; for(const c of this.connections){ if(c.provider) m[c.provider]=(m[c.provider]||0)+1; } return m; }
}
