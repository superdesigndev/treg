<script>
import { useDashboard } from '../state/context'

// One of the team's own tools or skills, on Your own tools: the card is Connections' credential card,
// so the two halves of "whose key" read alike. The card opens the shareable page; the buttons act.
export default {
  props: {
    t: { type: Object, default: null },      // a tool (endpoint or CLI)
    skill: { type: Object, default: null },  // or a knowledge skill (a recipe-only bundle)
  },
  setup: useDashboard,
  computed: {
    name() { return (this.t || this.skill).name },
    href() { return this.t ? this.rowHref(this.t) : '/app/skills/'+encodeURIComponent(this.skill.name) },
    // Where a call runs is the one fact that changes what a member may do with it.
    where() {
      const t = this.t
      if (!t) return { label: 'Skill', tone: 'quiet', title: 'A knowledge skill: an agent installs it with the CLI' }
      if (!t.cli) return { label: 'API', tone: 'quiet', title: 'Called through the proxy, the key injected server-side' }
      if (t.server_runnable) return { label: 'Server', tone: 'ok', title: 'Runs on the server: the key is injected there, never on a member\'s machine' }
      return { label: 'Local only', tone: 'warn', title: 'This CLI authenticates from the member\'s own machine (treg run --local)' }
    },
    creds() { return this.t ? this.credChips(this.t).map(w => w.label).join(', ') : '' },
  },
  methods: {
    open() { this.t ? this.rowOpen(this.t) : this.openDetail('skill', this.skill.name) },
  },
}
</script>

<template>
<div class="pl-card cn-card own-card" :class="'cn-'+where.tone" role="link" tabindex="0"
     title="Open its page (shareable link)" @click="open" @keydown.enter.self="open">
  <div class="cn-top">
    <span class="pl-logo lg gen" :style="{background:platTileBg(name)}" aria-hidden="true"><span class="pt-i">{{platInitial({label:name})}}</span></span>
    <span class="cn-id">
      <a :href="href" @click.prevent.stop="open"><b>{{name}}</b></a>
      <span class="pl-meta" :title="t ? t.base_url : ''">{{t ? t.host : 'treg skill install '+name}}</span>
    </span>
    <span class="cn-st" :title="where.title">{{where.label}}</span>
  </div>

  <p v-if="t" class="cn-what">
    {{t.cli ? 'CLI '+(t.cli.bin||t.name) : 'HTTP endpoint'}} · {{creds ? creds+' credential' : 'no credential'}}<template v-if="t.owner"> · added by {{short(t.owner)}}</template>
  </p>

  <div v-if="t" class="cn-acts" @click.stop>
    <button class="pl-btn sm" @click="openUse(t)" title="Use it - API call or CLI run">Try it</button>
    <button class="pl-btn sm ghost" @click="openCopy(t)" title="Copy a call snippet">Copy snippet</button>
    <span class="cn-links">
      <button v-if="t.cli && canRegister" @click="toggleLocalRun(t)" :title="localRunTitle(t)">Local runs {{t.cli.enabled?'on':'off'}}</button>
      <button v-if="canRegister" @click="openEditTool(t)">Edit</button>
      <button v-if="canRegister" class="cn-del" :class="{armed:confirmDelTool===t.id}" @click="deleteTool(t)">
        {{confirmDelTool===t.id ? 'Click again to delete' : 'Delete'}}</button>
    </span>
  </div>
  <div v-else class="cn-acts" @click.stop>
    <button class="pl-btn sm" @click="openRecipeCopy(skill)">Install</button>
    <span class="cn-links">
      <button @click="openRecipeView(skill)">View SKILL.md</button>
      <button v-if="canRegister" class="cn-del" :class="{armed:confirmDelBundle===skill.id}" @click="deleteRecipe(skill)">
        {{confirmDelBundle===skill.id ? 'Click again to delete' : 'Delete'}}</button>
    </span>
  </div>
</div>
</template>
