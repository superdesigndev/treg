<script lang="ts">
import { defineComponent, type PropType } from 'vue'
import { iconUrl, setupText, directiveSetupText, claudeConnectorLink, hideBrokenImage, installsInHost, type Agent, type IconUrl } from './data'

export default defineComponent({
  props: {
    agent: { type: Object as PropType<Agent>, required: true },
    icon: { type: Function as PropType<IconUrl>, default: iconUrl },
    command: { type: String, required: true },
    base: { type: String, default: () => location.origin },
    team: String, token: String, showToken: Boolean, copied: Boolean, copyDisabled: Boolean,
  },
  emits: ['copy', 'toggle-token', 'plugin', 'connector'],
  computed: {
    full() { return this.agent.directiveSetup ? directiveSetupText(this.base, this.team, this.token) : setupText(this.command, this.team, this.token) },
    masked() { return this.agent.directiveSetup ? directiveSetupText(this.base, this.team, this.token, !this.showToken) : setupText(this.command, this.team, this.token, !this.showToken) },
    connectorLink: () => claudeConnectorLink(location.origin),
    inHost(): boolean { return installsInHost(this.agent) },
    showSetup(): boolean { return !this.inHost || !!this.agent.setupFallback },
  },
  methods: { hideBrokenImage },
})
</script>

<template>
<div class="agent-setup-instructions">
  <p class="sub" style="display:flex;align-items:center;gap:8px;margin:0 0 16px"><img v-if="agent.icon" :src="icon(agent.icon)" alt="" style="width:18px;height:18px" @error="hideBrokenImage">Setting up treg for <b style="color:var(--ink)">{{agent.name}}</b></p>
  <template v-if="inHost">
    <h2 style="margin:0 0 10px;font-size:19px">{{agent.connector ? 'Add treg as a connector' : 'Install the treg plugin'}}</h2>
    <p class="sub" style="margin:0 0 14px">Open {{agent.name}} and add treg, then sign in when it asks.{{agent.setupFallback ? '' : ' You need no setup line and no key.'}}</p>
    <a v-if="agent.connector" class="btn" :href="connectorLink" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:8px" @click="$emit('connector')"><img :src="icon(agent.icon)" alt="" style="width:16px;height:16px">Add connector in {{agent.name}} ↗</a>
    <a v-else class="btn" :href="agent.plugin" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:8px" @click="$emit('plugin')"><img :src="icon(agent.icon)" alt="" style="width:16px;height:16px">Install plugin in {{agent.name}} ↗</a>
  </template>
  <template v-if="showSetup">
  <h2 v-if="!inHost" style="margin:0 0 14px;font-size:19px">In your agent's chat, send:</h2>
  <p v-else class="sub" style="font-size:12px;margin:18px 0 8px">{{agent.setupFallback}}</p>
  <div class="lc-codewrap"><button class="lc-cp" type="button" :disabled="copyDisabled" @click="$emit('copy',full)">{{copied?'✓ copied':'Copy'}}</button><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{masked}}</pre></div>
  <button v-if="token" class="text-button" type="button" style="font-size:12px;margin-top:6px" @click="$emit('toggle-token')">{{showToken?'Hide':'Show'}} key</button>
  <p v-if="!inHost" class="sub" style="font-size:12px;margin:12px 0 0">{{agent.directiveSetup ? 'Your agent runs these steps, signs in with your team & token, and confirms it worked.' : team&&token ? 'Your agent reads that file and signs in with your team & token \u2014 it installs the CLI and starts calling tools.' : 'Your agent reads that file, installs the CLI and guides you through signing in.'}}</p>
  </template>
</div>
</template>
