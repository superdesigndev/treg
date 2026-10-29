<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-labelledby="run-tool-dialog-title" aria-modal="true" v-dialog="() => { tryTool=null }" @click.self="tryTool=null" style="place-items:stretch;justify-items:end">
      <div class="drawer"><div class="hd" style="padding:15px 18px;border-bottom:1px solid var(--line)"><b id="run-tool-dialog-title">Use “{{tryTool.name}}”</b><button class="btn sm" @click="tryTool=null" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px;overflow:auto">
          <template v-if="useMode==='call'">
            <p class="explain">Enter the API path for <span class="mono">{{tryTool.host}}</span> (everything after the domain) and Send - it runs through the proxy with the key injected. You never send the key.</p>
            <div v-if="tryTool.examples && tryTool.examples.length">
              <div class="lbl">Examples - click to fill</div>
              <div class="exrow"><span v-for="(ex,exi) in tryTool.examples" :key="exi" class="exchip" :class="{on:tryPath===ex.path}" @click="tryPath=ex.path; tryMethod=ex.method||'GET'"><span class="m">{{ex.method||'GET'}}</span>{{ex.note||ex.path}}</span></div>
            </div>
            <div class="field"><select v-model="tryMethod"><option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option></select><input v-model="tryPath" placeholder="path e.g. me" @keyup.enter="runTry"/></div>
            <textarea v-if="tryMethod!=='GET'" v-model="tryBody" placeholder='request body (e.g. {"key":"value"})' rows="3" style="width:100%;margin-top:8px;background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:var(--rb);padding:8px;font-family:var(--mono);font-size:12.5px"></textarea>
            <div class="muted" style="font-size:12px;margin:6px 0 12px">→ {{tryMethod}} {{tryTool.base_url}}/{{tryPath}}</div>
            <button class="btn primary" @click="runTry" :disabled="trying">{{trying?'Sending…':'▶ Send'}}</button>
            <div v-if="tryResp!==null" style="margin-top:14px"><div class="muted" style="font-size:12px;margin-bottom:6px">Response <span class="badge" :class="(tryStatus>0&&tryStatus<400)?'ok':'invalid'">{{tryStatus||'ERR'}}</span> · {{tryMs}}ms · key injected by registry</div><pre>{{tryResp}}</pre></div>
          </template>

          <template v-if="useMode==='local'">
            <p class="explain">This tool is a CLI. Run it on your own machine with the treg CLI: treg supplies the credential at run time, so you never paste the key.</p>
            <pre class="code">treg run {{tryTool.name}} -- &lt;arguments&gt;</pre>
            <p v-if="!(tryTool.cli && tryTool.cli.enabled)" class="muted" style="font-size:12px;margin:8px 0 0">Local runs are off for this tool, so this command is refused until an owner or admin turns on <b>Local runs</b> on its card.</p>
            <div style="margin-top:12px"><button class="btn primary" @click="copy('treg run '+tryTool.name+' -- ')">⧉ {{copied?'Copied!':'Copy'}}</button></div>
          </template>
        </div></div>
    </div>
</template>
