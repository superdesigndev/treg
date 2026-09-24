<script>
import { useDashboard } from '../state/context'
import BrandMark from '../components/BrandMark.vue'
export default { components: { BrandMark }, setup: useDashboard }
</script>

<template>
<div class="scrim" role="dialog" aria-modal="true" >
      <div class="modal" :style="{width: welcome.step===0?'min(470px,94vw)':(welcome.step===3?'min(680px,94vw)':'min(560px,94vw)')}">
        <div style="padding:26px 26px 22px">
          <template v-if="welcome.step===0">
            <div class="brand" style="color:var(--accent);font-size:15px;letter-spacing:.5px;margin-bottom:12px"><BrandMark/>treg</div>
            <h2 style="margin:0 0 8px;font-size:20px">Welcome{{me?', '+me.split('@')[0]:''}} 👋</h2>
            <p class="sub" style="margin:0 0 18px">Create your team — it's where you keep API keys and skills so your teammates and their agents can call them <b>without holding the keys</b>. You can invite people and add secrets right after.</p>
            <div class="field"><input v-model="welcome.name" placeholder="Team name, e.g. Superdesign" @keyup.enter="welcomeCreate"/></div>
            <button class="btn primary" style="width:100%;margin-top:4px" @click="welcomeCreate" :disabled="welcome.busy">{{welcome.busy?'Creating…':'Create team →'}}</button>
          </template>
          <template v-else-if="welcome.step===1">
            <div class="brand" style="color:var(--accent);font-size:15px;letter-spacing:.5px;margin-bottom:12px"><BrandMark/>treg</div>
            <h2 style="margin:0 0 8px;font-size:20px">Which agent are you using?</h2>
            <p class="sub" style="margin:0 0 18px">Choose your agent for the best setup instructions.</p>
            <treg-agent-picker v-model="welcome.agent" :icon="agentIcon"></treg-agent-picker>
            <div class="wc-foot">
              <a href="#" class="sub" @click.prevent="welcomeFinish">Skip</a>
              <button class="btn primary" @click="track('onboarding_agent_picked',{agent:welcome.agent}); welcome.step=2">Next →</button>
            </div>
          </template>
          <template v-else-if="welcome.step===2">
            <treg-setup-instructions :agent="welcomeAgent" :icon="agentIcon" :command="welcomeSetupCmd" :team="activeSlugNow || '<team-slug>'" :token="myToken" :show-token="startTokenShow" :copied="startCopied==='wc'" @copy="copyStart($event,'wc')" @toggle-token="startTokenShow=!startTokenShow" @plugin="track('onboarding_plugin_install_clicked',{agent:welcome.agent})"></treg-setup-instructions>
            <div class="wc-foot">
              <a href="#" class="sub" @click.prevent="welcome.step=1">← Back</a>
              <button class="btn primary" @click="welcome.step=3">Next →</button>
            </div>
          </template>
          <template v-else>
            <treg-try-it-out :copied="startCopied.startsWith('wtry-')?startCopied.slice(5):''" @example="track('tryit_prompt_copied',{key:$event.k,cat:$event.cat,from:'onboarding'}); copyStart($event.prompt,'wtry-'+$event.k)" @provider="welcomeTryProvider"></treg-try-it-out>
            <div class="wc-foot">
              <a href="#" class="sub" @click.prevent="welcomeFinish">Skip</a>
              <button class="btn primary" @click="track('tryit_browse_catalog',{from:'onboarding'}); welcome.on=false; go('connections')">Browse all catalog →</button>
            </div>
          </template>
          <div v-if="welcome.err" class="banner" style="margin-top:12px">{{welcome.err}}</div>
        </div>
      </div>
    </div>
</template>
