<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div  class="scrim" role="dialog" aria-modal="true" @click.self="topupOpen=false">
      <div class="modal" style="padding:18px 20px;width:min(620px,94vw)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <h3 style="margin:0">Top up credits</h3>
          <button class="btn sm" @click="topupOpen=false" aria-label="Close">✕</button>
        </div>
        <p class="sub" style="margin:6px 0 14px">Choose an amount. Bigger top-ups earn bonus credit; you can always add more later.</p>
        <div class="fundgrid" style="max-width:none;grid-template-columns:repeat(5,minmax(80px,1fr))">
          <button v-for="p in billing.topup.presets" :key="p" class="fundcard" :class="{sel:topupPick===p}" :disabled="billingBusy" @click="topupPick=p">
            <b>${{p}}</b>
            <!-- The bonus is named ON the qualifying buttons: the amount is chosen here, and a
                 preset that quietly forfeits a bonus is the whole failure this is meant to prevent.
                 The referral bonus (refPresetBonus) stacks on the tier bonus. -->
            <span v-if="tierBonus(p)" style="color:var(--green)">+{{money(tierBonus(p))}} bonus</span>
            <span v-else-if="refPresetBonus(p)" style="color:var(--green)">+{{money(refPresetBonus(p))}} bonus</span>
          </button>
          <button class="fundcard" :class="{sel:topupPick==='other'}" :disabled="billingBusy" @click="pickOther()">
            <b>Other</b>
          </button>
        </div>
        <div v-if="topupPick==='other'" style="margin-top:12px">
          <div style="display:flex;align-items:center;border:1px solid var(--line);border-radius:var(--rb);background:var(--bg);padding:0 14px">
            <span style="font-family:var(--mono);font-size:22px;color:var(--muted)">$</span>
            <input type="number" :min="billing.topup.min_usd" :max="billing.topup.max_usd" step="1" v-model.number="topupOther" placeholder="0" autocomplete="off" data-lpignore="true"
                   style="flex:1;border:0;background:transparent;color:var(--ink);font-family:var(--mono);font-size:22px;font-weight:700;padding:12px 8px;outline:none;min-width:0"/>
          </div>
          <p class="sub" style="margin:6px 0 0">Whole dollars, ${{billing.topup.min_usd}}–${{billing.topup.max_usd.toLocaleString()}}.</p>
        </div>

        <!-- Auto top-up. Read-only line when the team already has a mandate; otherwise the toggle. -->
        <div style="margin-top:14px;border:1px solid var(--line);border-radius:var(--rb);padding:10px 12px;background:var(--panel2)">
          <template v-if="billing.autotopup.enabled||billing.autotopup.consented_at">
            <b style="font-size:12.5px">Auto top-up is {{billing.autotopup.enabled?'on':'set up'}}.</b>
            <span class="sub" style="margin:0"> Manage or turn it off from the billing page.</span>
          </template>
          <template v-else>
            <label style="display:flex;gap:10px;align-items:flex-start;cursor:pointer">
              <span class="tswitch" style="margin-top:2px"><input type="checkbox" v-model="topupAuto"/><span class="knob"></span></span>
              <span>
                <b style="font-size:12.5px">Auto top-up {{topupAuto?'on':'off'}}</b>
                <!-- The mandate. Not decoration: an off-session charge with no recorded agreement to
                     THESE numbers is an unauthorized charge under PSD2/SCA. The numbers are the
                     server defaults ($20 when below $5), shown here in full; the billing page's
                     Edit panel changes them. Not the top-up amount: a $200 buyer does not want
                     $200 refills. -->
                <span class="sub" style="display:block;margin:2px 0 0">
                  <template v-if="topupAuto">I authorize treg to charge my saved card <b>${{autoAmount}}</b> automatically whenever my balance drops below <b>${{autoThreshold}}</b>. Cancel any time from the billing page.</template>
                  <template v-else>Calls fail with a 402 once the balance runs out. Turn this on to keep agents running without watching it.</template>
                </span>
              </span>
            </label>
          </template>
        </div>

        <div style="margin-top:14px;border:1px solid var(--line);border-radius:var(--rb);padding:10px 12px;font-family:var(--mono);font-size:12.5px">
          <div style="display:flex;justify-content:space-between"><span>Credit added</span><span v-if="!topupUsd">—</span><span v-else><b>{{money(topupUsd*1e6+topupBonusMicro)}}</b><span v-if="topupBonusMicro" class="sub" style="margin:0"> ({{money(topupUsd*1e6)}} + <span style="color:var(--green)">{{money(topupBonusMicro)}} bonus</span>)</span></span></div>
          <div style="display:flex;justify-content:space-between;border-top:1px solid var(--line);margin-top:8px;padding-top:8px;font-weight:700"><span>Total due</span><span>{{topupUsd?money(topupUsd*1e6):'—'}}</span></div>
        </div>
        <p v-if="topupErr" class="sub" style="color:var(--red);margin:10px 0 0">{{topupErr}}</p>
        <p class="sub" style="margin:10px 0 0">You'll pay on Stripe's secure page. The balance updates the moment the payment lands.</p>
        <div style="margin-top:14px;display:flex;justify-content:flex-end;gap:8px">
          <button class="btn" @click="topupOpen=false" :disabled="billingBusy">Cancel</button>
          <button class="btn primary" :disabled="billingBusy||!topupValid" @click="payTopup()">{{billingBusy?'Opening Stripe…':(topupUsd?'Pay $'+topupUsd+' now':'Pay now')}}</button>
        </div>
      </div>
    </div>
</template>
