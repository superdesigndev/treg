
export default {
// ---- referrals ----
    // GET /referrals also runs the payout sweep server-side, so simply opening this page is what
    // makes a reward whose hold has elapsed actually land. That is deliberate: treg has no
    // scheduler, so the work rides on a request someone is already making.
    async loadReferrals(){ if(!this.authed) return;
      this.ref={...this.ref, loading:true};
      // One call: GET mints the code if this is the first visit (asking for the page IS the lazy
      // trigger), so there is no POST-then-GET round trip and no window where `link` is empty.
      const out=await this.api('/referrals').catch(()=>null);
      if(out) this.ref={...out, loading:false}; else this.ref={...this.ref, loading:false}; },
// How much extra THIS preset earns a referred team, or 0. Guarded on the offer existing, so a
    // team that arrived on its own sees the buttons exactly as before.
    refPresetBonus(usd){ const o=this.billing&&this.billing.referral_offer;
      // NULL-GUARD FIRST. `referral_offer` is null for every team that was not referred — i.e. most
      // of them — and reading through it throws inside a render, which in Vue blanks the ENTIRE
      // dashboard rather than just this row. Cost one blank page during review; keep it on its own
      // line rather than folded into the expression below, where deleting a clause drops it silently.
      if(!o) return 0;
      // Against what is REMAINING, not the full minimum: a team that already added $5 unlocks the
      // bonus with another $5, and marking that button "one-time" would be simply wrong.
      return (usd*1000000 >= o.remaining_micro) ? o.referred_micro : 0; },
// The top-bar entry names the offer: legacy's "get $5" line out-drew a bare "Refer a friend" by a
    // wide margin. Amounts come from /meta (config only), never from GET /referrals, which has side
    // effects. Falls back to the plain label while /meta loads or when either side earns nothing.
    refEntryLabel(){ const r=this.meta&&this.meta.referral;
      if(!r || !(r.referrer_micro>0) || !(r.referred_micro>0)) return 'Refer a friend';
      const usd=m=>m%1000000===0 ? '$'+m/1000000 : this.money(m);
      return 'Give '+usd(r.referred_micro)+', get '+usd(r.referrer_micro); },
async copyRefLink(){ try{ await navigator.clipboard.writeText(this.ref.link); }catch(e){}
      this.refCopied=true; this.track('referral_link_copied');
      setTimeout(()=>{ this.refCopied=false; }, 1600); },
// The row's own status, in the words a referrer thinks in. `capped`/`rejected` say WHY rather
    // than showing a blank — "I referred someone and got nothing" is the support ticket this
    // program generates, and the answer belongs on the page, not in an email to us.
    refStatus(r){
      if(r.status==='paid') return 'Paid';
      if(r.status==='qualified') return 'Added funds — pays '+this.bhistDate(r.pays_at);
      if(r.status==='pending') return 'Signed up';
      if(r.status==='capped') return 'Over your limit';
      return 'Not eligible'; }
}
