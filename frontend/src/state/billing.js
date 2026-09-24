
export default {
money(micro){ const m=Math.round(Number(micro)||0), s=m<0?'-':'', a=Math.abs(m);
      if(a%10000===0) return s+'$'+(a/1e6).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
      return s+'$'+(a/1e6).toFixed(6).replace(/0+$/,''); },
async loadBilling(){ if(!this.canAdmin || !this.activeOrgId){ this.billing=null; return; }
      this.autoOpen=false; this.autoConsent=false;
      this.billing=await this.api('/billing').catch(()=>null);
      if(this.billing){ this.topupAmount=this.billing.topup.default_usd;
        this.autoAmount=Math.round(this.billing.autotopup.amount_usd);
        this.autoThreshold=Math.round(this.billing.autotopup.threshold_usd);
        this.loadBillingHistory(); }
      // The daily cap lives beside the balance for the user even though it is an org setting.
      this.capCfg=await this.api(`/orgs/${this.activeOrgId}/settings`).catch(()=>null);
      if(this.capCfg){ this.capUsd=this.capCfg.daily_cap_micro ? (this.capCfg.daily_cap_micro/1e6).toFixed(2) : '';
        const k=await this.api(`/orgs/${this.activeOrgId}/tag-keys`).catch(()=>null);
        this.budDims=[...new Set([...((k&&k.seen)||[]), ...(this.capCfg.budget_dims||[])])];
        if(!this.budDims.length) this.budDims=['customer'];
        this.budDim=this.budDim||this.budDims[0];
        this.budgets=await this.api(`/orgs/${this.activeOrgId}/budgets`).catch(()=>[]); } },
defaultFor(dim){ return (this.budgets||[]).find(b=>b.dim===dim && b.is_default) || null; },
overridesFor(dim){ return (this.budgets||[]).filter(b=>b.dim===dim && !b.is_default); },
fmtCap(b){ return b.daily_cap_micro==null ? 'no limit' : this.money(b.daily_cap_micro)+'/day'; },
// The server's own words when it has any. Every budget refusal is one a builder must be able to
    // act on — "above_platform_ceiling" carries the ceiling, "too_many_budget_dimensions" names the
    // dimensions in use — so the generic fallback is the last resort, never the first.
    _errMsg(e, fallback){ return (e&&e.detail&&e.detail.message) || (e&&typeof e.detail==='string'&&e.detail)
      || (e&&e.message) || fallback; },
// Blank `val` addresses the DIMENSION'S DEFAULT — `PUT /budgets/{dim}`, the route with no value.
    _budgetUrl(dim, val){ const base=`/orgs/${this.activeOrgId}/budgets/${encodeURIComponent(dim)}`;
      return val ? `${base}/${encodeURIComponent(val)}` : base; },
async _putBudget(dim, val, body){
      return this.api(this._budgetUrl(dim, val),
        {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    },
async _reloadBudgets(){ this.budgets=await this.api(`/orgs/${this.activeOrgId}/budgets`).catch(()=>this.budgets); },
async saveBudget(){ this.budBusy=true; this.budErr='';
      try{
        const body={};
        if(this.budDaily!=='') body.daily_cap_micro=Math.round(parseFloat(this.budDaily)*1e6);
        await this._putBudget(this.budDim, this.budVal.trim(), body);
        this.budVal=''; this.budDaily='';
        await this.loadBilling();   // re-reads settings: a first limit DECLARES the dimension
        await this._reloadBudgets();
      }catch(e){ this.budErr=this._errMsg(e, 'could not save that limit'); }
      finally{ this.budBusy=false; } },
async toggleBlock(b){ this.budErr='';
      // A PARTIAL put: only `status` travels, so caps set earlier survive being blocked/unblocked.
      try{ await this._putBudget(b.dim, b.val, {status: b.status==='blocked'?'active':'blocked'});
        await this._reloadBudgets();
      }catch(e){ this.budErr=this._errMsg(e, 'could not change that'); } },
async removeBudget(b){ this.budErr='';
      try{ await this.api(this._budgetUrl(b.dim, b.val), {method:'DELETE'}); await this._reloadBudgets();
      }catch(e){ this.budErr=this._errMsg(e, 'could not remove that'); } },
async saveCap(){ this.capBusy=true; this.capErr='';
      try{
        const micro=Math.round(parseFloat(this.capUsd||'0')*1e6);
        this.capCfg=await this.api(`/orgs/${this.activeOrgId}/settings`,
          {method:'PATCH', headers:{'content-type':'application/json'},
           body:JSON.stringify({daily_cap_micro:micro})});
        this.capUsd=this.capCfg.daily_cap_micro ? (this.capCfg.daily_cap_micro/1e6).toFixed(2) : '';
      }catch(e){
        this.capErr=this._errMsg(e, 'could not save that limit');
      }finally{ this.capBusy=false; } },
// Amounts come from our own credit blocks, so this list can never disagree with the balance above
    // it; Stripe supplies only the document links, and bhist.ok===false means those were unavailable.
    async loadBillingHistory(){ if(!this.canAdmin || !this.billing || !this.billing.configured){ this.bhist={items:[],loading:false,ok:true}; return; }
      this.bhist={items:[],loading:true,ok:true};
      const out=await this.api('/billing/history').catch(()=>null);
      this.bhist={items:(out&&out.items)||[], loading:false, ok:!out||out.stripe_ok!==false}; },
async openPortal(){ this.billingBusy=true; this.err='';
      try{ const out=await this.api('/billing/portal',{method:'POST'});
        // Stripe's hosted portal owns card, billing address, tax ID and the full invoice archive. Its
        // return_url comes back to #billing, so the same tab is right here as it is for Checkout.
        window.location.href=out.url; }
      catch(e){ this.err='Could not open the billing portal: '+(e.detail||e.status); this.billingBusy=false; } },
bhistLink(i){ return i.invoice_pdf || i.hosted_invoice_url || i.receipt_url || ''; },
bhistLabel(i){ return (i.invoice_pdf||i.hosted_invoice_url) ? 'Invoice' : (i.receipt_url ? 'Receipt' : ''); },
// created_at is naive UTC (the models._now convention), so it needs the Z that isoformat omits —
    // without it the browser reads it as local time and a late-evening top-up shows the wrong day.
    bhistDate(iso){ if(!iso) return '';
      const d=new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : iso+'Z');
      return isNaN(d)?'':d.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'}); },
async addFunds(amount){ this.billingBusy=true; this.topupAmount=amount; this.err='';
      // Named differently from the server's topup_started (same click, two vantage points —
      // a shared name would double-count in trends); this one exists to link session replays.
      this.track('topup_checkout_opened',{amount_usd:amount});
      try{ const out=await this.api('/billing/topup',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount_usd:amount,checkout_source:window.TregTracking?.checkoutSource()||'app'})});
        // Stripe's own hosted page does the payment; the balance moves when its webhook lands, never
        // on the return redirect (which a payer could simply type). Same tab, so the return lands here.
        window.location.href=out.url; }
      catch(e){ this.err='Could not start the payment: '+(e.detail||e.status); this.billingBusy=false; } },
// ---- top-up modal ----
    openTopup(){ this.topupPick=this.billing.topup.default_usd; this.topupOther=null; this.topupErr='';
      // Default ON only for a team with no mandate; a team that already decided is not re-asked.
      this.topupAuto=!(this.billing.autotopup.enabled||this.billing.autotopup.consented_at);
      this.topupOpen=true; },
// "Other" starts one rung above the biggest card, not blank: the card row ends at $200, so the
    // amount a payer reaches for here is larger than that, and a prefilled number is one keystroke
    // away from theirs. Only on first pick - a value they typed survives re-selecting the card.
    pickOther(){ if(this.topupPick!=='other'&&!this.topupOther){ const top=Math.max(...this.billing.topup.presets); this.topupOther=Math.min(this.billing.topup.max_usd, top>=200?500:top*2); } this.topupPick='other'; },
tierBonus(usd){ const t=this.billing&&this.billing.topup.bonus_tiers; if(!t||!usd) return 0;
      // Keys arrive as strings from JSON; the highest floor at or below the amount applies.
      let pct=0; Object.keys(t).map(Number).sort((a,b)=>a-b).forEach(k=>{ if(usd>=k) pct=t[k]; });
      return Math.floor(usd*1e6*pct/100); },
async payTopup(){ const usd=this.topupUsd; if(!this.topupValid) return;
      this.billingBusy=true; this.topupErr=''; this.topupAmount=usd;
      this.track('topup_checkout_opened',{amount_usd:usd, auto_opt_in:this.topupAuto, bonus_micro:this.topupBonusMicro});
      try{
        const fresh=!(this.billing.autotopup.enabled||this.billing.autotopup.consented_at);
        if(fresh&&this.topupAuto){
          // Consent first, Checkout second: the mandate has to exist before the card that will be
          // charged under it. The server stores the numbers and marks it "no_card"; Checkout's
          // saved card then arms it from the setup webhook. A failure here stops the payment too -
          // paying without the auto top-up the user just agreed to would be a silent downgrade.
          this.billing=await this.api('/billing/autotopup',{method:'POST',headers:{'content-type':'application/json'},
            body:JSON.stringify({enabled:true, consent:true, amount_usd:this.autoAmount, threshold_usd:this.autoThreshold, monthly_cap_usd:this.autoCapUsd, setup_url:false})});
        }
        const out=await this.api('/billing/topup',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount_usd:usd,checkout_source:window.TregTracking?.checkoutSource()||'app'})});
        window.location.href=out.url; }
      catch(e){ this.topupErr='Could not start the payment: '+(e.detail||e.status); this.billingBusy=false; } },
// The toggle itself never arms auto top-up - it can only DISARM (off is safe) or open the
    // settings panel; arming still goes through the consent checkbox + confirm button below.
    autoToggled(){ if(this.billing.autotopup.enabled){ this.autoOpen=false; this.setAuto(false); return; }
      this.autoOpen=!this.autoOpen; if(!this.autoOpen) this.autoConsent=false; },
async setAuto(on){ this.billingBusy=true; this.err='';
      try{ const out=await this.api('/billing/autotopup',{method:'POST',headers:{'content-type':'application/json'},
             body:JSON.stringify({enabled:on, consent:on, amount_usd:on?this.autoAmount:null, threshold_usd:on?this.autoThreshold:null})});
        // No card yet: consent is stored, and Stripe's hosted card page finishes the job. Auto top-up
        // arms itself from the setup_intent.succeeded webhook, so there's nothing more to click here.
        if(out.setup_url){ window.location.href=out.setup_url; return; }
        this.billing=out; this.autoConsent=false; this.autoOpen=false; }
      catch(e){ this.err='Auto top-up change failed: '+(e.detail||e.status); }
      this.billingBusy=false; }
}
