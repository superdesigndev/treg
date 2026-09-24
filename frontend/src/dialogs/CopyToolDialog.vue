<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="copyTool=null">
      <div class="modal"><div class="hd"><b>Use “{{copyTool.name}}”</b><button class="btn sm ico" @click="copyTool=null" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px">
          <div class="tabs"><button v-for="t in snippetTabs" :key="t" :class="{active:snippetTab===t}" @click="snippetTab=t">{{t}}</button></div>
          <p class="explain">One call to the proxy - your key is injected server-side. <b>PATH</b> is the <span class="mono">{{copyTool.host}}</span> path you'd normally call.</p>
          <div v-if="copyTool.examples && copyTool.examples.length">
            <div class="lbl">Examples - click to drop into the snippet</div>
            <div class="exrow"><span v-for="(ex,exi) in copyTool.examples" :key="exi" class="exchip" :class="{on:exPath===ex.path}" @click="pickEx(ex)"><span class="m">{{ex.method||'GET'}}</span>{{ex.note||ex.path}}</span></div>
          </div>
          <pre class="code" v-html="snip.html"></pre>
          <div style="margin-top:12px;display:flex;gap:10px;align-items:center"><button class="btn primary" @click="copy(snip.text)">⧉ {{copied?'Copied!':'Copy'}}</button><span class="sub" style="font-size:12px;margin:0"><template v-if="myToken">Your token is included - paste and run.</template><template v-else><span class="mono">$TREG_TOKEN</span> is a placeholder - set it to your token.</template></span></div>
        </div></div>
    </div>
</template>
