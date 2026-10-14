<script>
import { useDashboard } from '../state/context'
import ProviderLogo from './ProviderLogo.vue'

// One tool, over whichever list opened it (a platform shelf, a comparison, a provider's
// tools). Not modal: the list behind it stays live, and ↑ ↓ walk it (`drawerIds`).
// Reading order is the decision order: what it does, what it costs and how well it does it, the
// one action, the command; parameters, the captured response and reviews after that.
export default {
  components: { ProviderLogo },
  setup: useDashboard,
  mounted() { this._keys = e => this.drawerKeys(e); window.addEventListener('keydown', this._keys) },
  unmounted() { window.removeEventListener('keydown', this._keys) },
}
</script>

<template>
<aside class="td" :class="{behind:!!epTry}" aria-label="Tool details">
  <div class="td-h">
    <ProviderLogo :service="drawerEp.provider" large />
    <div class="td-t">
      <a v-if="view!=='provider'" class="td-prov" :href="provUrl(drawerEp.provider)" @click.prevent="goProvider(drawerEp.provider)"
         :title="'Every tool '+(drawerEp.provider_display||drawerEp.provider)+' serves'">{{drawerEp.provider_display||drawerEp.provider}}</a>
      <span v-else class="td-prov">{{drawerEp.provider_display||drawerEp.provider}}</span>
      <b>{{drawerEp.name||clip(drawerEp.summary, 90)}}</b>
    </div>
    <span class="td-nav">
      <button @click="stepTool(-1)" aria-label="Previous tool" title="Previous (↑)"><svg viewBox="0 0 16 16"><path d="M4 10l4-4 4 4"/></svg></button>
      <button @click="stepTool(1)" aria-label="Next tool" title="Next (↓)"><svg viewBox="0 0 16 16"><path d="M4 6l4 4 4-4"/></svg></button>
      <button @click="closeTool" aria-label="Close" title="Close (Esc)"><svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8"/></svg></button>
    </span>
  </div>

  <div class="td-b">
    <p v-if="drawerEp.summary && drawerEp.summary!==drawerEp.name" class="td-sum">{{drawerEp.summary}}</p>

    <div class="td-stats">
      <div><span>Price</span><b>{{toolPrice(drawerEp)}}</b></div>
      <div><span>Works</span><b>{{drawerStats.works ? drawerStats.works.pct+'%' : '—'}}</b><i v-if="drawerStats.works">{{approxCalls(drawerStats.works.n)}}, 30 days</i></div>
      <div><span>Reviews</span><b v-if="drawerStats.useful" class="rev" :class="drawerStats.useful.tone">{{drawerStats.useful.label}}</b><b v-else-if="drawerEp.reviews" class="rev early">Early</b><b v-else>—</b><i v-if="drawerEp.reviews">{{approxTeams(drawerEp.reviews.teams)}}</i></div>
    </div>

    <!-- The primary button is the next step to your agent using this tool (`drawerNext`): connect the
         account or add the key it needs, else hand it the line. Trying it here is optional. -->
    <!-- Paused on this deployment: say so in place of the call line and the buttons that lead to it. -->
    <p v-if="drawerNext==='paused'" class="mk-notice">{{providerPaused(drawerEp.provider).message}}</p>
    <div v-else-if="drawerEp.call_template" class="pl-code"><code>{{drawerEp.call_template}}</code></div>
    <div class="td-act">
      <button v-if="drawerNext==='connect'" class="pl-btn" @click="catalogConnect(drawerEp)">{{endpointConnectLabel(drawerEp)}}</button>
      <button v-else-if="drawerNext==='key'" class="pl-btn" @click="catalogByok(drawerEp.provider, drawerEp)">Add your {{drawerEp.provider_display||drawerEp.provider}} key</button>
      <button v-if="drawerEp.call_template && drawerNext!=='paused'" class="pl-btn pl-copy" :class="{ghost:drawerNext!=='copy'}" @click="catalogCopy(drawerEp)">{{platCopied===drawerEp.id ? 'Copied' : 'Copy for your agent'}}</button>
      <button v-if="drawerNext==='copy'" class="pl-btn" :class="{ghost:!!drawerEp.call_template}" @click="catalogTry(drawerEp)">Try it</button>
      <a v-if="drawerEp.docs_url || provFact(drawerEp.provider,'pricing_url')" class="pl-link" :href="drawerEp.docs_url || provFact(drawerEp.provider,'pricing_url')"
         target="_blank" rel="noopener" @click="catalogDocs(drawerEp)">{{drawerEp.docs_url ? 'Docs' : 'Pricing'}} ↗</a>
    </div>

    <p class="td-facts">{{drawerFacts}}</p>

    <div class="td-tabs" role="tablist">
      <button role="tab" :aria-selected="epTabOf(drawerEp)==='req'" @click="setEpTab(drawerEp,'req')">Parameters</button>
      <button v-if="drawerEp.has_example" role="tab" :aria-selected="epTabOf(drawerEp)==='res'" @click="setEpTab(drawerEp,'res')">Example response</button>
      <button v-if="drawerEp.reviews" role="tab" :aria-selected="epTabOf(drawerEp)==='rev'" @click="setEpTab(drawerEp,'rev')">Reviews</button>
    </div>

    <div v-show="epTabOf(drawerEp)==='req'">
      <p v-if="drawerEp.input && drawerEp.input.note" class="td-note">{{drawerEp.input.note}}</p>
      <template v-for="s in paramSections(drawerEp)" :key="s.key">
        <p class="td-loc">{{s.label}}<span v-if="s.type">{{s.type}}</span></p>
        <dl class="td-params">
          <div v-for="p in s.rows" :key="p.name">
            <dt><code>{{p.name}}</code><span v-if="p.type">{{p.type}}</span><em v-if="p.required">required</em></dt>
            <dd v-if="p.note || p.example!=null">{{p.note}}<code v-if="p.example!=null" class="td-ex">{{fmtExample(p.example)}}</code></dd>
          </div>
        </dl>
      </template>
      <p v-if="!paramSections(drawerEp).length && !(drawerEp.input && drawerEp.input.note)" class="td-note">
        The catalog has no parameter reference for this tool yet; the provider's docs have them.</p>
      <details v-if="epFacts(drawerEp).length" class="td-more">
        <summary>Billing and limits</summary>
        <ul><li v-for="(f,fi) in epFacts(drawerEp)" :key="fi">{{f}}</li></ul>
      </details>
      <p v-if="!publicCatalog && drawerNext==='copy' && mkKnown(drawerEp.provider) && !mkOauth(drawerEp.provider)" class="td-byok">
        Have your own {{drawerEp.provider_display||drawerEp.provider}} key? <button class="pl-link" @click="catalogByok(drawerEp.provider, drawerEp)">Use it</button>:
        your key always wins, and those calls are never metered.</p>
    </div>

    <div v-if="drawerEp.has_example" v-show="epTabOf(drawerEp)==='res'" class="td-json">
      <p v-if="!platEx[drawerEp.id] || platEx[drawerEp.id].loading" class="td-note">Loading the captured response…</p>
      <p v-else-if="platEx[drawerEp.id].err" class="td-note">{{platEx[drawerEp.id].err}}</p>
      <pre v-else>{{platEx[drawerEp.id].text}}</pre>
    </div>

    <div v-if="drawerEp.reviews" v-show="epTabOf(drawerEp)==='rev'" class="td-rev">
      <!-- Scored: the label, the bar and the split. Early (no `share`): the quotes alone, said so. -->
      <template v-if="drawerStats.useful">
        <p class="td-rev-sum"><b class="rev" :class="drawerStats.useful.tone">{{drawerStats.useful.label}}</b> · {{drawerStats.useful.pct}}% positive, partly useful counting half</p>
        <div class="td-rev-bar" aria-hidden="true"><span v-for="v in verdictKinds" :key="v" :class="v" :style="{flexGrow:drawerEp.reviews.share[v]}"></span></div>
        <p class="td-rev-legend"><span v-for="v in verdictKinds" :key="v"><i :class="v"></i>{{verdictPct(drawerEp.reviews, v)}}</span></p>
        <p class="td-note">From {{approxTeams(drawerEp.reviews.teams)}}' agents after using the result, one vote per team, last 90 days.</p>
      </template>
      <template v-else>
        <p class="td-rev-sum"><b class="rev early">Early reviews</b> · {{approxTeams(drawerEp.reviews.teams)}}</p>
        <p class="td-note">What their agents said after using the result, quoted as written. A score needs 5 teams.</p>
      </template>
      <ul class="td-quotes">
        <li v-for="(q,qi) in drawerEp.reviews.samples" :key="qi">
          <span class="td-rev-v" :class="q.usefulness">{{verdictLabel(q.usefulness)}}</span>
          <p>{{q.reason}}</p>
          <span class="td-qm">{{verdictDate(q.month)}}<template v-if="verdictClient(q.client)"> · via {{verdictClient(q.client)}}</template></span>
        </li>
      </ul>
    </div>
  </div>
</aside>
</template>
