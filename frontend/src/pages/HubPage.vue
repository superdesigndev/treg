<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
          <template v-if="!hub.tool">
            <h1>Hub - {{activeName}}</h1>
            <p class="sub">Tools your team published for other people's agents: a JSON steps recipe or a script in a sandbox. Every step runs through your own tools and keys; callers pay the steps and your price.</p>
            <p v-if="hub.err" class="err">{{hub.err}}</p>
            <p v-if="hub.note" class="sub">{{hub.note}}</p>
            <div class="statgrid" style="margin:12px 0;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
              <div class="stat"><div class="n">{{hubLive()}}</div><div class="l">live tools</div></div>
              <div class="stat"><div class="n">{{money(hubEarned30())}}</div><div class="l">earned, 30 days</div></div>
              <div class="stat"><div class="n">{{hubRuns30()}}</div><div class="l">runs by others, 30 days</div></div>
              <div class="stat"><div class="n">{{hubFailing()}}</div><div class="l">failing</div></div>
            </div>
            <p class="sub">Create one from your terminal or your agent: <code>treg hub init &lt;name&gt; --script</code> · <code>hub_create</code> over MCP.</p>
            <p v-if="hub.loading" class="sub">Loading…</p>
            <p v-else-if="!hubNewest().length" class="sub">No hub tools yet.</p>
            <table v-else>
              <tr><th>Tool</th><th>Ver</th><th>Status</th><th>Kind</th><th>Price</th><th>Health</th><th style="text-align:right">Runs 30d</th><th style="text-align:right">Earned 30d</th></tr>
              <tr v-for="t in hubNewest()" :key="t.tool_id" class="act-row" @click="openHubTool(t)">
                <td><b>{{t.tool_id}}</b><div class="muted" style="font-size:11px;max-width:52ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{t.summary}}</div></td>
                <td>{{t.version}}</td>
                <td><span :class="{ok:t.status==='live', warn:t.status==='failed', muted:t.status==='retired'}">{{t.status}}</span></td>
                <td>{{t.kind}}</td>
                <td :title="t.price_label">{{t.price_range||t.price_label||('$'+t.price_usd+'/run')}}<span v-if="t.price_samples" class="muted"> · {{t.price_samples}} {{t.price_samples===1?'run':'runs'}}</span></td>
                <td><span :class="{ok:t.health==='ok', warn:t.health==='failing', muted:t.health==='unknown'}">{{t.health}}</span><span v-if="t.fails_in_a_row" class="muted"> · {{t.fails_in_a_row}} failed in a row</span></td>
                <td style="text-align:right">{{t.runs_30d}}</td>
                <td style="text-align:right">{{money(t.earned_30d_micro)}}</td>
              </tr>
            </table>
          </template>

          <template v-else>
            <div style="margin-bottom:8px"><button class="btn sm" @click="closeHubTool()">← All tools</button></div>
            <h1>{{hub.tool.tool_id.split('.').slice(1).join('.')}} <span class="muted" style="font-size:13px">v{{hub.tool.version}} · {{hub.tool.status}} · {{hub.tool.kind}}</span></h1>
            <p class="sub" style="margin:0 0 8px"><code>{{hub.tool.tool_id}}</code> · <a :href="hubPage(hub.tool)" target="_blank" rel="noopener">public page ↗</a></p>
            <p v-if="hub.err" class="err">{{hub.err}}</p>
            <p v-if="hub.note" class="sub">{{hub.note}}</p>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 4px">
              <button class="btn sm" @click="copyText(hubCallLine(hub.tool),'the call line')">Copy call line</button>
              <button class="btn sm" @click="copyText(hubPage(hub.tool),'the share URL')">Copy share URL</button>
              <button v-if="hub.tool.status==='live' && canRegister" class="btn sm" :class="{danger:hub.confirmRetire===hub.tool.tool_id}" @click="retireHubTool()">{{hub.confirmRetire===hub.tool.tool_id?'Confirm retire':'Retire tool'}}</button>
            </div>
            <div class="tabs" style="margin:10px 0 4px">
              <button :class="{active:hub.tab==='overview'}" @click="hub.tab='overview'">Overview</button>
              <button :class="{active:hub.tab==='versions'}" @click="hub.tab='versions'">Versions</button>
              <button :class="{active:hub.tab==='price'}" @click="hub.tab='price'">Price</button>
              <button :class="{active:hub.tab==='listing'}" @click="hub.tab='listing'">Listing</button>
              <button :class="{active:hub.tab==='earnings'}" @click="hub.tab='earnings'; loadHubEarnings()">Earnings</button>
              <button :class="{active:hub.tab==='runs'}" @click="hub.tab='runs'; loadHubHealth()">Runs &amp; log</button>
              <button :class="{active:hub.tab==='health'}" @click="hub.tab='health'; loadHubHealth()">Health</button>
            </div>

            <template v-if="hub.tab==='overview'">
              <div class="statgrid" style="margin:12px 0;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
                <div class="stat"><div class="n">{{hubRunCost(hub.tool)}}</div><div class="l">{{hub.tool.price_samples ? 'a run cost the caller, fees + your price ('+hub.tool.price_samples+(hub.tool.price_samples===1?' recent run)':' recent runs)') : 'a run costs the caller (no runs yet)'}}</div></div>
                <div class="stat"><div class="n sm">{{hub.tool.price_label}}</div><div class="l">your price</div></div>
                <div class="stat"><div class="n">{{hub.tool.runs_30d}}</div><div class="l">runs by others, 30d</div></div>
                <div class="stat"><div class="n">{{money(hub.tool.earned_30d_micro)}}</div><div class="l">earned, 30d</div></div>
                <div class="stat"><div class="n">{{hub.tool.health}}</div><div class="l">health</div></div>
              </div>
              <div class="lbl">Summary <span class="muted">(what an agent reads first)</span></div>
              <p class="sub">{{hub.tool.summary}}</p>
              <div class="lbl">Inputs</div>
              <table><tr><th>Name</th><th>Type</th><th>Default</th><th>Note</th></tr>
                <tr v-for="(v,k) in hub.tool.inputs" :key="k"><td><code>{{k}}</code></td><td>{{v.type}}<span v-if="v.max!==undefined"> ≤ {{v.max}}</span></td><td>{{v.default!==undefined?JSON.stringify(v.default):'required'}}</td><td class="muted">{{v.note||''}}</td></tr>
              </table>
              <div class="lbl" style="margin-top:12px">Uses <span class="muted">(only you see this)</span></div>
              <p class="sub"><code v-for="u in hub.tool.uses" :key="u" style="margin-right:8px">{{u}}</code></p>
              <div class="lbl">Output</div>
              <p class="sub"><code>{{(hub.tool.output&&hub.tool.output.fields)?hub.tool.output.fields.join(', '):Object.keys(hub.tool.output||{}).join(', ')}}</code></p>
              <template v-if="hub.tool.data"><div class="lbl">Data</div>
              <p class="sub">{{hub.tool.data.rows.toLocaleString()}} rows · {{(hub.tool.data.bytes/1024).toFixed(0)}} KB uploaded with this version (read-only; replace data.csv and publish for a new version)</p></template>
              <div class="lbl">Call it</div>
              <pre style="white-space:pre-wrap;word-break:break-all">{{hubCallLine(hub.tool)}}
POST {{proxy}}/call/{{hub.tool.tool_id}}    X-Treg-Token · JSON body of inputs</pre>
              <p class="sub">Files are read-only here; publish a new version from the terminal (<code>treg hub publish .</code>) or your agent (<code>hub_update</code>).</p>
            </template>

            <template v-if="hub.tab==='versions'">
              <p class="sub">The newest live version serves the id. A caller may pin one with <code>@N</code>; a pinned old version stays callable 30 days after a newer one lands.</p>
              <table><tr><th>Ver</th><th>Status</th><th>Published</th><th>By</th><th>Check</th><th>Health</th></tr>
                <tr v-for="v in hubVersions(hub.tool.tool_id)" :key="v.version"><td>{{v.version}}</td><td>{{v.status}}</td><td>{{(v.created_at||'').slice(0,16).replace('T',' ')}}</td><td>{{v.created_by}}</td>
                  <td>{{v.check_result?v.check_result.status:'-'}}<span v-if="v.check_result&&v.check_result.charged_micro" class="muted"> · {{v.check_result.charged_micro}} µ$</span></td><td>{{v.health}}</td></tr>
              </table>
            </template>

            <template v-if="hub.tab==='price'">
              <!-- Display only. Pricing is a block in recipe.json with three modes and optional bounds,
                   so a form here would cover one mode and silently flatten the others. The maker's
                   agent edits the block and publishes; the check gates the new version. -->
              <div class="statgrid" style="margin:12px 0;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))">
                <div class="stat"><div class="n sm">{{hub.tool.price_label}}</div><div class="l">your price</div></div>
                <div class="stat"><div class="n">{{hubRunCost(hub.tool)}}</div><div class="l">{{hub.tool.price_samples ? 'what a run cost the caller, '+hub.tool.price_samples+(hub.tool.price_samples===1?' recent run':' recent runs') : 'no runs yet'}}</div></div>
              </div>
              <p class="sub">{{hubPriceExplain(hub.tool)}} Provider fees (the catalog steps) are billed to the caller on top at cost; you pay nothing from them. Earned credit lands on your balance at once. No platform share in the MVP.</p>
              <div class="lbl">The pricing block in recipe.json <span class="muted">(version {{hub.tool.version}})</span></div>
              <pre style="white-space:pre-wrap">{{JSON.stringify(hub.tool.pricing||{}, null, 2)}}</pre>
              <div class="lbl" style="margin-top:12px">Change it</div>
              <p class="sub">Ask your agent: it edits the <span class="mono">pricing</span> block and publishes a new version, which goes live once its check passes. Callers pinned to <span class="mono">@{{hub.tool.version}}</span> keep this price.</p>
              <pre style="white-space:pre-wrap">{{hubPricePrompt(hub.tool)}}</pre>
              <button class="btn primary sm" @click="copyText(hubPricePrompt(hub.tool),'the prompt')">Copy prompt for your agent</button>
            </template>

            <template v-if="hub.tab==='listing'">
              <p class="sub">Neither choice bumps the version. Once listed, every new version and price change waits for treg's review.</p>
              <div style="margin:14px 0 4px">
                <div class="lbl">In catalog search</div>
                <p class="sub" style="margin:4px 0;max-width:70ch">
                  <b :class="{ok:hubListing(hub.tool)==='approved', warn:hubListing(hub.tool)==='rejected'}">{{hubListingWords(hub.tool)}}</b>
                  <template v-if="hubListing(hub.tool)==='rejected' && hub.tool.listing.reason"> Reason: {{hub.tool.listing.reason}}</template>
                  <template v-if="hubListing(hub.tool)==='requested' && hub.tool.listing.reason"> Last rejected: {{hub.tool.listing.reason}}</template>
                </p>
                <p class="sub" style="margin:4px 0;max-width:70ch">
                  <template v-if="hubListing(hub.tool)==='approved' && hub.tool.listing.capability">Beside the catalog providers of <code>{{hub.tool.listing.capability}}</code>: <code>catalog_get</code> on any of them shows your tool, with its success rate.</template>
                  <template v-else-if="hub.tool.capability">Proposed job: <code>{{hub.tool.capability}}</code>. Once approved, your tool sits beside that job's providers.</template>
                  <template v-else>No job named. Add <code>"capability"</code> to recipe.json (a capability id from <code>treg catalog search</code>) to sit beside that job's providers once approved.</template>
                </p>
                <p v-if="hub.tool.listing && hub.tool.listing.update" class="sub" style="margin:4px 0;max-width:70ch">
                  <template v-if="hub.tool.listing.update.state==='pending'"><b class="warn">An update waits for treg's review:</b>
                    <template v-if="hub.tool.listing.update.version"> version {{hub.tool.listing.update.version}}</template><template v-if="hub.tool.listing.update.version && hub.tool.listing.update.pricing"> and</template>
                    <template v-if="hub.tool.listing.update.pricing"> the price ${{hub.tool.listing.update.pricing.price_usd}}</template>. Callers keep the approved one until then.</template>
                  <template v-else><b class="warn">Your last update was rejected:</b> {{hub.tool.listing.update.reason}}{{/[.!?]$/.test(hub.tool.listing.update.reason)?'':'.'}} The approved version still serves.</template>
                </p>
                <button v-if="['none','rejected','unlisted'].includes(hubListing(hub.tool))" class="btn sm primary" :disabled="hub.flagSaving||!canRegister||hub.tool.status!=='live'" @click="setHubFlag('listed',true)">{{hubListing(hub.tool)==='rejected'?'Ask again':hubListing(hub.tool)==='unlisted'?'List it again':'Ask to list it'}}</button>
                <button v-else class="btn sm" :disabled="hub.flagSaving||!canRegister" @click="setHubFlag('listed',false)">{{hubListing(hub.tool)==='approved'?'Unlist':'Withdraw the request'}}</button>
                <p class="sub" style="margin:6px 0 0;max-width:70ch">Listed: it appears in catalog search and <code>catalog_search</code>, marked as a hub tool by your team, ranked by relevance with no boost. treg reviews each request, and each later version or price change. Not listed: only someone with the id or the share link can call it.</p>
              </div>
              <div style="margin:14px 0 4px">
                <label class="tgl" style="font-size:13.5px"><input type="checkbox" :checked="hub.tool.public_log!==false" :disabled="hub.flagSaving||!canRegister||hub.tool.status!=='live'" @change="setHubFlag('public_log',$event.target.checked)"/><span><b>Public run log on the share page</b></span></label>
                <p class="sub" style="margin:4px 0 0 21px;max-width:70ch">The last 20 runs and runs per day for 30 days: time, outcome, duration, units and the price paid. Never who called, never the inputs, never the output.</p>
              </div>
              <p class="sub">From the terminal: <code>treg hub list {{hub.tool.tool_id}}</code> · <code>treg hub unlist {{hub.tool.tool_id}}</code> · <code>treg hub log {{hub.tool.tool_id}} --public off</code></p>
            </template>

            <template v-if="hub.tab==='earnings'">
              <p v-if="!hub.earnings" class="sub">Loading…</p>
              <template v-else>
                <div class="statgrid" style="margin:12px 0;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
                  <div class="stat"><div class="n">{{money(hub.earnings.earned_micro)}}</div><div class="l">earned, 90 days</div></div>
                  <div class="stat"><div class="n">{{hub.earnings.runs}}</div><div class="l">runs by others</div></div>
                  <div class="stat"><div class="n">{{hub.earnings.by_day.reduce((a,d)=>a+d.ok,0)}} / {{hub.earnings.by_day.reduce((a,d)=>a+d.failed,0)}}</div><div class="l">ok / failed</div></div>
                </div>
                <p class="sub"><a :href="'/hub/tools/'+encodeURIComponent(hub.tool.tool_id)+'/earnings?days=90&format=csv'" target="_blank">Download CSV</a> · counts and amounts only; never who called.</p>
                <table><tr><th>Day</th><th style="text-align:right">Runs</th><th style="text-align:right">OK</th><th style="text-align:right">Failed</th><th style="text-align:right">Earned</th></tr>
                  <tr v-for="d in hub.earnings.by_day" :key="d.day"><td>{{d.day}}</td><td style="text-align:right">{{d.runs}}</td><td style="text-align:right">{{d.ok}}</td><td style="text-align:right">{{d.failed}}</td><td style="text-align:right">{{money(d.earned_micro)}}</td></tr>
                </table>
              </template>
            </template>

            <template v-if="hub.tab==='runs'">
              <p class="sub">The last runs of this version. You see the script's log lines and the full error; a caller sees only their trace.</p>
              <p v-if="!hub.health" class="sub">Loading…</p>
              <table v-else><tr><th>When</th><th>Run</th><th>Kind</th><th>Status</th><th style="text-align:right">Steps</th><th style="text-align:right">Steps cost</th><th style="text-align:right">Price</th><th style="text-align:right">ms</th></tr>
                <template v-for="r in hub.health.runs" :key="r.run_id">
                  <tr class="act-row" @click="toggleHubRun(r)"><td>{{(r.at||'').slice(5,16).replace('T',' ')}}</td><td><code>{{r.run_id.slice(0,10)}}…</code></td><td>{{r.kind}}</td><td><span :class="{ok:r.status==='ok', warn:r.status!=='ok'}">{{r.status}}</span><span v-if="r.error" class="muted"> · {{r.error}}</span></td><td style="text-align:right">{{r.steps}}</td><td style="text-align:right">{{r.cost_micro}} µ$</td><td style="text-align:right">{{money(r.price_micro)}}</td><td style="text-align:right">{{r.ms}}</td></tr>
                  <tr v-if="hub.runOpen===r.run_id"><td colspan="8">
                    <div v-if="!r.detail" class="muted">Loading…</div>
                    <div v-else style="font-size:12px;line-height:1.6">
                      <div><span class="muted">inputs</span> <code>{{JSON.stringify(r.detail.inputs)}}</code></div>
                      <div v-for="(s,i) in (r.detail.trace||[])" :key="i"><span class="muted">trace</span> wave {{s.wave}} · {{s.name}} · <code>{{s.call}}</code> · {{s.outcome}} {{s.status||''}} · {{s.cost_micro}} µ$ · {{s.key||''}} key · {{s.ms}} ms<span v-if="s.error" class="warn"> · {{String(s.error).slice(0,300)}}</span></div>
                      <div v-for="(l,i) in (r.detail.log||[])" :key="'l'+i"><span class="muted">log</span> {{l}}</div>
                      <div v-if="r.detail.error&&r.detail.error.message"><span class="muted">error</span> {{r.detail.error.error||''}} {{r.detail.error.message}}</div>
                    </div>
                  </td></tr>
                </template>
              </table>
            </template>

            <template v-if="hub.tab==='health'">
              <p v-if="!hub.health" class="sub">Loading…</p>
              <template v-else>
                <div class="card" style="max-width:640px"><b :class="{ok:hub.health.health==='ok', warn:hub.health.health==='failing'}">{{hub.health.health}}</b>
                  <span class="muted"> · {{hub.health.fails_in_a_row}} failed in a row · last run {{(hub.health.last_run_at||'-').slice(0,16).replace('T',' ')}}</span>
                  <div v-if="hub.health.last_check" class="sub" style="margin-top:6px">last check: {{hub.health.last_check.status}}<span v-if="hub.health.last_check.scheduled"> (scheduled)</span> at {{(hub.health.last_check.checked_at||'').slice(0,16).replace('T',' ')}}<span v-if="hub.health.last_check.error"> · {{hub.health.last_check.error.error||''}} {{hub.health.last_check.error.message||''}}</span></div>
                </div>
                <p class="sub" style="margin-top:10px">A tool is <b>failing</b> after 3 failed runs in a row (callers' runs or the scheduled check). It stays callable; the public page shows the state. Fix and publish a new version, or wait for the next check (<code>treg-worker hub check</code>, every 6 h).</p>
              </template>
            </template>
          </template>
</template>

<style scoped>
/* the hub's state words (live / failing / an error line), on these pages only */
.ok{color:var(--green)} .warn{color:var(--amber)} p.err{color:var(--red)}
</style>
