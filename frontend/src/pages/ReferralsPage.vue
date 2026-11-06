<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>

          <h1>{{refTab==='partner' ? 'Affiliate partner' : 'Refer a friend'}}</h1>
          <p class="sub" v-if="refTab==='partner'">Referral credit without a lifetime referral limit. Approval required.</p>
          <p class="sub" v-else :style="ref.loaded ? null : {visibility:'hidden'}">They get {{money(ref.terms.referred_micro)}} when they add
            {{money(ref.terms.min_topup_micro)}}. You get {{money(ref.terms.referrer_micro)}},
            {{ref.terms.hold_days}} days later.</p>
          <div class="tabs" style="max-width:720px">
            <button :class="{active:refTab==='friend'}" @click="refTab='friend'">Refer a friend</button>
            <button :class="{active:refTab==='partner'}" @click="refTab='partner'">Affiliate partner</button>
          </div>
          <div v-if="refTab==='partner'" style="max-width:720px">
            <div class="card">
              <div class="lbl">Who it's for</div>
              <p class="sub" style="margin:6px 0 0">People who send real volume: an audience, a newsletter,
                a community, or a team that builds on treg for clients.</p>
              <div class="lbl" style="margin-top:16px">What you earn</div>
              <p class="sub" style="margin:6px 0 0">The same referral rewards in credit, with no lifetime
                referral limit while your affiliate access is active. The usual payment requirements
                and reward waiting period still apply.</p>
              <template v-if="ref.cap.affiliate">
                <div class="lbl" style="margin-top:16px">Affiliate access active</div>
                <p class="sub" style="margin:6px 0 0">Keep sharing your referral link. Previously capped
                  referrals stay capped.</p>
              </template>
              <template v-else>
                <div class="lbl" style="margin-top:16px">How to get in</div>
                <p class="sub" style="margin:6px 0 14px">Tell us about your audience and how you'd send
                  people to treg. We'll review your application.</p>
                <a class="btn primary sm" href="https://forms.gle/5N3DyVPrNJkrGuxF6" target="_blank"
                   rel="noopener" @click="track('affiliate_apply_clicked')" style="text-decoration:none">Apply to be an affiliate partner</a>
              </template>
            </div>
            <p class="sub" style="margin:12px 0 0">Just want to tell a friend? The
              <a href="#" @click.prevent="refTab='friend'">refer-a-friend</a> program pays
              {{money(ref.terms.referrer_micro)}} in credit per friend, no application needed.</p>
          </div>

          <template v-if="refTab==='friend'">

          <p v-if="ref.loading" class="sub">Loading…</p>
          <!-- ONE column for the whole view. The children carry no max-width of their own: three
               different ones is what made the card, the tiles and the table start and end at three
               different x-positions. -->
          <div v-else style="max-width:720px">
            <!-- Everyone with a team gets a link — including free-tier users, who on a product
                 pitched as "$1.00 free, no card" are most users and the likeliest to tell a friend.
                 This branch is now only the degenerate case (no team to pay the reward into), kept
                 because the reward has to land SOMEWHERE and a silent zero is the worst version. -->
            <div v-if="!ref.eligible" class="card" style="margin-top:14px">
              <div class="lbl">Create a team to unlock your link</div>
              <p class="sub" style="margin:6px 0 12px">Referral rewards are paid as credit into a
                team you own, so you'll need one first.</p>
              <button class="btn primary sm" @click="go('orgs')">Go to teams</button>
            </div>

            <template v-else>
              <div class="card" style="margin-top:14px">
                <div class="lbl">Your link</div>
                <div style="display:flex;gap:8px;margin-top:8px">
                  <input readonly :value="ref.link" @focus="$event.target.select()"
                         style="flex:1;min-width:0;font-family:var(--mono);font-size:12px">
                  <button class="btn sm" @click="copyRefLink" style="flex:none">{{refCopied?'Copied':'Copy'}}</button>
                </div>
                <p class="sub" style="margin:10px 0 0" v-if="ref.credit_org">
                  Your reward lands in <b>{{ref.credit_org.name}}</b>.</p>
              </div>

              <!-- `.stat` expects `.n` (the figure) then `.l` (the label), in that order — any other
                   class names render as unstyled text, which is exactly what a hand-rolled k/v pair
                   did here. -->
              <!-- auto-FIT, not the sheet's auto-fill: at this width auto-fill lays out four 150px
                   tracks and leaves the fourth empty, so three tiles stop short of the column's
                   right edge while the card above and the table below reach it. auto-fit collapses
                   the empty track and the three stretch to fill. -->
              <div class="statgrid" style="margin-top:14px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
                <div class="stat"><div class="n">{{ref.totals.signed_up}}</div><div class="l">signed up</div></div>
                <div class="stat"><div class="n">{{ref.totals.topped_up}}</div><div class="l">topped up</div></div>
                <div class="stat"><div class="n">{{money(ref.totals.earned_micro)}}</div><div class="l">earned</div></div>
              </div>
              <p v-if="ref.totals.pending_micro" class="sub" style="margin:-8px 0 0">
                {{money(ref.totals.pending_micro)}} on the way — rewards land
                {{ref.terms.hold_days}} days after your friend adds funds.</p>

              <div v-if="ref.cap.affiliate" class="card" style="margin-top:14px">
                <div class="lbl">Affiliate access active</div>
                <p class="sub" style="margin:6px 0 0">Your qualifying referrals earn credit with no lifetime
                  referral limit. Previously capped referrals stay capped.</p>
              </div>
              <div v-else-if="ref.cap.limit != null && ref.cap.paid >= ref.cap.limit" class="card"
                   style="margin-top:14px;border-color:var(--accent)">
                <div class="lbl">You've hit the {{ref.cap.limit}}-referral limit</div>
                <p class="sub" style="margin:6px 0 0">Apply through the Affiliate partner tab above
                  to have the limit removed for future qualifying referrals.</p>
              </div>

              <div class="grp" style="margin:20px 0 8px">Your referrals</div>
              <p v-if="!ref.referrals.length" class="sub" style="margin:0">
                Nobody yet. Share your link above.</p>
              <!-- House-style table: bare `<table>` is ALREADY a card (background, border, radius)
                   and `th,td` already carry 10px/13px padding. Overriding that padding to `8px 0`
                   is what pushed the amount flush against the card's own right edge. -->
              <table v-else>
                <tr><th>Who</th><th>Signed up</th><th>Status</th><th style="text-align:right">Reward</th></tr>
                <tr v-for="(r,i) in ref.referrals" :key="i">
                  <td>{{r.email || '—'}}</td>
                  <td class="sub" style="white-space:nowrap">{{bhistDate(r.signed_up_at)}}</td>
                  <td>{{refStatus(r)}}</td>
                  <td style="text-align:right;font-family:var(--mono);white-space:nowrap">
                    <span v-if="r.reward_micro">{{money(r.reward_micro)}}</span>
                    <span v-else class="muted">—</span>
                  </td>
                </tr>
              </table>
            </template>
          </div>
          </template>

</template>
