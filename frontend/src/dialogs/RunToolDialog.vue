<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="tryTool=null" style="place-items:stretch;justify-items:end">
      <div class="drawer"><div class="hd" style="padding:15px 18px;border-bottom:1px solid var(--line)"><b>Use “{{tryTool.name}}”</b><button class="btn sm" @click="tryTool=null" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px;overflow:auto">
          <div v-if="canCall(tryTool) && canRun(tryTool)" class="exrow" style="margin-bottom:12px">
            <span class="exchip" :class="{on:useMode==='call'}" @click="useMode='call'"><span class="m">HTTP</span>API call</span>
            <span class="exchip" :class="{on:useMode==='run'}" @click="useMode='run'"><span class="m">CLI</span>run on server</span>
          </div>

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

          <template v-if="useMode==='run'">
            <p class="explain">Runs <span class="mono">{{(tryTool.cli&&tryTool.cli.bin)||tryTool.name}}</span> on the registry server with the credential injected there - the key never reaches your machine. Same as <span class="mono">treg run --server {{tryTool.name}}</span>.</p>
            <div class="field"><input v-model="runArgsStr" :placeholder="'CLI arguments, e.g. get /v1/balance'" @keyup.enter="doRun" style="width:100%"/></div>
            <div class="muted" style="font-size:12px;margin:6px 0 12px">→ {{(tryTool.cli&&tryTool.cli.bin)||tryTool.name}} {{runArgsStr}}</div>
            <button class="btn primary" @click="doRun" :disabled="running">{{running?'Running…':'❯ Run'}}</button>
            <div v-if="runOut" style="margin-top:14px">
              <div class="muted" style="font-size:12px;margin-bottom:6px">Result <span class="badge" :class="runOut.exit_code===0?'ok':'invalid'">exit {{runOut.exit_code}}</span> · {{runOut.duration_ms}}ms<span v-if="runOut.timed_out"> · timed out</span> · key injected server-side</div>
              <pre v-if="runOut.stdout">{{runOut.stdout}}</pre>
              <pre v-if="runOut.stderr" style="opacity:.75">{{runOut.stderr}}</pre>
              <pre v-if="runOut.detail" style="opacity:.75">{{runOut.detail}}</pre>
            </div>
          </template>
        </div></div>
    </div>
</template>
