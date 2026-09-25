<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>

          <h1>Activity - {{activeName}}</h1>
          <!-- One page, two readings of the same traffic: the RAW FEED (every call, newest first)
               and the ROLLUPS (who/what/when, and spend per caller tag). They were separate sidebar
               entries, which made you leave one to answer a question about the other. -->
          <div class="tabs" style="margin:10px 0 4px">
            <button :class="{active:actTab==='feed'}" @click="actTab='feed'">Activity</button>
            <button v-if="canAdmin" :class="{active:actTab==='usage'}" @click="actTab='usage'; loadUsage()">Usage</button>
            <button v-if="actTab==='feed' && activityRows.length" class="act-toggle" @click="actOkOnly=!actOkOnly" :title="actOkOnly?'Include failed and refused calls':'Hide failed and refused calls'">{{actOkOnly?'Show all ('+activityRows.length+')':'Successes only'}}</button>
          </div>

          <template v-if="actTab==='feed'">
          <p class="sub">{{actOkOnly?'Successful calls and runs in this org.':'Every proxy call and server CLI run in this org.'}}</p>
          <p v-if="activityCachedCount" class="sub">{{activityCachedCount}} of {{activityCallCount}} loaded {{activityCallCount===1?'call was':'calls were'}} served from the archive.</p>
          <div class="field" style="max-width:460px;margin-bottom:10px"><select class="msel" v-model="activityKey" @change="loadCalls"><option value="">All API keys</option><option v-for="k in apiKeys" :key="k.id" :value="String(k.id)">{{k.identity}} — {{k.name}}</option></select></div>
          <table><tr><th>When</th><th>Who</th><th>Key</th><th v-if="anyTagged">Tagged</th><th>Tool</th><th>Action</th><th>Status</th><th style="text-align:right">Cost</th></tr>
            <tr v-for="a in activityShown" :key="a.kind+'-'+a.id" :class="{'act-row':a.kind==='call'}" @click="a.kind==='call'&&openCall(a)" :title="a.kind==='call'?(a.has_result?'Show request and response':'Show call details'):''"><td class="muted">{{when(a.created_at)}}</td><td>{{activityWho(a)}}<span v-if="activityOwner(a)" class="chip" style="margin-left:6px" :title="'Agent owner: '+activityAgentKey(a).created_by">owner: {{activityOwner(a)}}</span><span v-if="a.client && a.client!=='cli'" class="chip" style="margin-left:6px" :title="'reported by the runtime — attribution, not authentication'">via {{a.client}}</span></td><td><span v-if="a.api_key_name" class="chip">{{a.api_key_name}}<span v-if="a.api_key_prefix" class="muted mono"> · {{a.api_key_prefix}}</span></span><span v-else class="muted">—</span></td><td v-if="anyTagged"><template v-if="a.tags"><span v-for="(v,k) in a.tags" :key="k" class="chip" style="margin-right:4px" :title="'X-Treg-Meta '+k+'='+v">{{k}}={{v}}</span></template><span v-else class="muted">—</span></td><td>{{a.tool}}</td><td><span v-if="a.kind==='run'" class="chip" style="margin-right:6px" :title="a.where==='local'?'ran on this member\'s machine':'ran on the registry server'">{{a.where||'run'}}</span>{{a.action}}<!-- A generation task's artifact, once it succeeded: the provider's time-limited URL, or the CLI command that retrieves it (treg never downloads media). --><template v-if="a.task"><a v-if="a.task.result_url" :href="a.task.result_url" target="_blank" rel="noopener" style="margin-left:8px" :title="taskArtifactTitle(a.task)" @click.stop>result ↗</a><span v-else-if="a.task.fetch_command" class="chip" style="margin-left:8px" :title="'retrieve it from the CLI: '+a.task.fetch_command">result via CLI</span><span v-if="a.task.result_url||a.task.fetch_command" class="muted" style="margin-left:6px;font-size:.85em">{{a.task.ttl_note?'expires in '+a.task.ttl_note:'time-limited link'}}</span></template></td><td><span class="badge" :class="a.ok?'ok':'invalid'">{{a.status}}</span><span v-if="a.task" class="chip" style="margin-left:6px" :title="taskStateTitle(a.task)">{{taskStateLabel(a.task)}}</span><span v-if="a.has_result" class="act-view">result ›</span></td><td style="text-align:right;white-space:nowrap" class="muted" :title="a.held?'reserved - settles when the task finishes, refunded if it fails':(a.tier==='platform'?'charged to team balance':(a.cost!=null?'estimated — billed to your own provider key':''))"><span v-if="a.held" style="font-size:.85em">hold </span>{{a.cost!=null?money(a.cost):'—'}}<span v-if="a.cached" class="chip cached" style="margin-left:6px" title="Served from treg's archive instead of calling the provider.">Cached</span></td></tr></table>
          <p v-if="callsLoaded && !activityRows.length" class="sub">No activity yet.</p>
          <p v-else-if="callsLoaded && !activityShown.length" class="sub">No successful calls yet — <a href="#" @click.prevent="actOkOnly=false">show all {{activityRows.length}}</a>.</p>
          </template>

          <template v-if="canAdmin && actTab==='usage'">
          <div class="tut-head" style="align-items:flex-end">
            <div><p class="sub" style="margin:0">Calls &amp; runs per member over the last {{usageDays}} days. Counts only - no request or response content is stored.</p></div>
            <select class="msel" :value="usageDays" @change="usageDays=+$event.target.value; loadUsage()"><option :value="7">7 days</option><option :value="30">30 days</option><option :value="90">90 days</option></select>
          </div>

          <div v-if="usage" style="margin-top:16px">
            <div class="statgrid">
              <div class="stat"><div class="n">{{usage.totals.total}}</div><div class="l">total events</div></div>
              <div class="stat"><div class="n">{{usage.totals.call}}</div><div class="l">API calls</div></div>
              <div class="stat"><div class="n">{{usage.totals.local_run}}</div><div class="l">local runs</div></div>
              <div class="stat"><div class="n">{{usage.totals.server_run}}</div><div class="l">server runs</div></div>
            </div>
            <div class="lbl" style="margin-top:20px">By member</div>
            <table v-if="usage.by_user.length">
              <tr><th>Member</th><th style="text-align:right">API</th><th style="text-align:right">Local</th><th style="text-align:right">Server</th><th style="text-align:right">Total</th></tr>
              <tr v-for="u in usage.by_user" :key="u.user_email">
                <td>{{short(u.user_email)}}</td><td style="text-align:right" class="muted">{{u.call}}</td>
                <td style="text-align:right" class="muted">{{u.local_run}}</td><td style="text-align:right" class="muted">{{u.server_run}}</td>
                <td style="text-align:right"><b>{{u.total}}</b></td>
              </tr>
            </table>
            <p v-else class="sub">No usage in this window.</p>
            <div v-if="usage.by_tool.length" class="lbl" style="margin-top:20px">Top tools</div>
            <table v-if="usage.by_tool.length">
              <tr><th>Tool</th><th style="text-align:right">Events</th></tr>
              <tr v-for="t in usage.by_tool" :key="t.name"><td>{{t.name}}</td><td style="text-align:right">{{t.total}}</td></tr>
            </table>
            <div v-if="usage.by_day.length" class="lbl" style="margin-top:20px">Per day</div>
            <table v-if="usage.by_day.length">
              <tr><th>Day</th><th style="text-align:right">Events</th></tr>
              <tr v-for="d in usage.by_day" :key="d.day"><td class="muted">{{d.day}}</td><td style="text-align:right">{{d.total}}</td></tr>
            </table>
          <!-- SPEND BY CALLER TAG — one section per key the team has ACTUALLY SENT, not a picker.
               A dropdown hides the other dimensions behind an interaction, and the whole point of
               tagging by both customer and workspace is seeing them side by side. The list comes from
               /tag-keys (observed), so a team that never tags sees nothing here at all.

               Money here comes from the LEDGER, unlike the per-member/tool/day counts below, which
               come from the audit table and are allowed to lose rows. This is what a reselling team
               invoices from, so it has to be complete. -->
          <template v-for="key in tagKeys" :key="key">
            <div v-if="tagUsage[key]" style="margin-top:20px">
              <div class="lbl" style="margin:0">By {{key}} <span class="chip" style="margin-left:6px" title="a caller tag from X-Treg-Meta">X-Treg-Meta</span></div>
              <p class="sub" style="margin:4px 0 8px;font-size:12px">
                What each <b>{{key}}</b> spent, from the ledger. A call is credited in full to every
                tag it carries, so these sections are different slices of the same money — never add
                two of them together.
              </p>
              <table v-if="tagUsage[key].rows.length">
                <tr><th>{{key}}</th><th style="text-align:right">Calls</th><th style="text-align:right">Spend</th></tr>
                <tr v-for="r in tagUsage[key].rows" :key="r.value">
                  <td><span class="chip">{{r.value}}</span></td>
                  <td style="text-align:right" class="muted">{{r.calls}}</td>
                  <td style="text-align:right"><b>{{money(r.charged_micro)}}</b></td>
                </tr>
                <tr v-if="tagUsage[key].unattributed_micro">
                  <td class="muted" title="calls carrying no value for this tag — shown, never dropped">untagged</td>
                  <td style="text-align:right" class="muted">—</td>
                  <td style="text-align:right" class="muted">{{money(tagUsage[key].unattributed_micro)}}</td>
                </tr>
              </table>
              <p v-else class="sub">Nothing tagged <b>{{key}}</b> in this window.</p>
            </div>
          </template>

          </div>
          <p v-else class="sub">Loading…</p>
          </template>

</template>
