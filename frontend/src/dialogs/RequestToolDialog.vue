<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="reqAsk=false;reqErr=''">
      <div class="modal" style="width:min(520px,95vw)"><div class="hd"><b>Request a tool</b><button class="btn sm ico" @click="reqAsk=false;reqErr=''" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px">
          <template v-if="!reqDone">
            <p class="explain" style="margin-top:0">Missing a provider or capability? Requests go straight to the catalog team — the most-asked-for tools get added first.</p>
            <div class="lbl" style="margin-top:14px">What's missing</div>
            <div class="field" style="margin:0"><input v-model="reqForm.capability" maxlength="200" placeholder="e.g. Ahrefs backlinks, flight prices, HN comments" @keyup.enter="submitToolRequest"/></div>
            <div class="lbl" style="margin-top:14px">Details <span style="text-transform:none;letter-spacing:0;color:var(--muted2)">— optional</span></div>
            <div class="field" style="margin:0"><textarea v-model="reqForm.note" maxlength="2000" rows="3" placeholder="What you'd use it for, or a link to the provider's docs" style="resize:vertical;min-width:0"></textarea></div>
            <template v-if="!me">
              <div class="lbl" style="margin-top:14px">Contact <span style="text-transform:none;letter-spacing:0;color:var(--muted2)">— optional, to hear when it lands</span></div>
              <div class="field" style="margin:0"><input v-model="reqForm.contact" maxlength="200" placeholder="you@work.com"/></div>
            </template>
            <div v-if="reqErr" class="banner" style="margin-top:12px">{{reqErr}}</div>
            <div style="display:flex;align-items:center;gap:12px;margin-top:18px">
              <button class="btn primary" @click="submitToolRequest" :disabled="reqBusy">{{reqBusy?'Sending…':'Send request'}}</button>
              <span class="sub" style="font-size:11.5px;margin:0">Takes ten seconds — no signup needed.</span>
            </div>
          </template>
          <template v-else>
            <div style="text-align:center;padding:18px 6px 8px">
              <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="var(--accent, currentColor)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>
              <p style="margin:12px 0 4px;font-weight:600">Got it — thanks.</p>
              <p class="sub" style="margin:0 0 16px">Requests are reviewed as they come in; the most-asked-for tools get added first.</p>
              <button class="btn" @click="reqAsk=false">Close</button>
            </div>
          </template>
        </div></div>
    </div>
</template>
