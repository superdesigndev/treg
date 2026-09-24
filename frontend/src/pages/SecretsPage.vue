<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>

          <div v-if="orgMsg" class="tut-notice" style="max-width:660px;margin-bottom:14px;display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
            <span>{{orgMsg}}</span><button class="btn sm ico" @click="orgMsg=''" aria-label="Dismiss">✕</button>
          </div>
          <div v-if="secretErr" class="banner err" style="max-width:660px;margin-bottom:14px;display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
            <span>{{secretErr}}</span><button class="btn sm ico" @click="secretErr=''" aria-label="Dismiss">✕</button>
          </div>
          <div class="tut-head">
            <div><h1>Bring your own keys &amp; skills</h1>
              <div class="tabs" style="margin:8px 0 4px">
                <button @click="go('tools')">Skills &amp; tools</button>
                <button class="active">Secrets</button>
                <button @click="go('resources')">Team resources</button>
              </div>
              <p class="sub" style="margin:0">The credentials your tools inject. Values are encrypted server-side and never shown.</p></div>
          </div>
          <div class="ttable-wrap" v-if="secrets.length" style="margin-bottom:16px"><table class="ttable">
            <tr v-for="s in secrets" :key="s.id">
              <td class="tn"><b>{{s.name}}</b></td>
              <td class="ta"><span class="chip" :class="s.kind">{{s.kind}}</span></td>
              <td class="th">owner: {{short(s.owner)}}</td>
              <td class="tx"><button class="btn sm ico" :class="{danger:confirmDelSecret===s.id}" @click="deleteSecret(s)" :title="confirmDelSecret===s.id?'Click again to delete':'Delete'">✕</button></td>
            </tr>
          </table></div>
          <p v-else class="sub">No secrets yet — add one below, or bulk-load everything at once (setup guide under the form).</p>
          <div class="tgh" style="margin-top:6px">Add {{secretRows.length>1?'secrets':'a secret'}}</div>
          <p class="sub" v-if="keyNameSuggestions.length" style="max-width:660px;margin:0 0 8px;font-size:12px">
            Name a key after its catalog provider — e.g. <span class="mono">{{keyNameSuggestions.slice(0,3).map(p=>p.service).join(', ')}}</span> —
            and catalog calls use it automatically. The name field suggests every provider that takes a pasted key.</p>
          <p class="sub" style="margin:2px 0 8px">Tip: paste a whole <span class="mono">.env</span> into the name field — it splits into rows automatically.</p>
          <div v-for="(row,i) in secretRows" :key="i" class="field" style="max-width:660px;flex-wrap:wrap;margin-bottom:8px">
            <input v-model="row.name" list="secret-name-suggest" placeholder="name, e.g. apollo" style="min-width:150px" @paste="pasteEnv($event,i,'name')" @keyup.enter="addSecrets"/>
            <input v-model="row.value" :type="row.kind==='param'?'text':'password'" :placeholder="row.kind==='param'?'value, e.g. a project id (not secret)':'value (encrypted server-side)'" style="min-width:190px" @paste="pasteEnv($event,i,'value')" @keyup.enter="addSecrets"/>
            <select v-model="row.kind" class="msel"><option>env</option><option>oauth</option><option>secret_file</option><option value="param">param (non-secret)</option></select>
            <button v-if="secretRows.length>1" class="btn sm ico" @click="secretRows.splice(i,1)" title="Remove row">✕</button>
            <!-- The ladder matches by NAME EQUALITY, so the hint either confirms a name the catalog
                 will find, or offers the exact one when the row is a near miss (APOLLO_API_KEY). -->
            <span v-if="secretNameHint(row)" class="sub" style="flex-basis:100%;font-size:11.5px;margin:-2px 0 0">
              <template v-if="secretNameHint(row).ok">✓ {{secretNameHint(row).text}}</template>
              <template v-else>{{secretNameHint(row).text}}
                <a href="#" @click.prevent="row.name=secretNameHint(row).service">rename to “{{secretNameHint(row).service}}”</a></template>
            </span>
          </div>
          <!-- Every provider a key can be pasted into, as native input suggestions on the name field. -->
          <datalist id="secret-name-suggest">
            <option v-for="p in keyNameSuggestions" :key="p.service" :value="p.service">{{p.display_name}}</option>
          </datalist>
          <p class="sub" v-if="secretRows.some(r=>r.kind==='param')" style="margin:2px 0 8px">A param is non-secret config (project id, org id) — injected alongside a credential into CLI env or an HTTP query.</p>
          <div class="field" style="max-width:660px">
            <button class="btn primary" @click="addSecrets" :disabled="secretBusy||!secretRowsReady">{{secretBusy?'…':(secretRowsReady>1?('Add '+secretRowsReady+' secrets'):'Add secret')}}</button>
            <button class="btn" @click="secretRows.push({name:'',value:'',kind:'env'})">＋ row</button>
            <button v-if="secretRows.length>1" class="btn" @click="secretRows=[{name:'',value:'',kind:'env'}]">Clear</button>
          </div>
          <div v-if="!secrets.length" style="margin-top:20px">
          <div style="max-width:660px;border:1px solid var(--line);border-radius:16px;padding:20px 22px;background:var(--panel2)">
            <h3 style="margin:0 0 6px;font-size:15px">Add your own keys &amp; skills</h3>
            <p class="sub" style="margin:0 0 12px">Bulk-load your keys (and skills) from your machine — each lands here encrypted, referenced by name.</p>
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
