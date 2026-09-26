<script>
import { useDashboard } from '../state/context'
import FindAnswer from '../components/FindAnswer.vue'
export default { components: { FindAnswer }, setup: useDashboard, beforeUnmount(){ this.findUnschedule(); } }
</script>

<template>

          <div class="tut-head">
            <div><h1>{{toolCountText ? toolCountText+' tools' : 'Tools'}} for agents</h1><p class="sub" style="margin:0">Connect an account once. treg holds the credential server-side and injects it on every call — nothing lands on your machine.</p></div>
            <!-- (The balance pill lives in the side nav, above the account block.) -->
            <div class="tut-actions">
              <button class="btn sm" @click="openToolRequest()" title="Missing a tool or provider? Tell us — requests steer what gets added next"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><line x1="12" y1="7" x2="12" y2="13"/><line x1="9" y1="10" x2="15" y2="10"/></svg>Request a tool</button>
              <button class="btn sm" @click="vendorAsk=true" title="Sell an API? Get it listed in this catalog"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.83z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>List as vendor</button>
              <!-- The one header action a paying visitor is actually looking for: where do I put MY
                   key. It only switches tabs, but naming it makes the Platform tab findable. -->
              <button class="btn sm primary" @click="publicCatalog ? openSignin() : goByok()" title="Register your own provider key — your key wins over treg's and those calls are never metered"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21 2-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777Zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>Bring your own key</button>
            </div>
          </div>
          <!-- One box, two questions: a platform name filters the shelves as you type, and the finder
               answers whatever is typed once typing pauses, or at once on Enter (state/find.js).
               Clearing the box is how you leave an answer. -->
          <div class="cat-find" v-if="plats.list.length">
            <svg class="cat-find-i" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
            <input :ref="el => setElement('search', el)" v-model="q" aria-label="Search the catalog"
                   placeholder="Search a platform, or describe what your agent needs to do"
                   @input="findSchedule($event.target.value)"
                   @keydown.enter="q.trim() && findRun(q)" @keydown.esc="q=''; findExit()">
            <button v-if="q" class="cat-find-x" type="button" aria-label="Clear the search" @click="q=''; findExit()">×</button>
          </div>
          <!-- A sentence is a job, not a name: say so where the eye already is, as one clickable row. -->
          <!-- Enter searches every tool for whatever is typed. A name still filters the shelves as you
               type, so for a short query the row is quieter; for a sentence it is the main action. -->
          <button v-if="plats.list.length && q.trim() && !findActive" class="cat-find-suggest" :class="{quiet:!findIsJob(q)}" type="button" @click="findRun(q)">
            <span class="cat-find-suggest-i" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg></span>
            <span class="cat-find-suggest-t">{{findIsJob(q) ? 'Find tools for' : 'Search all tools for'}} <b>“{{q.trim()}}”</b></span>
            <kbd>Enter</kbd>
          </button>
          <div v-if="connErr" class="banner" style="margin-top:12px">{{connErr}}</div>
          <div v-for="c in needSecondCred" :key="'n'+c.id" class="banner" style="margin-top:12px">
            <div><b>{{c.name}}</b> is connected, but can't call the API on its own yet. {{c.extra_credential_note}}</div>
            <div style="display:flex;gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap">
              <input class="bindinput" style="flex:1;min-width:220px" type="password"
                     :placeholder="c.extra_credential_label||'Second credential'"
                     v-model="extraCred[c.id]" @keyup.enter="saveExtraCred(c)"/>
              <button class="btn sm primary" :disabled="!extraCred[c.id] || extraBusy===c.id" @click="saveExtraCred(c)">
                {{extraBusy===c.id?'Saving…':'Save & finish setup'}}
              </button>
            </div>
          </div>
          <div v-if="staleConns.length" class="banner" style="margin-top:12px">
            <b>{{staleConns.length}} connection{{staleConns.length===1?'':'s'}} need reconnecting.</b>
            treg can't renew {{staleConns.length===1?'it':'them'}} automatically — without a fresh consent
            {{staleConns.length===1?'it':'they'}} will stop working:
            <span class="mono">{{staleConns.map(c=>c.name).join(', ')}}</span>
          </div>

          <!-- The marketplace has two axes, and the tab bar is the choice between them: every tab
               but the last asks WHICH DATA you want (platform tiles, grouped by category), while
               "Platform" is the original integration shelf — which ACCOUNT you hold. Data-first is
               the default because that is the question an agent actually arrives with. -->
          <!-- A described job (the search box's Enter, see state/find.js) is answered here, above the
               shelves rather than instead of them: the shelves stay, lit where the answer landed. -->
          <FindAnswer v-if="findActive" />

          <div class="mk-tabs-wrap" v-if="plats.list.length">
            <div class="mk-tabs" role="tablist" aria-label="Catalog">
              <button v-for="t in mkTabs" :key="t.key" role="tab" :aria-selected="mkTabActive===t.key"
                      :class="{on:mkTabActive===t.key}" @click="mkTab=t.key">{{t.label}} <span>{{t.n}}</span></button>
            </div>
          </div>

          <!-- Platform tiles: the endpoint catalog's own axis. Absent — silently — on a server
               whose build has no /catalog, which then only has the Platform tab to show. -->
          <template v-if="mkTabActive!=='platform'">
            <div class="tgroup shelf" v-for="g in platCatGroups" :key="g.category">
              <div class="sec-head">
                <h2 class="sec-h"><b>{{g.category}}</b><span class="sec-n">{{g.total}}</span></h2>
                <span class="sec-hint" v-if="g.hint">{{g.hint}}</span>
              </div>
              <div class="pt-grid">
                <!-- The card is a NAME and two facts, nothing more: a description paragraph made
                     every card tall enough that a shelf of twelve became a scroll, and it repeated
                     what the name and category already said. The summary survives as the hover
                     title, so nothing is lost for the one visitor who wants it. -->
                <button v-for="pl in g.items" :key="pl.slug" class="pt-card"
                        :class="{'find-hit':findHits[pl.slug], 'find-dim':find.phase==='done' && findGroups.length && !findHits[pl.slug]}"
                        :title="pl.summary ? pl.label+' — '+pl.summary : pl.label"
                        :aria-label="'Open '+pl.label" @click="openPlatform(pl.slug)">
                  <span v-if="findHits[pl.slug]" class="pt-find">{{findHits[pl.slug]}} match{{findHits[pl.slug]===1?'':'es'}}</span>
                  <div class="pt-top">
                    <!-- A platform's OWN mark, not its providers': the card is the platform. Anything
                         we haven't drawn falls back to a generated initial tile, not a broken image. -->
                    <span class="pt-logo" :class="{gen:platLogoBad[pl.slug]}"
                          :style="platLogoBad[pl.slug] ? {background:platTileBg(pl.slug)} : null">
                      <img v-if="!platLogoBad[pl.slug]" :src="'/logos/platforms/'+pl.slug+'.svg'" alt=""
                           aria-hidden="true" @error="platLogoBad[pl.slug]=true">
                      <span v-else class="pt-i">{{platInitial(pl)}}</span>
                    </span>
                    <span style="min-width:0">
                      <span class="pt-name">{{platShort(pl.label)}}</span>
                      <span class="pt-cat">{{pl.category}}</span>
                    </span>
                    <!-- The corner answers the only question that changes what you do next: can I
                         call this today, or is there a signup between me and it? Which provider
                         serves it is a decision for the platform page, not a browse-time fact. -->
                    <!-- Connection state is a MEMBER fact. On a public catalog URL there is no
                         team to be connected, so the badge would read "not connected" on all 80
                         tiles — a wall of red herrings for someone just browsing what exists. -->
                    <span class="pt-conn" v-if="!publicCatalog">
                      <span v-if="platConnected(pl)" class="chip go"
                            :title="'You have a connected account for '+platConnNames(pl)"><span class="godot"></span>Connected</span>
                      <span v-else class="pt-quiet" title="No connected account for any provider serving this platform yet">not connected</span>
                    </span>
                  </div>
                  <div class="pt-foot">
                    <span class="pt-eps"><span class="pt-dot"></span>{{pl.endpoints}} endpoint{{pl.endpoints===1?'':'s'}}</span>
                    <span v-if="platPrice(pl)" class="pt-price" :class="{free:platPrice(pl).free}"
                          :title="platPriceTitle(pl)"><template v-if="!platPrice(pl).free">from </template><b>{{platPrice(pl).text}}</b></span>
                  </div>
                </button>
              </div>
              <!-- The tail of a long shelf, as one row: a stack of the marks plus two names, so it
                   reads as "there is more of this kind here" rather than as a bare count. -->
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
            </div>
            <!-- A query that names no platform is usually a JOB, not a typo: say what missed and offer
                 the finder, instead of implying the server has no catalog. -->
            <p v-if="!platCatGroups.length && platNameQuery && plats.list.length && !findSoon" class="find-miss">
              No platform is called that.</p>
            <div v-else-if="plats.settled && !platCatGroups.length && !q.trim()" class="mk-empty">
              No catalogued platforms{{mkTabActive==='all'?'':' in '+mkTabActive}} on this server yet — the
              <b>Platform</b> tab lists every integration you can connect.
            </div>
          </template>

          <!-- The original marketplace: integrations grouped by category, unchanged. -->
          <template v-if="mkTabActive==='platform'">
          <div class="mk-filters" style="margin-top:0">
            <button class="mk-chip" :class="{on:mkCat===''}" @click="mkCat=''">All <span>{{providers.length}}</span></button>
            <button v-for="g in providerGroups" :key="g.category" class="mk-chip"
                    :class="{on:mkCat===g.category}" @click="mkCat=g.category">{{g.category}} <span>{{g.items.length}}</span></button>
          </div>

          <div class="tgroup" v-for="g in shownGroups" :key="g.category">
            <div class="tgh">{{g.category}} <span class="tgh-n">{{g.items.length}}</span><span class="tgh-hint">{{g.hint}}</span></div>
            <!-- A LIST, not cards: this tab is where "bring your own key" lands, and the visitor is
                 scanning forty names for one they hold an account with. One provider per row keeps
                 every name on the same left edge; the summary rides along muted and truncated. The
                 whole row opens the provider page; the Connect / Add key action is inline, so the
                 common path is one click, not two. -->
            <div class="ttable-wrap"><table class="ttable prov-list">
              <tr v-for="p in g.items" :key="p.service" :id="'prov-'+p.service" class="prov-row"
                  :class="{focus:byokFocus===p.service}" tabindex="0" role="button"
                  :aria-label="'Open '+p.display_name"
                  @click="publicCatalog ? goPublicTool(p.service) : openProvider(p.service)"
                  @keyup.enter="publicCatalog ? goPublicTool(p.service) : openProvider(p.service)">
                <td class="tn prov-n">
                  <!-- Flex on a wrapper INSIDE the cell, never on the <td> — see the ledger rows. -->
                  <span class="prov-n-i">
                    <span class="plogo-tile"><img class="plogo" :src="'/logos/'+p.service+'.svg'" alt="" aria-hidden="true" @error="$event.target.style.visibility='hidden'"></span>
                    <span class="prov-name"><b>{{p.display_name}}</b><span class="prov-sum">{{p.summary}}</span></span>
                  </span>
                </td>
                <td class="prov-auth">{{p.auth_kind==='key'?'API key':(p.auth_kind==='token'?'your own bot':'one-click')}}</td>
                <td class="ta prov-st">
                  <!-- Connected count is the one fact that changes what you'd do next. -->
                  <span v-if="connCount[p.service]" class="chip ok">{{connCount[p.service]}} connected</span>
                  <span v-else-if="!p.configured" class="chip warn" title="This server has no client credentials for the provider">not configured</span>
                  <span v-else class="mk-quiet">not connected</span>
                </td>
                <td class="tx" @click.stop>
                  <button v-if="p.configured" class="btn sm" :class="{primary:!connCount[p.service]}"
                          @click="publicCatalog ? openSignin() : startConnect(p)"
                          :title="p.auth_kind==='key'||p.auth_kind==='token' ? 'Paste your own '+p.display_name+' credential — treg keeps it server-side' : 'Connect '+p.display_name+' — approve on their site'">
                    {{connCount[p.service] ? 'Add account' : (p.auth_kind==='key'||p.auth_kind==='token' ? 'Add key' : 'Connect')}}</button>
                </td>
              </tr>
            </table></div>
          </div>
          </template>


</template>
