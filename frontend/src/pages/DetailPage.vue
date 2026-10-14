<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>

          <div class="tut-head">
            <div>
              <p class="sub" style="margin:0 0 4px"><a href="/app#tools" @click.prevent="go('tools')">← Tools</a></p>
              <h1 style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">{{detail.name}}
                <span class="chip">{{detail.kind==='skill' ? 'skill' : (detailData&&detailData.cli ? 'endpoint + CLI' : 'endpoint')}}</span>
                <span v-if="detailData&&detailData.owner" class="chip" :title="'shared by '+detailData.owner">{{detailData.owner}}</span>
              </h1>
            </div>
            <div class="tut-actions" style="position:relative">
              <template v-if="detailData && detail.kind==='skill' && (detailData.tools||[]).length">
                <button class="btn sm" @click.stop="(detailData.tools.length===1 ? tryDetailTool(detailData.tools[0]) : tryMenu=!tryMenu)" title="Call one of this skill's tools right here — the key stays server-side">▶ Try it</button>
                <div v-if="tryMenu" style="position:fixed;inset:0;z-index:29" @click="tryMenu=false"></div>
                <div class="dropdown" v-if="tryMenu" style="left:auto;right:0;top:110%;width:260px" @click.stop>
                  <div class="row" v-for="t in detailData.tools" :key="'try'+t.id" @click="tryDetailTool(t)"><span style="min-width:0"><b style="font-size:12.5px">⚒ {{t.name}}</b><span class="sub" style="display:block;font-size:11px;margin-top:1px">{{t.host||t.base_url}}</span></span></div>
                </div>
              </template>
              <template v-if="canRegister && detailData">
                <button v-if="detail.kind==='tool'" class="btn sm" @click="configureTool(detailData)" :title="'Edit this '+(detailData.cli?'CLI':'endpoint')+' — credentials, '+(detailData.cli?'deny list, local runs':'bindings')">⚙ Configure</button>
                <template v-else-if="(detailData.tools||[]).length">
                  <button class="btn sm" @click.stop="(detailData.tools.length===1 ? configureTool(detailData.tools[0]) : cfgMenu=!cfgMenu)" title="Edit this skill's tools — credentials, deny lists, local runs">⚙ Configure</button>
                  <div v-if="cfgMenu" style="position:fixed;inset:0;z-index:29" @click="cfgMenu=false"></div>
                  <div class="dropdown" v-if="cfgMenu" style="left:auto;right:0;top:110%;width:260px" @click.stop>
                    <div class="row" v-for="t in detailData.tools" :key="'cfg'+t.id" @click="configureTool(t)"><span style="min-width:0"><b style="font-size:12.5px">⚒ {{t.name}}</b><span class="sub" style="display:block;font-size:11px;margin-top:1px">{{t.host||t.base_url}}</span></span></div>
                  </div>
                </template>
              </template>
              <button class="btn sm" @click="copyDetail(detailShareUrl,'url')" title="Copy this page's URL — anyone on the team can open it">{{detailCopied==='url'?'✓ copied':'⧉ Copy link'}}</button>
              <button v-if="canAdmin && detailData" class="btn sm primary" @click="openShare" title="Invite someone outside the team — they'll land right on this page">Share…</button>
            </div>
          </div>
          <div v-if="detailErr" class="banner" style="margin-top:12px">{{detailErr}}</div>
          <div v-if="detailNote" class="tut-notice" style="margin-top:12px;max-width:860px">{{detailNote}}</div>
          <p v-if="detailLoading" class="sub">Loading…</p>

          <template v-if="detailData && detail.kind==='skill'">
            <div style="max-width:860px;border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin:14px 0;background:var(--panel)">
              <h3 style="margin:0 0 6px;font-size:14px">Use with your agent</h3>
              <p class="sub" style="margin:0 0 10px">Send someone this page's link, or paste this into a coding agent (Claude Code / Codex / Gemini) — it installs the CLI, signs them in as themselves, and pulls just this skill. <template v-if="(detailData.secrets||[]).length">Its credential{{detailData.secrets.length>1?'s stay':' stays'}} server-side — calls go through the proxy, no key lands on their machine.</template></p>
              <pre class="code" style="white-space:pre-wrap;max-height:34vh;overflow:auto">{{detailPrompt}}</pre>
              <div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                <button class="btn primary" @click="copyDetail(detailPrompt,'prompt')">⧉ {{detailCopied==='prompt'?'Copied!':'Copy prompt'}}</button>
                <button class="btn" @click="copyDetail('treg skill install '+detail.name,'cli')" title="Just the CLI command, if treg is already set up">{{detailCopied==='cli'?'✓ copied':'treg skill install '+detail.name}}</button>
              </div>
            </div>
            <div v-if="(detailData.tools||[]).length || (detailData.secrets||[]).length" style="max-width:860px;margin:0 0 14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
              <span class="sub" style="margin:0">Bundled:</span>
              <a v-for="t in detailData.tools" :key="'dt'+t.id" class="chip" :href="'/app/tools/'+encodeURIComponent(t.name)" @click.prevent="openDetail('tool',t.name)" style="cursor:pointer" :title="'tool · '+(t.host||t.base_url)">⚒ {{t.name}}</a>
              <span v-for="s in detailData.secrets" :key="'ds'+s.id" class="chip" title="credential — stored encrypted server-side, injected by the proxy, never shown">🔒 {{s.name||s.local_name}}</span>
            </div>
            <div style="max-width:860px;border:1px solid var(--line);border-radius:16px;overflow:hidden;display:flex;min-height:280px;background:var(--panel)">
              <div style="width:220px;flex:none;border-right:1px solid var(--line);padding:10px 0;overflow:auto;max-height:60vh">
                <div v-for="row in detailTree" :key="row.path"
                     :style="{padding:'4px 12px 4px '+(14+row.depth*14)+'px', cursor:row.dir?'default':'pointer', fontSize:'12.5px',
                              color:row.dir?'var(--muted)':'var(--ink)', background:(!row.dir&&detailFile===row.path)?'var(--panel2)':'transparent'}"
                     @click="!row.dir && (detailFile=row.path)">{{row.dir?'▸ ':'· '}}{{row.name}}</div>
              </div>
              <pre class="code" style="flex:1;margin:0;border:0;border-radius:0;white-space:pre-wrap;word-break:break-word;overflow:auto;max-height:60vh">{{detailFileContent}}</pre>
            </div>
          </template>

          <template v-if="detailData && detail.kind==='tool'">
            <div style="max-width:860px;border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin:14px 0;background:var(--panel)">
              <h3 style="margin:0 0 6px;font-size:14px">Use with your agent</h3>
              <p class="sub" style="margin:0 0 10px">Send someone this page's link, or paste this into a coding agent. Calls go through the proxy — the credential is injected server-side, never on their machine.</p>
              <pre class="code" style="white-space:pre-wrap;max-height:34vh;overflow:auto">{{detailPrompt}}</pre>
              <div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                <button class="btn primary" @click="copyDetail(detailPrompt,'prompt')">⧉ {{detailCopied==='prompt'?'Copied!':'Copy prompt'}}</button>
                <button class="btn" @click="openCopy(detailData)" title="Language-specific call snippets (cURL / Python / Node / …)">⧉ Call snippets</button>
                <button class="btn" @click="openUse(detailData)" title="Try it right here">▶ Try it</button>
              </div>
            </div>
            <div style="max-width:860px;border:1px solid var(--line);border-radius:16px;padding:16px 18px;background:var(--panel)">
              <div class="lbl">Upstream</div>
              <p style="margin:2px 0 12px"><span class="mono">{{detailData.base_url}}</span></p>
              <div class="lbl">Credentials</div>
              <p class="sub" style="margin:2px 0 12px" v-if="(detailData.bindings||[]).length || (detailData.cli&&(detailData.cli.inject||[]).length)"><span class="chip" v-for="(w,wi) in credChips(detailData)" :key="'dw'+wi" :class="w.kind" :title="w.title">{{w.label}}</span> injected server-side on every call — the value never appears here or on a caller's machine.</p>
              <p class="sub" style="margin:2px 0 12px" v-else>none — a public upstream, no credential needed.</p>
              <template v-if="(detailData.examples||[]).length">
                <div class="lbl">Examples</div>
                <div class="exrow" style="margin:4px 0 12px"><span v-for="(ex,exi) in detailData.examples" :key="'de'+exi" class="exchip"><span class="m">{{ex.method||'GET'}}</span>{{ex.note||ex.path}}</span></div>
              </template>
              <template v-if="detailData.cli">
                <div class="lbl">CLI</div>
                <p class="sub" style="margin:2px 0 12px"><span class="mono">treg run {{detail.name}} -- &lt;args&gt;</span>
                  <span class="chip ok" v-if="detailData.server_runnable" title="Runs on the server — the key is injected there, never on a member's machine">server</span>
                  <span class="chip" v-else title="Local only — this CLI authenticates from the member's own machine (treg run --local)">local-only</span>
                  <span class="chip run" v-if="!detailData.cli.enabled" title="The owner has local runs switched off for this tool">⌘ run off</span>
                </p>
                <template v-if="(detailData.cli.deny||[]).length">
                  <div class="lbl">Guardrails</div>
                  <p class="sub" style="margin:2px 0 12px">runs matching these patterns are refused before the key is injected: <span class="mono" v-for="(p,pi) in detailData.cli.deny" :key="'dg'+pi" style="margin-right:8px">{{p}}</span></p>
                </template>
              </template>
              <template v-if="detailParentSkill">
                <div class="lbl">From skill</div>
                <p style="margin:2px 0 0"><a :href="'/app/skills/'+encodeURIComponent(detailParentSkill.name)" @click.prevent="openDetail('skill',detailParentSkill.name)">▚ {{detailParentSkill.name}}</a> — the recipe + files that registered this tool.</p>
              </template>
            </div>
          </template>

</template>
