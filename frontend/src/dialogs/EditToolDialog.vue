<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="newTool=false;toolErr=''">
      <div class="modal" style="width:min(620px,95vw)"><div class="hd"><b>{{(tForm.id?'Edit ':'Add ')+(tForm.mode==='cli'?'CLI':'endpoint')}}</b><button class="btn sm" @click="newTool=false;toolErr=''" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px">
          <template v-if="tForm.mode!=='cli'">
            <p class="explain">An endpoint = an upstream base URL + one or more credential bindings (how treg injects each key). Need a key? Close this and open <b>⚿ Secrets</b> first.</p>
            <div class="frow"><label>Name</label><input v-model="tForm.name" :disabled="!!tForm.id" placeholder="e.g. openai"/></div>
            <div class="frow"><label>Base URL</label><input v-model="tForm.base_url" placeholder="https://api.openai.com"/></div>
            <div class="lbl" style="margin-top:12px;display:flex;align-items:center">Bindings - how each key is injected <button class="btn sm" @click="addBinding" style="margin-left:auto">＋ binding</button></div>
            <div class="bindrow" v-for="(b,i) in tForm.bindings" :key="i">
              <select v-model="b.secret_id" class="msel">
                <option v-if="!secrets.length" :value="null" disabled>- add a secret -</option>
                <option v-for="s in secrets" :key="s.id" :value="s.id">{{s.name}}</option>
              </select>
              <select v-model="b.injector" class="msel"><option>env</option><option>oauth</option></select>
              <select v-model="b.location" class="msel"><option value="header">header</option><option value="query">query</option></select>
              <input v-model="b.name" placeholder="field (Authorization)" class="bindinput"/>
              <input v-model="b.format" placeholder="Bearer {secret}" class="bindinput"/>
              <input v-if="b.injector==='oauth'" v-model="b.secret_field" placeholder="token" class="bindinput" style="max-width:80px;min-width:70px"/>
              <button v-if="tForm.bindings.length>1" class="btn sm" @click="removeBinding(i)" aria-label="Remove binding">✕</button>
            </div>
            <p class="sub" style="font-size:11px"><span class="mono">{secret}</span> becomes the credential - e.g. header <span class="mono">Authorization: Bearer {secret}</span>, or query <span class="mono">api_key={secret}</span>. Every binding applies on each call (multi-credential upstreams).</p>
            <div class="frow" v-if="projects.length"><label>Project</label>
              <select v-model="tForm.project" class="msel">
                <option :value="null">— team-wide (everyone) —</option>
                <option v-for="p in projects" :key="p.id" :value="p.slug">{{p.name}}</option>
              </select></div>
            <p v-if="projects.length" class="sub" style="font-size:11px;margin-top:-4px">Optional. Team-wide is the default. A project only narrows things for members you have scoped to it.</p>
          </template>
          <template v-else>
            <p class="explain">Members run <span class="mono">treg run {{tForm.cli.bin||'&lt;cli&gt;'}} -- &lt;args&gt;</span> — treg injects the key into that run's environment only; it never lands on their machine.</p>
            <div class="frow"><label>CLI command</label><input v-model="tForm.cli.bin" :disabled="!!tForm.id" placeholder="e.g. gh" style="font-family:var(--mono)"/></div>
            <div class="frow"><label>Package</label><input v-model="tForm.cli.package" placeholder="how to install it, e.g. brew install gh (optional)"/></div>
            <div class="frow"><label>API base URL</label><input v-model="tForm.base_url" placeholder="https://api.github.com — the provider behind the CLI"/></div>
            <div class="lbl" style="margin-top:14px;display:flex;align-items:center">Binding key - injected as an env var, per run <button class="btn sm" @click="tForm.cli.inject.push({via:'env',name:'',secret_id:null})" style="margin-left:auto">＋ env var</button></div>
            <div class="bindrow" v-for="(inj,i) in tForm.cli.inject" :key="'inj'+i">
              <select v-model="inj.secret_id" class="msel">
                <option v-if="!secrets.length" :value="null" disabled>- add a secret -</option>
                <option v-for="s in secrets" :key="s.id" :value="s.id">{{s.name}}</option>
              </select>
              <input v-model="inj.name" placeholder="env var, e.g. GH_TOKEN" class="bindinput" style="font-family:var(--mono)"/>
              <button v-if="tForm.cli.inject.length>1" class="btn sm" @click="tForm.cli.inject.splice(i,1)" aria-label="Remove env var">✕</button>
            </div>
            <div class="frow" v-if="projects.length"><label>Project</label>
              <select v-model="tForm.project" class="msel">
                <option :value="null">— team-wide (everyone) —</option>
                <option v-for="p in projects" :key="p.id" :value="p.slug">{{p.name}}</option>
              </select>
            </div>
            <label class="cliopt"><span class="tswitch"><input type="checkbox" v-model="tForm.cli.enabled"/><span class="knob"></span></span><span style="flex:1;min-width:0"><b>Local runs</b><span class="sub" style="display:block;margin-top:2px">{{tForm.cli.enabled?'on — members can treg run '+(tForm.cli.bin||'it')+' with the key injected':'off — every run is refused'}}</span></span></label>
            <div class="lbl" style="margin-top:14px;display:flex;align-items:center">Deny list - matching runs are refused before the key is injected <button class="btn sm" @click="tForm.cli.deny.push('')" style="margin-left:auto">＋ pattern</button></div>
            <p class="sub" style="font-size:11px;margin:4px 0 8px">Each row is a regex, matched against the whole command line AND each argument — block subcommands that execute arbitrary code (<span class="mono">(^|\s)run(\s|$)</span>) or flags that redirect the key (<span class="mono">--publish-url\b</span>).</p>
            <div class="bindrow" v-for="(p,i) in tForm.cli.deny" :key="'dn'+i">
              <input v-model="tForm.cli.deny[i]" placeholder="regex, e.g. (^|\s)run(\s|$)" class="bindinput" style="font-family:var(--mono)"/>
              <button class="btn sm" @click="tForm.cli.deny.splice(i,1)" aria-label="Remove pattern">✕</button>
            </div>
            <p class="sub" v-if="!tForm.cli.deny.length && !(tForm.cli.deny_defaults && catalogExtra.length)" style="font-size:11px;margin:4px 0 8px;color:var(--warn,#e0a458)">No guardrails — any {{tForm.cli.bin||'CLI'}} invocation gets the key.</p>
            <label class="cliopt" v-if="catalogExtra.length"><span class="tswitch"><input type="checkbox" v-model="tForm.cli.deny_defaults"/><span class="knob"></span></span><span style="flex:1;min-width:0"><b>Catalog defaults</b><span class="sub" style="display:block;margin-top:2px">treg's built-in deny patterns for {{tForm.cli.bin}} — shown below, maintained centrally</span></span></label>
            <template v-if="tForm.cli.deny_defaults && catalogExtra.length">
              <div class="bindrow" v-for="(cp,ci) in catalogExtra" :key="'cd'+ci">
                <input :value="cp" disabled class="bindinput" style="font-family:var(--mono);opacity:.55"/>
                <span class="chip" title="From the treg catalog — applies to every org's instance of this CLI; maintained centrally">catalog</span>
              </div>
            </template>
          </template>
          <div v-if="toolErr" class="banner">{{toolErr}}</div>
          <button class="btn primary" style="margin-top:6px" @click="saveTool" :disabled="toolBusy">{{toolBusy?'Saving…':(tForm.id?'Save changes':(tForm.mode==='cli'?'Create CLI':'Create endpoint'))}}</button>
        </div></div>
    </div>
</template>
