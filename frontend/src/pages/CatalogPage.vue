<script>
import { useDashboard } from '../state/context'
import FindAnswer from '../components/FindAnswer.vue'
import CatalogSearch from '../components/CatalogSearch.vue'

// The catalog index: every platform, by category. It answers what an agent can call; whose
// credential it calls with is the Connections page's question. The look is the platform shelf's.
export default { components: { FindAnswer, CatalogSearch }, setup: useDashboard }
</script>

<template>
<div class="pl cat">
  <!-- A title and the box: someone here came to look something up. Your own keys have their own page
       (Connections, in the nav); asking for a tool and listing one sit at the foot. -->
  <header class="pl-hero">
    <h1>{{toolCountText ? toolCountText+' tools' : 'Tools'}} for agents</h1>
    <!-- One box, two questions: a platform name filters the shelves as you type; when typing pauses
         the finder answers above the filtered shelves, and Enter asks for
         the full answer (state/find.js). Clearing the box is how you leave an answer. -->
    <CatalogSearch v-if="plats.list.length" v-model="q" placeholder="Search a platform, or describe what your agent needs to do" />
  </header>

  <!-- A described job is answered above the shelves rather than instead of them: the shelves
       stay, lit where a full answer landed, or still filtered under an auto answer. -->
  <FindAnswer v-if="findActive && !find.scope" class="pl-find" />

  <div class="mk-tabs-wrap" v-if="plats.list.length">
    <div class="mk-tabs" role="tablist" aria-label="Catalog categories">
      <button v-for="t in mkTabs" :key="t.key" role="tab" :aria-selected="mkTab===t.key"
              :class="{on:mkTab===t.key}" @click="mkTab=t.key">{{t.label}} <span>{{t.n}}</span></button>
    </div>
  </div>

  <section class="pl-sec" v-for="g in platCatGroups" :key="g.category">
    <h2 class="pl-h"><span>{{g.category}}</span><em>{{g.total}}</em></h2>
    <p v-if="g.hint" class="cat-hint">{{g.hint}}</p>
    <div class="pl-grid pl-grid-t">
      <!-- A card is a NAME and two facts: a description made every card tall enough that a shelf of
           twelve became a scroll. The summary survives as the hover title. -->
      <button v-for="pl in g.items" :key="pl.slug" class="pl-card pl-tool cat-card"
              :class="{'find-hit':findFull && findHits[pl.slug], 'find-dim':findFull && find.phase==='done' && findGroups.length && !findHits[pl.slug]}"
              :title="pl.summary ? pl.label+': '+pl.summary : pl.label"
              :aria-label="'Open '+pl.label" @click="openPlatform(pl.slug)">
        <span v-if="findFull && findHits[pl.slug]" class="pt-find">{{findHits[pl.slug]}} match{{findHits[pl.slug]===1?'':'es'}}</span>
        <!-- A platform's OWN mark, not its providers': the card is the platform. Anything we haven't
             drawn falls back to a generated initial tile, not a broken image. -->
        <span class="pl-logo lg" :class="{gen:platLogoBad[pl.slug]}"
              :style="platLogoBad[pl.slug] ? {background:platTileBg(pl.slug)} : null">
          <img v-if="!platLogoBad[pl.slug]" :src="'/logos/platforms/'+pl.slug+'.svg'" alt=""
               aria-hidden="true" @error="platLogoBad[pl.slug]=true">
          <span v-else class="pt-i">{{platInitial(pl)}}</span>
        </span>
        <span class="pl-tool-b">
          <span class="cat-name"><b>{{platShort(pl.label)}}</b>
            <!-- A member's own credential, and only the positive fact: calls here use your account or
                 key. Neutral, not green: every platform works, on treg's key; a green "Connected" read
                 as "only these do". -->
            <span v-if="!publicCatalog && platOwn(pl).length" class="cat-own" role="img"
                  :title="platConnTitle(pl)" :aria-label="platConnTitle(pl)">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/></svg></span></span>
          <span v-if="platPaused(pl)" class="pl-meta" :title="providerPaused(pl.providers[0]).message">Paused</span>
          <span v-else class="pl-meta">{{pl.endpoints}} tool{{pl.endpoints===1?'':'s'}}<template v-if="platPrice(pl)"> · <span :title="platPriceTitle(pl)">{{platPrice(pl).free ? platPrice(pl).text : 'from '+platPrice(pl).text}}</span></template></span>
        </span>
      </button>
    </div>
    <!-- The tail of a long shelf, as one row: a stack of the marks plus two names, so it reads as
         "there is more of this kind here" rather than as a bare count. -->
    <button v-if="g.rest.length" class="pt-more" @click="platShelfOpen[g.category]=true"
            :aria-label="'Show the other '+g.rest.length+' '+g.category+' platforms'">
      <span class="pt-stack">
        <span v-for="pl in g.rest.slice(0,7)" :key="pl.slug" class="pt-mini" :title="pl.label"
              :class="{gen:platLogoBad[pl.slug]}"
              :style="platLogoBad[pl.slug] ? {background:platTileBg(pl.slug)} : null">
          <img v-if="!platLogoBad[pl.slug]" :src="'/logos/platforms/'+pl.slug+'.svg'" alt=""
               aria-hidden="true" @error="platLogoBad[pl.slug]=true">
          <span v-else class="pt-i">{{platInitial(pl)}}</span>
        </span>
      </span>
      <span class="pt-more-t">{{moreLabel(g.rest)}}</span>
      <span class="pt-more-a" aria-hidden="true">→</span>
    </button>
  </section>

  <!-- A query that names no platform is usually a JOB, not a typo: say what missed and offer the
       finder, instead of implying the server has no catalog. -->
  <p v-if="!platCatGroups.length && platNameQuery && plats.list.length && !findSoon && !findActive" class="find-miss">No platform is called that.</p>
  <p v-else-if="plats.settled && !plats.list.length" class="pl-empty">This server has no catalog yet.
    <button v-if="!publicCatalog" class="pl-link" @click="go('connections')">Connect your own accounts</button></p>

  <footer class="cat-foot">
    <p>Missing a tool? <button class="pl-link" @click="openToolRequest()">Request a tool</button>
      <span aria-hidden="true">·</span> Sell an API? <button class="pl-link" @click="vendorAsk=true">List as vendor</button></p>
  </footer>
</div>
</template>
