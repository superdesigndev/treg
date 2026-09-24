<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard, mounted(){ this.loadPlatforms() } }  // the catalog size in the copy
</script>

<template>

          <div v-if="orgMsg" class="tut-notice" style="max-width:660px;margin-bottom:14px;display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
            <span>{{orgMsg}}</span><button class="btn sm ico" @click="orgMsg=''" aria-label="Dismiss">✕</button>
          </div>
          <div class="tut-head">
            <div><h1>Bring your own keys &amp; skills</h1>
              <div class="tabs" style="margin:8px 0 4px">
                <button class="active">Skills &amp; tools</button>
                <button v-if="canRegister" @click="go('secrets')">Secrets</button>
                <button @click="go('resources')">Team resources</button>
              </div>
              <p class="sub" style="margin:0" v-if="filteredTools.length">Call any of these with no key on your machine. <b>Copy</b> a snippet, or <b>Try it</b> here.</p><p class="sub" style="margin:0" v-else-if="canRegister && !q">Register an upstream API here, then call it with no key on your machine.</p></div>
            <div class="tut-actions" v-if="canRegister" style="position:relative">
              <button class="btn sm" @click="openAddSkill">＋ Skill</button>
              <button class="btn sm primary" @click.stop="addToolMenu=!addToolMenu" aria-haspopup="true" :aria-expanded="addToolMenu">＋ Add tool</button>
              <div v-if="addToolMenu" style="position:fixed;inset:0;z-index:29" @click="addToolMenu=false"></div>
              <div class="dropdown" v-if="addToolMenu" style="left:auto;right:0;top:110%;width:280px" @click.stop>
                <div class="row" @click="openAddTool('endpoint')"><span style="min-width:0"><b style="font-size:12.5px">⛁ Endpoint</b><span class="sub" style="display:block;font-size:11px;margin-top:1px">an HTTP API, called through the proxy</span></span></div>
                <div class="row" @click="openAddTool('cli')"><span style="min-width:0"><b style="font-size:12.5px">⌘ CLI</b><span class="sub" style="display:block;font-size:11px;margin-top:1px">a command members run with the key injected</span></span></div>
                <div class="row" @click="addToolMenu=false; mkTab='platform'; go('connections')"><span style="min-width:0"><b style="font-size:12.5px">▤ From the catalog</b><span class="sub" style="display:block;font-size:11px;margin-top:1px">browse ready-made endpoints by platform — many need no key</span></span></div>
              </div>
            </div>
          </div>
          <div v-if="toolErr && !newTool" class="banner" style="margin-top:12px">{{toolErr}}</div>
          <div v-if="runNote" class="banner" style="margin-top:12px;display:flex;gap:8px;align-items:flex-start">
            <span>Local runs are on for {{runNote}}. During a run the key is injected into that member's process - consider storing a <b>restricted</b> key (many providers offer read-only/scoped keys) rather than a full one.</span>
            <button class="btn sm ico" @click="runNote=''" style="margin-left:auto" aria-label="dismiss">✕</button>
          </div>

          <div class="seg" v-if="hasAnyTools">
            <button :class="{on:toolTab==='all'}" @click="toolTab='all'">All</button>
            <button :class="{on:toolTab==='endpoints'}" @click="toolTab='endpoints'">Endpoints/CLI <span>{{endpoints.length}}</span></button>
            <button :class="{on:toolTab==='skills'}" @click="toolTab='skills'">Integration Skills <span>{{skillTools.length}}</span></button>
            <button :class="{on:toolTab==='recipes'}" @click="toolTab='recipes'">Skills <span>{{recipes.length}}</span></button>
          </div>

          <div class="tgroup" v-for="grp in toolGroups" :key="grp.key" v-show="grp.rows.length && (toolTab==='all'||toolTab===grp.key)">
            <div class="tgh">{{grp.label}} <span class="tgh-n">{{grp.rows.length}}</span><span class="tgh-hint">{{grp.hint}}</span></div>
            <div class="ttable-wrap"><table class="ttable">
              <tr v-for="t in grp.rows" :key="t.id" @click="rowOpen(t)" style="cursor:pointer" :title="'Open the '+rowTarget(t).kind+'’s page (shareable link)'">
                <td class="tn"><a :href="rowHref(t)" @click.prevent.stop="rowOpen(t)" style="color:inherit;text-decoration:none"><b>{{t.name}}</b></a></td>
                <td class="th"><span class="mono">{{t.host}}</span></td>
                <td class="ta"><span v-if="t.cli" class="chip run" :title="localRunTitle(t)">⌘ run{{t.cli.enabled?'':' off'}}</span><span v-if="t.server_runnable" class="chip ok" title="Runs on the server — the key is injected there, never on a member's machine">server</span><span v-else-if="t.cli" class="chip" title="Local only — this CLI authenticates from the member's own machine (treg run --local)">local-only</span><span class="chip" :class="w.kind" v-for="(w,wi) in credChips(t)" :key="'w'+wi" :title="w.title">{{w.label}}</span></td>
                <td class="tx" @click.stop><button v-if="t.cli && canRegister" class="btn sm ico" :class="{on:t.cli.enabled}" @click="toggleLocalRun(t)" :title="localRunTitle(t)">⌘</button><button class="btn sm ico" @click="openCopy(t)" title="Copy a call snippet">⧉</button><button class="btn sm ico" @click="openUse(t)" title="Use it - API call or CLI run">▶</button><button v-if="canRegister" class="btn sm ico" @click="openEditTool(t)" title="Edit">✎</button><button v-if="canRegister" class="btn sm ico" :class="{danger:confirmDelTool===t.id}" @click="deleteTool(t)" :title="confirmDelTool===t.id?'Click again to delete':'Delete'">✕</button></td>
              </tr>
            </table></div>
          </div>

          <div class="tgroup" v-show="recipes.length && (toolTab==='all'||toolTab==='recipes')">
            <div class="tgh">Skills <span class="tgh-n">{{recipes.length}}</span><span class="tgh-hint">knowledge skills - pull with the CLI</span></div>
            <div class="ttable-wrap"><table class="ttable">
              <tr v-for="r in recipes" :key="r.id" @click="openDetail('skill',r.name)" style="cursor:pointer" title="Open the skill’s page (shareable link)">
                <td class="tn"><a :href="'/app/skills/'+encodeURIComponent(r.name)" @click.prevent.stop="openDetail('skill',r.name)" style="color:inherit;text-decoration:none"><b>{{r.name}}</b></a></td>
                <td class="th" colspan="2"><span class="mono muted">treg skill install {{r.name}}</span></td>
                <td class="tx" @click.stop><button class="btn sm ico" @click="openRecipeCopy(r)" title="How to install / use">⧉</button><button class="btn sm ico" @click="openRecipeView(r)" title="View the SKILL.md">✎</button><button v-if="canRegister" class="btn sm ico" :class="{danger:confirmDelBundle===r.id}" @click="deleteRecipe(r)" :title="confirmDelBundle===r.id?'Click again to delete':'Delete'">✕</button></td>
              </tr>
            </table></div>
          </div>

          <p v-if="!loading && !hasAnyTools" class="sub">Nothing here{{q?' matches "'+q+'"':' yet'}}.<template v-if="!q && isPersonal(activeOrg) && myOrgs.some(o=>!isPersonal(o))"> This is your <b>personal</b> space - your team's tools live under another org. <a href="#" @click.prevent="jumpToTeam()">Switch to a team →</a></template></p>
          <div v-if="!loading && !hasAnyTools && !q && canRegister" style="margin-top:14px">
          <div style="max-width:660px;border:1px solid var(--line);border-radius:16px;padding:20px 22px;background:var(--panel2)">
            <h3 style="margin:0 0 6px;font-size:15px">Nothing of your own yet — but you can already call {{toolCountText||'thousands of'}} tools</h3>
            <p class="sub" style="margin:0 0 12px">New verified accounts get <b>$1.00 of free credit once</b> when creating an eligible team, so the catalog works before you register anything: find a tool by what it does, see the price, call it. <a href="#" @click.prevent="go('connections')">Browse the catalog →</a><br>When you're ready to add your <i>own</i> keys and skills — your <span class="mono">.env</span> and skill folders — load them here and they become callable tools for the whole team.</p>
            <div class="seg" style="margin-bottom:10px">
              <button :class="{on:emptyTab==='agent'}" @click="emptyTab='agent'">Agent instruction</button>
              <button :class="{on:emptyTab==='manual'}" @click="emptyTab='manual'">Manual</button>
            </div>
            <template v-if="emptyTab==='manual'">
              <div class="lbl">1 · Install the CLI &amp; sign in</div>
              <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('curl -fsSL '+proxy+'/install.sh | sh\ntreg login','es1')">{{startCopied==='es1'?'✓ copied':'copy'}}</button><pre><span class="hl-cmd">curl</span> <span class="hl-flag">-fsSL</span> <span class="hl-str">{{proxy}}/install.sh</span> | sh
<span class="hl-cmd">treg</span> login</pre></div>
              <div class="lbl" style="margin-top:12px">2 · Preview, then upload — it scans your <span class="mono">.env</span> + skill folders, you pick what to share.</div>
              <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('treg scan\ntreg upload --all','es3')">{{startCopied==='es3'?'✓ copied':'copy'}}</button><pre><span class="hl-comment"># preview what's here (read-only)</span>
<span class="hl-cmd">treg</span> scan
<span class="hl-comment"># register keys + skills</span>
<span class="hl-cmd">treg</span> upload <span class="hl-flag">--all</span></pre></div>
            </template>
            <template v-else>
              <p class="sub" style="margin:0 0 8px">One line, token included — your agent reads llms.txt, installs the CLI, signs in, and registers your skills + keys (read-only scan first, you approve).</p>
              <div class="lc-codewrap"><button class="lc-cp" @click="copyAgentGuide('agent')">{{agentRowCopied==='agent'?'✓ copied':'copy'}}</button><pre style="max-height:220px;overflow:auto">{{buildAgentPrompt('agent', true)}}</pre></div>
            </template>
          </div>
          </div>

</template>
