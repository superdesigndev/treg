<script lang="ts">
import { defineComponent, type PropType } from 'vue'
import { agents, moreAgents, iconUrl, hideBrokenImage, type IconUrl } from './data'

export default defineComponent({
  props: { modelValue: String, icon: { type: Function as PropType<IconUrl>, default: iconUrl } },
  emits: ['update:modelValue'],
  data: () => ({ moreOpen: false }),
  computed: {
    agents: () => agents,
    moreAgents: () => moreAgents,
    moreSelected() { return moreAgents.find(a => a.id === this.modelValue) },
  },
  methods: {
    pick(id: string) { this.$emit('update:modelValue', id); this.moreOpen = false },
    hideBrokenImage,
  },
})
</script>

<template>
<div><div class="agent-grid">
  <button v-for="a in agents" :key="a.id" type="button" class="agent-card" :class="{on:modelValue===a.id}" :aria-pressed="modelValue===a.id" @click="pick(a.id)"><img :src="icon(a.icon)" alt="" @error="hideBrokenImage"><span>{{a.name}}</span></button>
  <button type="button" class="agent-card" :class="{on:moreOpen||moreSelected}" :aria-expanded="moreOpen" @click="moreOpen=!moreOpen"><span aria-hidden="true">☰</span><span>{{moreSelected&&!moreOpen?moreSelected.name:'More'}}</span><span aria-hidden="true" style="margin-left:auto">▾</span></button>
</div><div v-if="moreOpen" class="agent-grid" style="margin-top:10px">
  <button v-for="a in moreAgents" :key="a.id" type="button" class="agent-card" :class="{on:modelValue===a.id}" :aria-pressed="modelValue===a.id" @click="pick(a.id)"><img v-if="a.icon" :src="icon(a.icon)" alt="" @error="hideBrokenImage"><span v-else aria-hidden="true">✳</span><span>{{a.name}}</span></button>
</div></div>
</template>
