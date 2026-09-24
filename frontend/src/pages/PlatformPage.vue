<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>

          <!-- Stacked, not the two-column .tut-head the other pages use: a platform can be served by
               a dozen providers, and as a right-hand column that chip list steals half the width and
               wraps the title into a three-line ribbon ("People & / contact / data"). Full-width
               title, then the intro at a readable measure, then the providers as their own row. -->
          <div class="plat-head">
            <button class="btn sm" style="margin-bottom:12px" @click="go('connections')">← Catalog</button>
            <div class="plat-title">
              <span class="pt-logo" style="width:44px;height:44px;flex:0 0 44px"
                    :class="{gen:platLogoBad[platSlug]}"
                    :style="platLogoBad[platSlug] ? {background:platTileBg(platSlug)} : null">
                <img v-if="!platLogoBad[platSlug]" :src="'/logos/platforms/'+platSlug+'.svg'" alt=""
                     aria-hidden="true" @error="platLogoBad[platSlug]=true">
                <span v-else class="pt-i">{{platInitial({label:platLabel, slug:platSlug})}}</span>
              </span>
              <h1>{{platLabel}}</h1>
            </div>
            <p class="sub plat-intro">Every endpoint treg knows for this platform, one ledger, filed by subject — jobs several
              providers do sit on a single row, so you can compare price and coverage before you spend a call.</p>
            <div class="plat-provs" v-if="platProviders.length">
              <span class="plat-provs-l">Providers</span>
              <!-- Signed out, both destinations (a provider's marketplace page, the vault) need an
                   account, so the chips state who supplies the shelf and the last one is the way in. -->
              <button v-for="s in platProviders" :key="s" class="btn sm"
                      @click="publicCatalog ? openSignin() : openProvider(s)">{{provName(s)}}{{publicCatalog?'':' \u2192'}}</button>
              <button class="btn sm" @click="publicCatalog ? openSignin() : goByok(platProviders.length===1 ? platProviders[0] : null)"
                      title="Register your own provider key — your key wins over treg's and those calls are never metered">🔑 Bring your own key</button>
            </div>
          </div>

          <div v-if="platLoading" class="mk-empty" style="margin-top:14px">Loading the catalog…</div>
          <div v-else-if="platErr" class="mk-empty" style="margin-top:14px">{{platErr}}</div>
          <template v-else-if="platData">
            <!-- THE LEDGER. One table for the whole platform, filed into sticky domain sections
                 (user · video · search · shop · …, "other" always last). Within a section the
                 MERGED rows come first — a job several providers do, on one comparable line —
                 then the endpoints only one provider offers, each led by its own summary, because
                 "Get Showcase Product List" says more than the capability id ever could. -->
            <div class="lbar">
              <!-- The chips scroll rather than wrap: a bar that grows a second row as you filter
                   would shift the sticky section headings out from under it. -->
              <div class="lchips">
                <button class="mk-chip" :class="{on:!platDomain}" @click="platDomain=''">All <span>{{platBrowseCount}}</span></button>
                <button v-for="d in platDomainTabs" :key="d.domain" class="mk-chip"
                        :class="{on:platDomain===d.domain}"
                        @click="platDomain = platDomain===d.domain ? '' : d.domain">{{d.domain}} <span>{{d.n}}</span></button>
              </div>
              <label class="lchk"><input type="checkbox" v-model="platVerifiedOnly"> verified only</label>
              <input class="lfind" v-model="platQ" placeholder="filter… e.g. comments, showcase">
            </div>
            <!-- Two numbers, because a merged row is several endpoints: what you are scrolling
                 through, and how much of the catalog that actually is. -->
            <div class="lstat">{{platStats.rows}} row{{platStats.rows===1?'':'s'}} · {{platStats.eps}} endpoint{{platStats.eps===1?'':'s'}}<span
                  v-if="platDomain || platQ || platVerifiedOnly"> · filtered <button class="lclear" @click="platClearFilters">clear</button></span></div>

            <div class="ttable-wrap lwrap" v-if="platLedger.length"><table class="ledger">
              <thead><tr><th class="lth-w">What it does</th><th>Providers / route</th><th class="lth-p">Price</th><th class="lth-v">✓</th></tr></thead>
              <!-- A row, its section heading and its expanded detail are all table rows, so each
                   level rides a wrapper tag — a tbody per group would strip the row separators. -->
              <template v-for="sec in platLedger" :key="sec.domain">
                <tr class="lsec" :class="{on:sec.actions}"><td colspan="4">
                  <!-- The one collapsed section: every account/utility endpoint on the platform,
                       whatever domain it nominally belongs to. -->
                  <button v-if="sec.actions" class="lsec-btn" :aria-expanded="platActionsOpen"
                          @click="platActionsOpen=!platActionsOpen"><span class="lcar">▸</span>Actions
                    <span>{{sec.count}}</span></button>
                  <template v-else>{{sec.domain}} <span>{{sec.rows.length}}</span></template>
                </td></tr>
                <template v-for="r in sec.rows" :key="r.key">
                  <!-- EVERY row expands, merged or not: the row says what it does, the expansion
                       says how to call it, and which of the two a visitor needs is not something
                       the row shape can decide for them. -->
                  <tr class="lrow" :class="{open:platOpen[r.key], go:r.ready}" @click="toggleRow(r)"
                      :aria-expanded="!!platOpen[r.key]">
                    <!-- The flex lives on a wrapper INSIDE the cell, never on the <td>. A td with
                         `display:flex` stops being a table-cell: the browser wraps it in an
                         anonymous cell that stretches to the row height while the flex box sizes to
                         its content and keeps the border — so the row separator under column one
                         landed a pixel above the one under column two, and the hover background
                         split along the same seam. -->
                    <td class="lsum">
                      <div class="lsum-i">
                        <span class="lcar">▸</span>
                        <span style="min-width:0">
                          <b :title="r.description!==r.title ? r.description : null">{{r.title}}</b>
                        </span>
                      </div>
                    </td>
                    <td>
                      <!-- One pill PER PROVIDER — not per endpoint, or TikHub's four takes on the
                           same job repeat four identical pills. A pill is a name, a price only when
                           the price is a real number, and a ✓. THREE of them, then a +N chip: the
                           strip never wraps, so a merged row is exactly as tall as a single one.
                           The full list is one click down, on the provider sub-rows. -->
                      <div v-if="r.kind==='merged'" class="lprovs" :title="r.provTitle">
                        <span v-for="pill in r.pills" :key="pill.name" class="pchip">
                          <b>{{pill.name}}</b><span v-if="pill.price">{{pill.price}}</span><span
                              v-if="pill.verified" class="vmark">✓</span></span>
                        <span v-if="r.pillsMore" class="pchip more" :title="r.pillsMoreTitle">+{{r.pillsMore}}</span>
                      </div>
                      <span v-else class="lpath mono"><b>{{r.endpoints[0].provider_display||r.endpoints[0].provider}}</b>
                        <span class="chip" v-if="r.endpoints[0].platform_eligible===false">{{endpointAccessLabel(r.endpoints[0])}}</span><span class="cat-m">{{r.endpoints[0].method}}</span>{{r.endpoints[0].path}}<!--
                        --><span v-if="r.mgmt" class="chip lkind"
                              :title="r.endpoints[0].kind==='account' ? 'Manages the provider account itself — lists, campaigns, webhooks' : 'A helper route: token exchange, enum lookups, format cleanup'">{{r.endpoints[0].kind}}</span></span>
                    </td>
                    <td class="lprice" :title="r.priceTitle">{{r.price}}<span
                          v-if="r.priceNative" class="cost-nat">({{r.priceNative}})</span></td>
                    <td><span v-if="r.verified" class="vmark" title="Called for real against the live API, and the response captured">✓</span><span
                          v-else class="xmark" title="Documented, but treg has not called it with a live key yet">·</span></td>
                  </tr>
                  <tr v-if="platOpen[r.key]" :key="r.key+'::d'" class="cat-d">
                    <td colspan="4">
                      <!-- TWO LEVELS on a merged row: it opens to its providers, one collapsed line
                           each, and a provider opens to its own instruction. Dropping six full
                           parameter tables on one click buried the comparison the merge exists to
                           make. A single row has nothing to compare, so it skips the middle level
                           and its detail renders straight away — same block either way, so the two
                           paths can never present the instruction differently. -->
                      <div class="lgrp" v-for="e in r.endpoints" :key="e.id">
                        <button v-if="r.kind==='merged'" class="lsub" :class="{on:epOpen[e.id]}"
                                :aria-expanded="!!epOpen[e.id]" @click.stop="toggleEp(e)">
                          <span class="lcar">▸</span>
                          <span class="plogo-tile"><img class="plogo" :src="'/logos/'+e.provider+'.svg'" alt="" aria-hidden="true" @error="$event.target.style.visibility='hidden'"></span>
                          <span class="lsub-label"><b>{{e.provider_display||e.provider}}</b><span v-if="e.name" class="lsub-name">{{e.name}}</span></span>
                          <span class="chip">{{endpointAccessLabel(e)}}</span>
                          <span class="lsub-price" :title="costTitle(e.cost)">{{costShort(e.cost)}}</span>
                          <span v-if="e.verified" class="vmark" :title="'Called for real on '+e.verified">✓</span>
                          <span v-else class="xmark" title="Documented, but treg has not called it with a live key yet">·</span>
                          <span v-if="catEndpointConnected(e)" class="chip go" title="You have a connected account with an authorization method that can call this"><span class="godot"></span>connected</span>
                          <span class="lsub-path mono"><span class="cat-m">{{e.method}}</span>{{e.path}}</span>
                        </button>
                        <div class="lep" v-if="r.kind!=='merged' || epOpen[e.id]">
                          <div class="lep-h">
                            <!-- On a merged row the sub-row above already names the provider and the
                                 route, so the detail leads with the chips instead of repeating them. -->
                            <template v-if="r.kind!=='merged'">
                              <span class="plogo-tile"><img class="plogo" :src="'/logos/'+e.provider+'.svg'" alt="" aria-hidden="true" @error="$event.target.style.visibility='hidden'"></span>
                              <b>{{e.provider_display||e.provider}}</b>
                              <span class="mono cat-path"><span class="cat-m">{{e.method}}</span>{{e.path}}</span>
                            </template>
                          <span class="lep-chips">
                            <!-- The short form even here: the chip is a label, and "per result ·
                                 price in provider dashboard" is a sentence. It rides in the facts
                                 list below, where a sentence belongs. -->
                            <span class="chip">{{endpointAccessLabel(e)}}</span>
                            <span class="chip" :title="costTitle(e.cost)">{{costShort(e.cost)}}<span
                                  v-if="costNative(e.cost)" class="cost-nat">({{costNative(e.cost)}})</span></span>
                            <span v-if="e.verified" class="chip ver" :title="'Called for real on '+e.verified+' and the response captured'">verified {{e.verified}}</span>
                            <span v-else class="chip" title="Documented, but treg has not called it with a live key yet">unverified</span>
                            <!-- Scope is the difference between "point this at any handle" and "this
                                 only ever sees the account you connected", which changes what you
                                 can build. -->
                            <span v-if="e.scope==='own_account'" class="chip own"
                                  :title="e.id==='fishaudio.voices.list'?'Uses the connected Fish account under BYOK, otherwise this treg team\'s voices':'Reads the account YOU connect via OAuth, not arbitrary public accounts'">{{e.id==='fishaudio.voices.list'?'team or your account':'your account'}}</span>
                            <span v-else-if="e.scope==='any_account'" class="chip any"
                                  title="Reads any public account, page or query — no OAuth connection to that account needed">any account</span>
                            <span v-if="e.tier && e.tier!=='core'" class="chip">{{e.tier}}</span>
                          </span>
                        </div>
                        <p class="cat-sum">{{e.summary||'No summary in the catalog for this endpoint.'}}</p>
                        <!-- TABS. What you SEND and what comes BACK are two documents, and stacking
                             them made the expansion a page you scrolled rather than read. The bar
                             also carries the two things you want without scrolling at all: the
                             provider's docs, and the Connect button — the one action on this page
                             that unblocks every row, so it gets the strongest treatment the design
                             language has (ink fill), not a ghost button below the fold. -->
                        <div class="ltabs">
                          <button class="ltab" :class="{on:epTabOf(e)==='req'}" @click.stop="setEpTab(e,'req')">Request</button>
                          <!-- No captured response means no tab and no placeholder: a greyed-out tab
                               is a promise the catalog can't keep, and it draws the eye to the one
                               thing that isn't there. Endpoints without an example just have one
                               tab, which reads as a label for the pane under it. -->
                          <button v-if="e.has_example" class="ltab" :class="{on:epTabOf(e)==='res'}"
                                  @click.stop="setEpTab(e,'res')">Example response</button>
                          <span class="ltabs-r">
                            <a v-if="e.docs_url" class="btn sm" :href="e.docs_url" target="_blank" rel="noopener" @click.stop>Docs ↗</a>
                            <a v-else-if="provFact(e.provider,'pricing_url')" class="btn sm" :href="provFact(e.provider,'pricing_url')" target="_blank" rel="noopener" @click.stop>Pricing ↗</a>
                            <!-- Always offered: the drawer's access dry-run says how (or whether) THIS
                                 org can call it, and disables Run with the reason when it can't. -->
                            <!-- OAuth providers can't be served on treg's key (they act AS your account),
                                 so Connect is their primary CTA and Try-it is secondary. Key/token
                                 providers are the reverse: Try-it (treg's key) leads, own key is optional. -->
                            <!-- Calling costs money and needs a team, so a public visitor gets the
                                 way in rather than a button that can only 401. Everything else in
                                 the expander — parameters, limits, rate card, the copyable command
                                 — is open, and is the part worth reading before you sign up. -->
                            <button class="btn sm" :class="{primary:!mkOauth(e.provider)}" @click.stop="publicCatalog ? openSignin() : openEpTry(e)"
                                    :title="publicCatalog ? 'New verified accounts get $1.00 once on an eligible team, no card' : 'Call it now, or copy the agent / CLI / API way to run it'">▶ Try it</button>
                            <span v-if="catEndpointConnected(e)" class="chip go" title="You have a connected account with an authorization method that can call this"><span class="godot"></span>Connected</span>
                            <button v-else-if="mkOauth(e.provider)" class="btn sm primary" @click.stop="publicCatalog ? openSignin() : openProvider(e.provider)"
                                    :title="endpointConnectLabel(e)+' — calls act as your account'">{{endpointConnectLabel(e)}}</button>
                            <!-- Lands on the Catalog's Platform tab focused on this provider — the
                                 whole key shelf in view, not a dead-end detail page — so the user
                                 sees where their key lives among the rest before they paste it. -->
                            <button v-else-if="mkKnown(e.provider)" class="btn sm" @click.stop="publicCatalog ? openSignin() : goByok(e.provider)"
                                    :title="'Register your own '+(e.provider_display||e.provider)+' key — your key wins over treg\'s and those calls are never metered'">Bring your own key</button>
                          </span>
                        </div>
                        <div v-show="epTabOf(e)==='req'">
                        <!-- What you have to SEND. The captured response answered "what comes back"
                             while this half was missing, which made every endpoint look uncallable
                             until you left for the provider's docs. Query first, then path, then
                             body: the order you fill them in for the common GET case. -->
                        <div class="prm" v-if="paramSections(e).length || (e.input && e.input.note)">
                          <div class="prm-h">Parameters</div>
                          <p class="prm-note" v-if="e.input && e.input.note">{{e.input.note}}</p>
                          <div class="prm-sec" v-for="s in paramSections(e)" :key="s.key">
                            <div class="prm-loc">{{s.label}}<span v-if="s.type" class="prm-type mono">{{s.type}}</span></div>
                            <table class="prm-t">
                              <tr><th>Name</th><th>Type</th><th>Required</th><th>Notes</th></tr>
                              <tr v-for="p in s.rows" :key="p.name">
                                <td class="prm-n mono">{{p.name}}</td>
                                <td class="prm-ty mono">{{p.type||'—'}}</td>
                                <td><span v-if="p.required" class="prm-req">required</span><span v-else class="prm-opt">optional</span></td>
                                <td class="prm-d">
                                  <span v-if="p.note">{{p.note}}</span>
                                  <span v-if="p.example!=null" class="prm-ex mono">e.g. {{fmtExample(p.example)}}</span>
                                </td>
                              </tr>
                            </table>
                          </div>
                        </div>
                        <p v-else class="prm-none">The catalog has no parameter reference for this endpoint yet — the provider's docs have them.</p>
                        <!-- The provider-wide facts an expanded row needs and a table cell can't
                             hold: how it meters, what it rate-limits, where the rate card lives. -->
                        <ul class="lfacts" v-if="epFacts(e).length"><li v-for="(f,fi) in epFacts(e)" :key="fi">{{f}}</li></ul>
                        <!-- The line that actually runs it. `treg call` proxies the key server-side,
                             so this is paste-ready with no key on the machine that runs it. -->
                        <!-- The line that actually runs it, at the bottom of the Request tab — the
                             last thing you read before you go and run it. -->
                        <div class="lcall" v-if="e.call_template">
                          <code class="mono">{{e.call_template}}</code>
                          <button class="btn sm" @click.stop="copyCall(e)">{{platCopied===e.id?'✓ copied':'Copy'}}</button>
                        </div>
                        <div class="lep-id mono">{{e.id}}</div>
                        </div>
                        <!-- Fetched when the tab is first opened, never with the page: a platform can
                             carry hundreds of endpoints and the captured responses are the heaviest
                             thing in the catalog. -->
                        <div class="cat-ex" v-show="epTabOf(e)==='res'">
                          <div v-if="!platEx[e.id] || platEx[e.id].loading" class="mk-quiet" style="font-size:12px">Loading the captured response…</div>
                          <div v-else-if="platEx[e.id].err" class="mk-quiet" style="font-size:12px">{{platEx[e.id].err}}</div>
                          <pre v-else class="code">{{platEx[e.id].text}}</pre>
                        </div>
                        </div>
                      </div>
                    </td>
                  </tr>
                </template>
              </template>
            </table></div>
            <div v-else class="mk-empty" style="margin-top:14px">Nothing on this platform matches that filter — clear it, or
              <button class="lclear" @click="platClearFilters">start over</button>.</div>
          </template>

</template>
