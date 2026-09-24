<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div  class="scrim" role="dialog" aria-modal="true" @click.self="tokenAsk=null">
      <div class="modal" style="padding:16px">
        <h3 style="margin:0 0 6px"><span class="plogo-tile"><img class="plogo" :src="'/logos/'+tokenAsk.provider.service+'.svg'" alt="" aria-hidden="true" @error="$event.target.style.visibility='hidden'"></span>Connect {{tokenAsk.provider.display_name}}</h3>
        <p class="sub" style="margin:0 0 12px">You bring your own {{tokenAsk.provider.auth_kind==='key'?'API key':'bot'}}, so it stays yours — treg
          holds it server-side and injects it on every call.</p>
        <a v-if="tokenAsk.provider.setup_url" class="btn sm primary" :href="tokenAsk.provider.setup_url"
           target="_blank" rel="noopener">{{tokenAsk.provider.setup_action_label||'Create the app'}}</a>
        <ol style="margin:12px 0 0;padding-left:20px;color:var(--muted);font-size:12.5px;line-height:1.7">
          <li v-for="(st,i) in (tokenAsk.provider.setup_steps||[])" :key="i">{{st}}</li>
        </ol>
        <div style="margin-top:12px">
          <label class="labelcls" style="display:block;font-size:11px;color:var(--muted);margin-bottom:4px">{{tokenAsk.provider.token_label||'Token'}}</label>
          <input class="bindinput" style="width:100%" type="password" autocomplete="off"
                 :placeholder="tokenAsk.provider.token_placeholder" v-model="tokenAsk.token"
                 @keyup.enter="submitToken"/>
        </div>
        <p v-if="tokenAsk.provider.setup_note" class="sub" style="margin:10px 0 0;font-size:11.5px">{{tokenAsk.provider.setup_note}}</p>
        <p v-if="tokenAsk.provider.probe_cost_micro" class="mk-notice" style="margin:10px 0 0">
          Verifying this key makes one provider-billed test call costing {{money(tokenAsk.provider.probe_cost_micro)}}.
          treg will not reuse this paid request for health checks.
        </p>
        <div v-if="tokenAsk.err" class="banner" style="margin-top:10px">{{tokenAsk.err}}</div>
        <div style="margin-top:14px;text-align:right;display:flex;gap:8px;justify-content:flex-end">
          <button class="btn sm" @click="tokenAsk=null">Cancel</button>
          <button class="btn sm primary" :disabled="!tokenAsk.token.trim()||tokenAsk.busy" @click="submitToken">
            {{tokenAsk.busy?'Verifying…':'Connect'}}</button>
        </div>
      </div>
    </div>
</template>
