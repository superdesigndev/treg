<script>
import { useDashboard } from '../state/context'

export default {
  props: { orgId: { type: Number, required: true }, slug: { type: String, required: true }, owner: Boolean },
  setup: useDashboard,
  data: () => ({ sso: null, blockedMembers: [], busy: '', error: '', message: '', itEmail: '', showInviteIT: false }),
  watch: { orgId() { this.loadSSO() } },
  mounted() { this.loadSSO() },
  computed: {
    subscribed() { return this.sso && ['active', 'trialing', 'past_due'].includes(this.sso.subscription_status) },
    directoryPlan() { return this.sso?.plan === 'sso_directory' },
    setupStepCount() { return this.directoryPlan ? 3 : 2 },
    completedSteps() {
      if (!this.sso) return 0
      return Number(this.sso.sso_ready) + Number(this.directoryPlan && this.sso.directory_ready) + Number(this.sso.enforced)
    },
  },
  methods: {
    formatUsdCents(cents) {
      return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100)
    },
    async copyEmployeeLink() {
      const url = new URL(`/auth/sso/${encodeURIComponent(this.slug)}/start`, location.origin).href
      this.message = await this.toClipboard(url) ? 'Employee sign-in link copied.' : 'Could not copy the link.'
    },
    async loadSSO() {
      this.sso = null
      this.blockedMembers = []
      try {
        this.sso = await this.api(`/orgs/${this.orgId}/sso`)
        if (this.owner) this.blockedMembers = (await this.api(`/orgs/${this.orgId}/sso/blocked`)).members
      }
      catch (e) { this.error = e.detail || 'Could not load SSO settings.' }
    },
    async action(kind, path, body) {
      this.busy = kind; this.error = ''; this.message = ''
      try {
        const result = await this.api(`/orgs/${this.orgId}/sso/${path}`, {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body || {}),
        })
        if (result.url) { location.href = result.url; return }
        this.message = kind === 'enforce' ? 'SSO access setting saved.' :
          kind === 'cancel' ? 'Subscription will end after the current billing period.' : 'Saved.'
        await this.loadSSO()
      } catch (e) { this.error = e.detail || `Could not ${kind}.` }
      finally { this.busy = '' }
    },
    async inviteIT() {
      const email = this.itEmail.trim()
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { this.error = "Enter your IT admin's email."; return }
      await this.action('invite IT admin', 'invite-it', { email })
      if (!this.error) this.message = 'WorkOS sent the IT admin a setup invitation.'
    },
    async restoreMember(userId) {
      await this.action('restore SSO access', `blocked/${userId}/restore`)
      if (!this.error) this.message = 'This employee can sign in with SSO again.'
    },
  },
}
</script>

<template>
  <section class="sso-page" aria-label="Single sign-on settings">
    <header class="sso-heading">
      <div>
        <div class="lbl">TEAM ACCESS</div>
        <h2>Company sign-in</h2>
        <p v-if="owner">Let employees join this team through your company's identity provider. They join as members; you can change their roles in Members.</p>
        <p v-else>Your team has company sign-in connected. Use the employee link for a future sign-in or share it with a coworker.</p>
      </div>
      <span v-if="owner && subscribed" class="sso-progress">{{completedSteps}} of {{setupStepCount}} steps done</span>
    </header>

    <p v-if="error" class="banner" role="alert">{{error}}</p>
    <p v-if="message" class="tut-notice" role="status">{{message}}</p>

    <template v-if="sso?.sso_ready && subscribed && !owner">
      <div class="sso-panel">
        <h3>Company sign-in is ready</h3>
        <p class="sso-muted">This link starts a company sign-in and can be shared with a coworker who has access through your identity provider.</p>
        <button class="btn sso-button" @click="copyEmployeeLink">Copy employee sign-in link</button>
      </div>
    </template>

    <template v-if="owner && sso">
      <div v-if="!sso.configured || !sso.billing_configured" class="sso-panel">
        <h3>Setup is unavailable</h3>
        <p class="sso-muted">WorkOS and Stripe must be configured on this deployment before you can set up company sign-in.</p>
      </div>

      <template v-else-if="!subscribed">
        <div class="sso-section-head"><div class="lbl">YOUR PLAN</div><h3>Choose how employees get access</h3></div>
        <div v-if="sso.enforced" class="sso-panel">
          <p class="sso-muted">This team still requires SSO. Allow regular sign-in before changing plans.</p>
          <button class="btn sm sso-button" :disabled="!!busy" @click="action('enforce','enforce',{enabled:false})">Allow regular team sign-in</button>
        </div>
        <p v-if="sso.cleanup_pending" class="sso-panel sso-muted">Your previous SSO setup is being removed. You can subscribe again when cleanup finishes.</p>
        <div v-if="sso.plans.length" class="sso-plan-grid">
          <article v-for="plan in sso.plans" :key="plan.id" class="sso-panel sso-plan">
            <div class="sso-plan-top"><span class="sso-plan-mark" aria-hidden="true">{{plan.directory_sync?'✦':'◇'}}</span><span v-if="plan.directory_sync" class="sso-pill">Automatic offboarding</span></div>
            <h4>{{plan.directory_sync?'SSO + Directory Sync':'SSO only'}}</h4>
            <p class="sso-muted">{{plan.directory_sync?'Employees sign in with SSO. Directory changes can also remove their team access and keys.':'Employees sign in with SSO. A team admin removes their access and keys manually.'}}</p>
            <div class="sso-price"><strong>{{formatUsdCents(plan.usd_monthly_cents)}}</strong><span>/ month, per team</span></div>
            <button class="btn sso-button" :class="plan.directory_sync?'primary':''" :disabled="!!busy || sso.cleanup_pending" @click="action('checkout','checkout',{plan:plan.id,price_id:plan.price_id})">
              Choose {{plan.directory_sync?'SSO + Directory Sync':'SSO only'}} <span aria-hidden="true">→</span>
            </button>
          </article>
        </div>
        <p v-else-if="!sso.cleanup_pending" class="sso-panel sso-muted">Subscription plans are temporarily unavailable. Please try again shortly.</p>
      </template>

      <template v-else>
        <div class="sso-panel sso-current-plan">
          <div class="sso-plan-summary">
            <div><div class="lbl">YOUR PLAN</div><h3>{{directoryPlan?'SSO + Directory Sync':'SSO only'}}</h3><p class="sso-muted">{{directoryPlan?'Company sign-in and automatic employee offboarding':'Company sign-in; remove employees from this team manually'}}</p></div>
            <span class="sso-pill" :class="sso.subscription_status==='past_due'?'sso-pill-warn':'sso-pill-ok'">{{sso.subscription_status==='past_due'?'Payment retrying':sso.cancel_at_period_end?'Ends this period':'Active'}}</span>
          </div>
          <p v-if="sso.subscription_status==='past_due'" class="sso-payment-note">Your payment is being retried. SSO remains available. Update your payment method in Billing.</p>
          <div class="sso-plan-foot">
            <span>{{sso.cancel_at_period_end?'Your setup stays available until the paid period ends.':'Billing is separate from your prepaid tool balance.'}}</span>
            <button v-if="!sso.cancel_at_period_end" class="sso-text-action" :disabled="!!busy" @click="action('cancel','cancel')">Cancel at end of billing period</button>
          </div>
        </div>

        <div class="sso-section-head"><div class="lbl">SETUP</div><h3>Get your team ready</h3><p>Complete these steps in order. The check marks update after treg verifies each step.</p></div>
        <div class="sso-steps">
          <article class="sso-panel sso-step" :class="{'sso-step-done':sso.sso_ready}">
            <div class="sso-step-main">
              <span class="sso-check" :class="{'done':sso.sso_ready}" aria-hidden="true">{{sso.sso_ready?'✓':'1'}}</span>
              <div class="sso-step-copy"><div class="sso-step-title"><h4>Connect and test company SSO</h4><span class="sso-step-state">{{sso.sso_ready?'Verified':'Next step'}}</span></div>
                <p>{{sso.sso_ready?'An employee has successfully signed in through your company provider.':'Connect your provider in WorkOS, then use the employee link for one test sign-in.'}}</p>
                <div class="sso-actions">
                  <button class="btn sso-button" :class="!sso.sso_ready?'primary':''" :disabled="!!busy" @click="action('open setup','setup/sso')">
                    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M6.5 2.5h-4v11h11v-4M8 8l5.5-5.5M9.5 2.5h4v4" stroke-linecap="round" stroke-linejoin="round"/></svg>
                    {{sso.sso_ready?'Manage SSO connection':'Set up company SSO'}}
                  </button>
                  <button class="btn sso-button" :disabled="!!busy" @click="copyEmployeeLink">
                    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="5" y="5" width="8" height="8" rx="1.5"/><path d="M3 10H2.5A1.5 1.5 0 0 1 1 8.5v-6A1.5 1.5 0 0 1 2.5 1h6A1.5 1.5 0 0 1 10 2.5V3" stroke-linecap="round"/></svg>
                    Copy employee sign-in link
                  </button>
                </div>
                <button class="sso-text-action sso-invite-toggle" :aria-expanded="showInviteIT" @click="showInviteIT=!showInviteIT">{{showInviteIT?'Hide IT invitation':'Someone else manages your provider? Invite your IT admin'}} <span aria-hidden="true">{{showInviteIT?'↑':'↓'}}</span></button>
                <div v-if="showInviteIT" class="sso-invite"><p>WorkOS will email setup instructions to your IT admin.</p><div class="field sso-invite-form"><input v-model="itEmail" type="email" autocomplete="email" aria-label="IT admin email" placeholder="it@company.com" @keyup.enter="inviteIT"/><button class="btn sso-button" :disabled="!!busy" @click="inviteIT">Send invitation <span aria-hidden="true">→</span></button></div></div>
              </div>
            </div>
          </article>

          <article v-if="directoryPlan" class="sso-panel sso-step" :class="{'sso-step-done':sso.directory_ready}">
            <div class="sso-step-main"><span class="sso-check" :class="{'done':sso.directory_ready}" aria-hidden="true">{{sso.directory_ready?'✓':'2'}}</span><div class="sso-step-copy">
              <div class="sso-step-title"><h4>Connect Directory Sync</h4><span class="sso-step-state">{{sso.directory_ready?'Connected':'Not connected'}}</span></div>
              <p>{{sso.directory_ready?'Employee changes can now update access in treg.':'Connect your employee directory so removals can revoke team access and keys.'}}</p>
              <button class="btn sso-button" :class="sso.sso_ready && !sso.directory_ready?'primary':''" :disabled="!!busy" @click="action('open setup','setup/dsync')"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M6.5 2.5h-4v11h11v-4M8 8l5.5-5.5M9.5 2.5h4v4" stroke-linecap="round" stroke-linejoin="round"/></svg>{{sso.directory_ready?'Manage directory':'Set up Directory Sync'}}</button>
            </div></div>
          </article>

          <article class="sso-panel sso-step" :class="{'sso-step-done':sso.enforced}">
            <div class="sso-step-main"><span class="sso-check" :class="{'done':sso.enforced}" aria-hidden="true">{{sso.enforced?'✓':setupStepCount}}</span><div class="sso-step-copy">
              <div class="sso-step-title"><h4>Require SSO for this team</h4><span class="sso-step-state">{{sso.enforced?'On':'Off'}}</span></div>
              <p>{{sso.enforced?'Members must use company SSO to access this team. The owner keeps normal sign-in for recovery.':'After a successful test sign-in, require company SSO for members. The owner keeps normal sign-in for recovery.'}}</p>
              <p v-if="directoryPlan && !sso.directory_ready && sso.sso_ready" class="sso-step-hint">Finish Directory Sync first if you want automatic employee offboarding.</p>
              <button class="btn sso-button" :class="sso.sso_ready && (!directoryPlan || sso.directory_ready) && !sso.enforced?'primary':''" :disabled="!!busy || (!sso.sso_ready && !sso.enforced)" @click="action('enforce','enforce',{enabled:!sso.enforced})"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M8 1.5 13 3v4.3c0 3.1-2 5.3-5 7.2-3-1.9-5-4.1-5-7.2V3l5-1.5Z" stroke-linejoin="round"/><path d="m5.8 7.9 1.5 1.5 3-3" stroke-linecap="round" stroke-linejoin="round"/></svg>{{sso.enforced?'Allow regular team sign-in':'Require SSO'}}</button>
              <span v-if="!sso.sso_ready" class="sso-step-hint">Complete the test sign-in first.</span>
            </div></div>
          </article>
        </div>

        <div v-if="blockedMembers.length" class="sso-panel sso-blocked"><h3>Employees removed by a team admin</h3><p class="sso-muted">They cannot rejoin through SSO until you allow access again.</p><div v-for="member in blockedMembers" :key="member.user_id" class="sso-blocked-row"><span>{{member.email}}</span><button class="btn sm" :disabled="!!busy" @click="restoreMember(member.user_id)">Allow SSO again</button></div></div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.sso-page{width:100%;margin:8px 0 40px;display:grid;gap:18px}
.sso-heading{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;padding:4px 2px 2px}
.sso-heading h2{font-size:28px;letter-spacing:-.04em;line-height:1.1;margin:7px 0 8px}
.sso-heading p,.sso-section-head p{max-width:650px;margin:0;color:var(--muted);line-height:1.5;font-size:13px}
.sso-progress{flex:none;font-size:12px;font-weight:650;color:var(--muted);padding-bottom:3px}
.sso-panel{background:var(--surface,var(--panel));border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:var(--shadow-sm)}
.sso-panel h3,.sso-section-head h3{font-size:19px;letter-spacing:-.025em;margin:5px 0 6px}
.sso-muted,.sso-step-copy p{color:var(--muted);font-size:13px;line-height:1.5;margin:0}
.sso-plan-summary{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}
.sso-pill{display:inline-flex;align-items:center;white-space:nowrap;border:1px solid var(--line);border-radius:999px;padding:5px 10px;font-size:11px;font-weight:650;background:var(--panel2)}
.sso-pill-ok{color:var(--green);border-color:color-mix(in srgb,var(--green) 28%,var(--line));background:color-mix(in srgb,var(--green) 7%,var(--surface,var(--panel)))}
.sso-pill-warn{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 25%,var(--line))}
.sso-payment-note{background:color-mix(in srgb,var(--accent) 8%,var(--surface,var(--panel)));border:1px solid color-mix(in srgb,var(--accent) 20%,var(--line));border-radius:10px;padding:11px 13px;font-size:12px;line-height:1.5;margin:18px 0 0}
.sso-plan-foot{border-top:1px solid var(--line);padding-top:14px;margin-top:20px;display:flex;justify-content:space-between;align-items:center;gap:12px;color:var(--muted);font-size:12px;line-height:1.5}
.sso-text-action{font:inherit;color:var(--ink);background:none;border:0;padding:3px 0;text-decoration:underline;text-underline-offset:3px;cursor:pointer;text-align:left}
.sso-text-action:hover{color:var(--accent)}.sso-text-action:disabled{opacity:.5;cursor:not-allowed}
.sso-section-head{padding:10px 2px 0}.sso-section-head h3{margin:7px 0 6px}
.sso-steps{display:grid;gap:12px}.sso-step{padding:20px 22px}.sso-step-done{border-color:color-mix(in srgb,var(--green) 28%,var(--line))}
.sso-step-main{display:flex;align-items:flex-start;gap:16px}.sso-check{width:28px;height:28px;flex:none;display:grid;place-items:center;border:1.5px solid var(--line);border-radius:7px;color:var(--muted);font-size:12px;font-weight:700;background:var(--panel2)}
.sso-check.done{border-color:var(--green);background:var(--green);color:var(--onAccent,#fff)}
.sso-step-copy{min-width:0;flex:1}.sso-step-title{display:flex;justify-content:space-between;align-items:baseline;gap:12px}.sso-step-title h4{font-size:15px;letter-spacing:-.01em;margin:2px 0 5px}.sso-step-state{font-size:11px;font-weight:650;color:var(--muted);white-space:nowrap}.sso-step-done .sso-step-state{color:var(--green)}
.sso-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.sso-button{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:36px}.sso-button svg{width:15px;height:15px;flex:none}
.sso-step-copy>.sso-button{margin-top:14px}.sso-step-hint{display:inline-block;margin:10px 0 0 10px;color:var(--muted);font-size:11px}.sso-step-copy p.sso-step-hint{display:block;margin:9px 0 0;font-size:11px}
.sso-invite-toggle{margin-top:16px;font-size:12px}.sso-invite{margin-top:9px;padding:14px;background:var(--panel2);border:1px solid var(--line);border-radius:10px}.sso-invite p{margin:0 0 9px}.sso-invite-form{display:flex;gap:8px}.sso-invite-form input{min-width:0;flex:1}
.sso-plan-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}.sso-plan{display:flex;flex-direction:column;gap:12px}.sso-plan-top{display:flex;justify-content:space-between;align-items:center}.sso-plan-mark{font-size:24px;line-height:1}.sso-plan h4{font-size:18px;margin:0}.sso-plan .sso-muted{min-height:58px}.sso-price{margin-top:auto;display:flex;align-items:baseline;gap:5px}.sso-price strong{font-size:26px;letter-spacing:-.04em}.sso-price span{font-size:12px;color:var(--muted)}.sso-plan>.sso-button{margin-top:4px}
.sso-blocked h3{margin-top:0}.sso-blocked-row{display:flex;align-items:center;justify-content:space-between;gap:12px;border-top:1px solid var(--line);padding-top:12px;margin-top:12px;font-size:13px}
@media(max-width:620px){.sso-heading,.sso-plan-summary,.sso-plan-foot{align-items:flex-start;flex-direction:column}.sso-panel{padding:17px}.sso-step-main{gap:12px}.sso-step-title{align-items:flex-start;flex-direction:column;gap:0}.sso-actions,.sso-invite-form{flex-direction:column;align-items:stretch}.sso-actions .sso-button,.sso-invite-form .sso-button{width:100%}.sso-step-hint{display:block;margin:9px 0 0}}
</style>
