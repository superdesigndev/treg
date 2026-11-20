<script setup lang="ts">
import { ref, watch } from 'vue'
import { initialsOf } from '../../images'

// A person's or team's picture, or its initials when there is none or it fails to load.
const props = withDefaults(defineProps<{ src?: string | null, name: string, size?: number, round?: boolean }>(),
  { src: null, size: 22, round: false })
const broken = ref(false)
watch(() => props.src, () => { broken.value = false })
</script>

<template>
  <span class="ui-avatar" :class="{ round }" aria-hidden="true"
        :style="{ width: size + 'px', height: size + 'px', fontSize: Math.max(9, Math.round(size * 0.4)) + 'px' }">
    <img v-if="src && !broken" :src="src" alt="" @error="broken = true">
    <template v-else>{{ initialsOf(name) }}</template>
  </span>
</template>

<style>
.ui-avatar { display:inline-grid; place-items:center; flex:none; overflow:hidden; border-radius:6px; background:var(--panel2); border:1px solid var(--line); color:var(--muted); font-weight:600; line-height:1; vertical-align:middle; }
.ui-avatar.round { border-radius:50%; }
.ui-avatar img { width:100%; height:100%; object-fit:cover; display:block; }
</style>
