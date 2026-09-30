<script>
import { useDashboard } from '../state/context'
import ToolDrawer from '../components/ToolDrawer.vue'
import FindAnswer from '../components/FindAnswer.vue'
import CatalogSearch from '../components/CatalogSearch.vue'
import ProviderLogo from '../components/ProviderLogo.vue'
import { DataTable } from '../components/ui/table'

const LIST = [
  { key: 'tool', header: 'Tool', wrap: true, mobile: 'primary' },
  { key: 'provider', header: 'Provider' },
  { key: 'price', header: 'Price', align: 'right' },
]

// A platform shelf, read at two levels: the shelf (the capabilities several providers serve, then
// every other tool) and one comparison (let treg pick, or compare the providers). A tool opens in a drawer over either,
// so a comparison never loses its table. The look is the landing page's: surfaces, not boxes.
export default {
  components: { ToolDrawer, FindAnswer, CatalogSearch, DataTable, ProviderLogo },
  setup: useDashboard,
  data: () => ({ listColumns: LIST }),
  methods: {
    shelfKey(t) { return t.job ? 'job:'+t.key : t.id },
    // A job several providers do opens its comparison; a one-provider tool opens in the drawer.
    openShelfItem(t) { t.job ? this.openComparison(t.slug) : this.openTool(t.id) },
  },
}
</script>

<template>
<div class="pl" :class="{dopen:!!drawerEp}">
  <div v-if="platLoading || catalogArm==='pending'" class="pl-empty">Loading the catalog…</div>
  <div v-else-if="platErr" class="pl-empty">{{platErr}}</div>

  <!-- THE SHELF -->
  <template v-else-if="platData && !platCap">
    <header class="pl-hero">
      <nav class="pl-crumbs" aria-label="Breadcrumb"><a href="/catalog" @click.prevent="go('catalog')">Catalog</a><span>/</span>{{platLabel}}</nav>
      <h1>{{platLabel}}</h1>
      <p v-if="platRow && platRow.summary" class="pl-lede">{{platRow.summary}}</p>
      <CatalogSearch v-model="platQ" :scope="platSlug" :scope-label="platLabel"
                     :placeholder="'Search '+platLabel+', or describe what your agent needs to do'" />
    </header>

    <FindAnswer v-if="findActive && find.scope===platSlug" class="pl-find" />

    <!-- The shelf, by use: the most used items as cards of one height (a short title, who serves it,
         the price), then the rest, and the account plumbing, as lists. A job several providers do
         opens its comparison; a one-provider tool opens in the drawer. -->
    <section v-if="platTop.length" class="pl-sec">
      <h2 class="pl-h"><span>{{platTopMeasured ? 'Most used' : 'Featured'}}</span><i></i></h2>
      <div class="pl-grid pl-grid-t">
        <template v-for="t in platTop" :key="t.job ? 'job:'+t.key : t.id">
          <a v-if="t.job" class="pl-card pl-cmp pl-job" :href="platUrl(platSlug, t.slug)" :title="t.hint" @click.prevent="openComparison(t.slug)">
            <b>{{t.title}}</b>
            <span class="pl-job-f">
              <span class="pl-stack" aria-hidden="true"><ProviderLogo v-for="s in t.logos" :key="s" :service="s" /></span>
              <span v-if="t.range" class="pl-job-p">{{t.range}}</span>
            </span>
            <span class="pl-meta">{{t.provN}} providers<template v-if="t.routed"> · <span class="pl-job-auto" title="One call: treg picks the provider for you">auto-route</span></template></span>
          </a>
          <button v-else class="pl-card pl-job" :class="{on:drawerTool===t.id}" :title="t.e.summary||t.title" @click="openTool(t.id)">
            <b>{{t.title}}</b>
            <span class="pl-job-f">
              <span class="pl-stack" aria-hidden="true"><ProviderLogo :service="t.e.provider" /></span>
              <span class="pl-job-p">{{toolPrice(t.e)}}</span>
            </span>
            <span class="pl-meta">{{t.e.provider_display||t.e.provider}}</span>
          </button>
        </template>
      </div>
    </section>

    <section v-for="sec in platSections" :key="sec.key" class="pl-sec">
      <h2 class="pl-h" :class="{'pl-h-quiet':sec.quiet}"><span>{{sec.label}}</span><i></i><em>{{sec.items.length}}</em></h2>
      <DataTable :class="{'pl-list-quiet':sec.quiet}" :columns="listColumns" :rows="sec.shown" :row-key="shelfKey" :selected="drawerTool"
                 interactive surface @row-click="openShelfItem">
        <template #cell-tool="{ row: t }"><b :title="t.job ? t.hint : (t.e.summary||t.title)">{{t.title}}</b></template>
        <template #cell-provider="{ row: t }">
          <span v-if="t.job" class="pl-by"><span class="pl-stack" aria-hidden="true"><ProviderLogo v-for="s in t.logosFew" :key="s" :service="s" /></span>{{t.provN}} providers<template v-if="t.routed"> · <span class="pl-job-auto">auto-route</span></template></span>
          <span v-else class="pl-by"><ProviderLogo :service="t.e.provider" />{{t.e.provider_display||t.e.provider}}</span>
        </template>
        <template #cell-price="{ row: t }">{{t.job ? t.range : toolPrice(t.e)}}</template>
      </DataTable>
      <button v-if="sec.hidden" class="pl-link pl-more-rows" @click="platSetupOpen=true">Show all {{sec.hidden}}</button>
    </section>

    <p v-if="platFilterQ && !platShelf.length && !platPlumbing.length && !findActive && !findSoon" class="pl-empty">
      Nothing in {{platLabel}} matches “{{platQ.trim()}}”. <button class="pl-link" @click="platQ=''">Clear the search</button></p>

    <footer v-if="platProvLine.length" class="pl-provs">
      <span class="pl-eyebrow">Served by</span>
      <div class="pl-provs-l">
        <a v-for="p in platProvLine" :key="p.s" class="pl-prov" :href="provUrl(p.s)" @click.prevent="goProvider(p.s)">
          <ProviderLogo :service="p.s" />{{p.name}}</a>
      </div>
    </footer>
  </template>

  <!-- ONE JOB -->
  <template v-else-if="platData && platCap">
    <div v-if="!platCapRow" class="pl-empty">{{platLabel}} has no job called “{{platCap}}”.
      <button class="pl-link" @click="closeComparison">See every job on {{platLabel}}</button></div>
    <template v-else>
      <header class="pl-hero">
        <nav class="pl-crumbs" aria-label="Breadcrumb"><a href="/catalog" @click.prevent="go('catalog')">Catalog</a><span>/</span><a
          :href="platUrl(platSlug)" @click.prevent="closeComparison">{{platLabel}}</a></nav>
        <h1 class="pl-h1-cmp">{{platCapRow.description}}</h1>
      </header>

      <!-- treg's own answer first when there is one: one call, and nobody has to choose. -->
      <section v-if="platComparison.routed" class="pl-autocard">
        <div class="pl-auto-t">
          <p class="pl-eyebrow">Auto-route</p>
          <h2>{{platComparison.provN}} providers, one call.</h2>
          <p>treg picks the best match for what you send and tries the next if one comes back empty.
            Your own keys always go first.</p>
          <ul class="pl-auto-f">
            <li><b>{{costShort(platComparison.routed.cost)}}</b>starting price</li>
            <li><b>$1</b>cap per call</li>
          </ul>
        </div>
        <div class="pl-auto-r">
          <!-- The page's one primary action is handing this line to your agent: copying needs no
               account and costs nothing. Trying it here is the optional step beside it. -->
          <div class="pl-code"><code>{{platComparison.routed.call_template}}</code></div>
          <div class="pl-auto-a">
            <button class="pl-btn pl-copy" @click="catalogCopy(platComparison.routed)">{{platCopied===platComparison.routed.id ? 'Copied' : 'Copy for your agent'}}</button>
            <button class="pl-btn ghost" @click="catalogTry(platComparison.routed)">Try it</button>
            <button class="pl-link" @click="openTool(platComparison.routed.id)">How it picks →</button>
          </div>
        </div>
      </section>

      <section class="pl-sec">
        <h2 class="pl-h"><span>{{platComparison.routed ? 'Or choose a provider' : 'Choose a provider'}}</span><i></i><em>{{platComparisonRows.length}}</em></h2>
        <DataTable :columns="comparisonColumns" :rows="platComparisonRows" :row-key="r => r.id" v-model:sort="platComparisonSort"
                   :selected="drawerTool" interactive variant="plain" surface @row-click="r => openTool(r.id)">
          <template #cell-provider="{ row: r }"><div class="c-prov-i">
            <ProviderLogo :service="r.e.provider" large />
            <span class="c-name"><b>{{r.e.provider_display||r.e.provider}}<span v-if="!r.e.verified" class="c-unv" title="Documented, but treg has not called it with a live key yet">unverified</span></b>
              <span v-if="r.twin" class="c-sub">{{clip(r.e.name||r.e.summary, 52)}}</span></span>
          </div></template>
          <template #cell-takes="{ row: r }"><span v-for="t in r.takes" :key="t" class="pl-tag">{{t}}</span><span v-if="!r.takes.length" class="c-none">—</span></template>
          <template #cell-price="{ row: r }"><span class="c-price">{{toolPrice(r.e)}}</span></template>
          <template #cell-works="{ row: r }"><span :title="worksTitle(r)"><template v-if="r.works"><span class="c-v">{{r.works.pct}}%</span><span class="c-n">{{approxCalls(r.works.n)}}</span></template><span v-else class="c-none">—</span></span></template>
          <template #cell-useful="{ row: r }"><span :title="usefulTitle(r)"><template v-if="r.useful"><span class="c-rev" :class="r.useful.tone">{{r.useful.label}}</span><span class="c-n">{{approxTeams(r.useful.n)}}</span></template><template v-else-if="r.e.reviews"><span class="c-rev early">Early</span><span class="c-n">{{approxTeams(r.e.reviews.teams)}}</span></template><span v-else class="c-none">—</span></span></template>
        </DataTable>
        <p class="pl-note"><b>Works</b> share of the last 30 days' calls that did not end in a provider error, past 20 calls.
          <b>Reviews</b> what teams' agents said after using the result, from Positive to Negative, one vote per team, scored once 5 teams have rated it; before that, Early, with their reasons quoted in the tool.</p>
      </section>

      <section v-if="platOtherComparisons.length" class="pl-sec">
        <h2 class="pl-h"><span>Other jobs on {{platLabel}}</span><i></i></h2>
        <div class="pl-grid">
          <a v-for="j in platOtherComparisons" :key="j.key" class="pl-card pl-cmp sm" :href="platUrl(platSlug, j.slug)" @click.prevent="openComparison(j.slug)">
            <b>{{j.title}}</b><span class="pl-meta">{{j.meta}}</span>
          </a>
        </div>
      </section>
    </template>
  </template>

  <ToolDrawer v-if="drawerEp" />
</div>
</template>
