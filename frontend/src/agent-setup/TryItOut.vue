<script lang="ts">
import { defineComponent } from 'vue'
import { examples, listedOauthGroups } from './data'

export default defineComponent({
  // `providers` is the /oauth/providers listing; a provider it leaves out shows no chip.
  props: { copied: String, headingId: String, providers: Array },
  emits: ['example', 'provider'],
  computed: {
    examples: () => examples,
    oauthGroups(): ReturnType<typeof listedOauthGroups> {
      return listedOauthGroups(this.providers as { service: string }[] | undefined)
    },
  },
})
</script>

<template>
<div>
  <h2 :id="headingId" style="margin:0 0 6px;font-size:19px">Try it out</h2>
  <div class="wc-wait" style="margin:0 0 16px"><span class="wc-waitdot"></span>Waiting for your agent &mdash; copy an example below and send it to get your first result.</div>
  <div class="try-grid">
    <button v-for="ex in examples" :key="ex.k" type="button" class="try-card" @click="$emit('example',ex)">
      <span class="try-cat"><span style="display:inline-flex;align-items:center;gap:7px"><img class="try-ico" :src="'/logos/platforms/'+ex.logo+'.svg'" alt=""/><img v-if="ex.avatar" class="try-ico avatar" :src="ex.avatar" alt=""/>{{ex.cat}}</span><span class="try-copy" :class="{done:copied===ex.k}">{{copied===ex.k ? '✓ copied' : '⧉ copy'}}</span></span>
      <span class="try-txt">{{ex.show||ex.prompt}}</span>
    </button>
  </div>
  <div class="oauth-div"><span>also connect OAuth to unlock new agent capabilities</span></div>
  <div v-for="g in oauthGroups" :key="g.label" style="margin-top:12px">
    <div class="oauth-grp">{{g.label}}</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
      <button v-for="p in g.items" :key="p.s" type="button" class="prov-chip" @click="$emit('provider',p.s,g.label)"><img :src="'/logos/'+p.s+'.svg'" alt=""/>{{p.n}}</button>
      <span v-if="g.soon.length" class="soon-note" :data-tip="g.soon.map(p=>p.n).join(', ')">{{g.soon.length}} coming soon</span>
    </div>
  </div>
</div>
</template>
