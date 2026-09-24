<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true"  @click.self="vendorAsk=false">
      <div class="modal" style="width:min(620px,95vw)"><div class="hd"><b>List your API in this catalog</b><button class="btn sm ico" @click="vendorAsk=false" aria-label="Close">✕</button></div>
        <div style="padding:16px 18px">
          <p class="explain">Sell an API? This is an <b>instruction for your coding agent</b>, not for you — paste it into Claude Code, Codex or any coding agent. The agent reads the hosted instructions, prepares the listing files, and opens a pull request on the treg repo — with your contact email so we can arrange live verification.</p>
          <div class="lbl">Agent instruction — paste into your coding agent</div>
          <pre class="code" style="white-space:pre-wrap">{{vendorPromptText}}</pre>
          <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <button class="btn primary" style="display:inline-flex;align-items:center;gap:7px" @click="copyVendorPrompt">
              <img :src="agentIconInv('claudecode-color')" alt="Claude Code" style="width:15px;height:15px" onerror="this.style.display='none'"/><img :src="agentIconInv('codex-color')" alt="Codex" style="width:15px;height:15px" onerror="this.style.display='none'"/>{{vendorCopied?'Copied!':'Copy agent instruction'}}</button>
            <a class="sub" style="font-size:12px;margin:0" :href="proxy+'/vendor-listing'" target="_blank" rel="noopener">Read the instructions yourself →</a>
          </div>
        </div></div>
    </div>
</template>
