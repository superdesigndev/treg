<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="closeEpTry" style="place-items:stretch;justify-items:end">
      <div class="drawer"><div class="hd" style="padding:15px 18px;border-bottom:1px solid var(--line)"><b>Try “{{epTry.id}}”</b><button class="btn sm" @click="closeEpTry" aria-label="Close">✕</button></div>
        <div class="bd" style="padding:16px 18px;overflow:auto">
          <p class="explain"><span class="mono">{{epTry.method||'GET'}} {{epTryDisplayPath}}</span><br>{{epTry.summary}}</p>
          <p class="sub" v-if="epTryAccess" style="margin:0 0 12px">
            <template v-if="epTryAccess.tier==='anonymous'">◎ {{epTryAccess.detail||'No provider key needed — the verified public upstream route is free.'}}</template>
            <template v-else-if="epTryAccess.tier==='platform'">⚡ {{epTryAccess.detail||'No key needed — served on treg\'s key, billed to the team balance.'}}</template>
            <template v-else-if="epTryAccess.tier==='tool'||epTryAccess.tier==='credential'">🔑 Served with your team's own {{epTry.provider_display||epTry.provider}} credential — billed by the provider, not the team balance.</template>
            <template v-else>{{epTryAccess.detail||'Not callable from this team yet.'}}</template>
          </p>

          <div v-if="epTryShowAuthSelector" class="field auth-method-field" style="max-width:520px;margin-bottom:16px">
            <label>Authorization</label>
            <select v-model="epTryAuthMethod" @change="loadEpTryAccess">
              <option v-for="m in epTryAuthMethods" :key="m" :value="m">{{authorizationMethodLabel(epTry.provider,m)}}</option>
            </select>
          </div>

          <div class="seg" style="margin-bottom:16px">
            <button :class="{on:epTryTab==='agent'}" @click="epTryTab='agent'">AI Agent</button>
            <button :class="{on:epTryTab==='cli'}" @click="epTryTab='cli'">CLI</button>
            <button :class="{on:epTryTab==='api'}" @click="epTryTab='api'">API</button>
            <button :class="{on:epTryTab==='manual'}" @click="epTryTab='manual'">Manual</button>
          </div>

          <!-- AI AGENT — one-line setup (token embedded here only), then the prompt to run this endpoint -->
          <template v-if="epTryTab==='agent'">
            <div class="lbl">1 · Set up with your agent</div>
            <p class="sub" style="margin:4px 0 8px">One line — the agent reads llms.txt, installs the CLI and signs in as you. Token &amp; team are baked in.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart(epTrySetupLine,'ep-setup')">{{startCopied==='ep-setup'?'✓ copied':'copy'}}</button><pre style="white-space:pre-wrap;word-break:break-all">{{epTrySetupLine}}</pre></div>
            <div class="lbl" style="margin-top:20px">2 · Ask your agent to use this</div>
            <p class="sub" style="margin:4px 0 8px">Paste this next — the agent calls the endpoint through treg, no key on its machine.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart(epTryAgentUse,'ep-use')">{{startCopied==='ep-use'?'✓ copied':'copy'}}</button><pre style="white-space:pre-wrap">{{epTryAgentUse}}</pre></div>
          </template>

          <!-- CLI — install, sign in, inspect, run this exact endpoint -->
          <template v-else-if="epTryTab==='cli'">
            <div class="lbl">1 · Install the CLI &amp; sign in</div>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('curl -fsSL '+proxy+'/install.sh | sh\ntreg login','ep-cli1')">{{startCopied==='ep-cli1'?'✓ copied':'copy'}}</button><pre>curl -fsSL {{proxy}}/install.sh | sh
treg login</pre></div>
            <div class="lbl" style="margin-top:18px">2 · See its params &amp; price</div>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart('treg catalog get '+epTry.id,'ep-cli2')">{{startCopied==='ep-cli2'?'✓ copied':'copy'}}</button><pre>treg catalog get {{epTry.id}}</pre></div>
            <div class="lbl" style="margin-top:18px">3 · Call it — {{epTry.id==='fishaudio.voices.list'?'BYOK or platform is selected server-side':(epTryAccess&&epTryAccess.tier==='anonymous'?'no provider key is needed':'the key is injected server-side')}}</div>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart(epTryCliCall,'ep-cli3')">{{startCopied==='ep-cli3'?'✓ copied':'copy'}}</button><pre style="white-space:pre-wrap;word-break:break-all">{{epTryCliCall}}</pre></div>
          </template>

          <!-- API — the raw /call/ passthrough with the token header -->
          <template v-else-if="epTryTab==='api'">
            <p v-if="epTry.id==='fishaudio.voices.list'" class="sub" style="margin:0 0 8px">One endpoint, one token. The server returns normalized voice rows from the connected Fish account under BYOK, or from this team's organization-owned voices otherwise.</p>
            <p v-else class="sub" style="margin:0 0 8px">One endpoint, one token. {{epTryAccess&&epTryAccess.tier==='anonymous'?'This verified public upstream route needs no provider key.':'treg injects the credential server-side.'}} treg relays the response verbatim.</p>
            <div class="lc-codewrap"><button class="lc-cp" @click="copyStart(epTryCurl,'ep-api')">{{startCopied==='ep-api'?'✓ copied':'copy'}}</button><pre style="white-space:pre-wrap;word-break:break-all">{{epTryCurl}}</pre></div>
            <p class="sub" v-if="!myToken" style="margin:8px 0 0;font-size:12px"><span class="mono">$TREG_TOKEN</span> is a placeholder — set it to your API token (copy it on Getting started).</p>
          </template>

          <!-- MANUAL — the live test form -->
          <template v-else>
            <!-- Not callable from here (no key connected / no treg price): don't strand the user on a
                 dead Run button — point them at the tab that DOES work. -->
            <div v-if="epTryAccess && epTryAccess.tier!=='anonymous' && epTryAccess.tier!=='platform' && epTryAccess.tier!=='tool' && epTryAccess.tier!=='credential'"
                 class="banner" style="margin:0">
              {{epTryAccess.missing_message || (mkOauth(epTry.provider) ? 'Can\'t run this here yet — connect '+(epTry.provider_display||epTry.provider)+' first, then Run.' : 'This endpoint needs a key. Use the AI Agent / CLI / API tab, or bring your own.')}}
              <div style="margin-top:10px">
                <button v-if="mkOauth(epTry.provider)" class="btn sm primary" @click="openProvider(epTry.provider); closeEpTry()">{{epTryAccess.action_label||endpointConnectLabel(epTry)}}</button>
                <button v-else-if="mkKnown(epTry.provider)" class="btn sm primary" @click="goByok(epTry.provider)">🔑 Bring your own key</button>
              </div>
            </div>
            <template v-else>
              <div class="field" v-for="p in epTryVisibleParams" :key="p.name" style="max-width:520px">
                <label class="mono" style="min-width:140px">{{p.name}} <span class="muted">({{p.location}})</span><span v-if="p.required" style="color:var(--accent)"> *</span></label>
                <input v-model="p.value" :placeholder="p.required?'required':'optional'"/>
              </div>
              <p v-if="!epTryVisibleParams.length && (epTry.method||'GET')==='GET'" class="sub" style="margin:0 0 12px">No parameters — just run it.</p>
              <template v-if="epTryBodyType==='multipart'">
                <div class="field" v-for="p in epTryMultipart" :key="p.name" style="max-width:520px">
                  <label class="mono" style="min-width:140px">{{p.name}}<span v-if="p.required" style="color:var(--accent)"> *</span></label>
                  <input v-if="String(p.type).includes('file')" type="file" :multiple="String(p.type).startsWith('array')" @change="setEpTryFiles(p.name,$event)"/>
                  <input v-else v-model="p.value" :placeholder="p.required?'required':'optional'"/>
                </div>
              </template>
              <div v-else-if="(epTry.method||'GET')!=='GET' && epTryBody!==''" class="field" style="max-width:520px;align-items:flex-start">
                <label style="min-width:140px">Body (JSON)</label>
                <textarea v-model="epTryBody" rows="6" style="width:100%;font-family:var(--mono);font-size:12px"></textarea>
              </div>
              <button class="btn primary" :disabled="epTryBusy" @click="runEpTry">{{epTryBusy?'Running…':'❯ Run'}}</button>
            </template>
            <div v-if="epTryResp" style="margin-top:14px">
              <div class="muted" style="font-size:12px;margin-bottom:6px">Result
                <span class="badge" :class="epTryStatus>=200&&epTryStatus<300?'ok':'invalid'">{{epTryStatus}}</span>
                · {{epTryMs}}ms<span v-if="epTryCost!=null"> · charged {{money(epTryCost)}}<span v-if="epTryCost===0 && epTryStatus>=200 && epTryStatus<300" :title="epTryAccess&&epTryAccess.tier==='anonymous'?'This verified public upstream route used no provider key and did not charge the team balance.':'The provider reported charging nothing for this call — usually its own cache serving a repeat lookup (look for cached: true in the response). You are billed exactly what the provider billed treg.'"> {{epTryAccess&&epTryAccess.tier==='anonymous'?'(free — no provider key ⓘ)':'(free — provider charged 0 ⓘ)'}}</span></span>
                <template v-if="epTry.id!=='fishaudio.voices.list'"> · logged in Activity like any call</template></div>
              <audio v-if="epTryAudioUrl" :src="epTryAudioUrl" controls style="width:100%;margin:8px 0"></audio>
              <a v-if="epTryAudioUrl" class="btn sm" :href="epTryAudioUrl" :download="epTryAudioName">Download audio</a>
              <pre v-if="!epTryAudioUrl" style="max-height:340px;overflow:auto">{{epTryResp}}</pre>
            </div>
            <div v-if="epTry.provider==='fishaudio'" style="margin-top:20px;border-top:1px solid var(--line);padding-top:14px">
              <div style="display:flex;justify-content:space-between;align-items:center"><b>{{fishVoicesSource==='byok'?'Your Fish Audio voices':'Team voices'}}</b><button class="btn sm" @click="loadFishVoices">Refresh</button></div>
              <p v-if="fishVoiceNote" class="sub" style="margin:6px 0 0">{{fishVoiceNote}}</p>
              <p v-if="!fishVoices.length && !fishVoiceBusy" class="sub">{{fishVoicesSource==='byok'?'No voices in this Fish Audio account yet.':'No platform-created voices in this team yet.'}}</p>
              <p v-if="fishVoiceBusy" class="sub">Loading voices…</p>
              <div v-for="v in fishVoices" :key="v.id" class="card" style="padding:10px;margin-top:8px">
                <div><b>{{v.display_name||'Untitled voice'}}</b><div class="mono muted" style="font-size:11px">{{v.upstream_id}}</div></div>
                <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
                  <button class="btn sm" @click="useFishVoice(v)">Use in TTS</button>
                  <button class="btn sm" @click="beginRenameFishVoice(v)">Rename</button>
                  <button class="btn sm danger" @click="beginDeleteFishVoice(v)">Delete</button>
                </div>
              </div>
            </div>
          </template>
        </div></div>
    </div>
</template>
