<script setup lang="ts">
import { ref } from 'vue'
import Avatar from './Avatar.vue'

// An avatar with a round + on its corner that opens the file picker. The parent uploads the file.
const props = withDefaults(defineProps<{ src?: string | null, name: string, size?: number, round?: boolean,
  busy?: boolean, label: string }>(), { src: null, size: 56, round: false, busy: false })
const emit = defineEmits<{ (e: 'file', ev: Event): void }>()
const input = ref<HTMLInputElement | null>(null)
</script>

<template>
  <span class="ui-avatar-edit" :class="{ busy }">
    <Avatar :src="src" :name="name" :size="size" :round="round" />
    <input ref="input" type="file" accept="image/png,image/jpeg,image/webp" class="ui-avatar-file"
           :aria-label="label + ' file'" @change="emit('file', $event)">
    <button type="button" class="ui-avatar-plus" :disabled="busy" :aria-label="(src ? 'Change ' : 'Add ') + label.toLowerCase()"
            :title="(src ? 'Change ' : 'Add ') + label.toLowerCase()" @click="input && input.click()">
      <svg v-if="!busy" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M8 3.5v9M3.5 8h9"/></svg>
      <span v-else class="ui-avatar-spin" aria-hidden="true"></span>
    </button>
  </span>
</template>

<style>
.ui-avatar-edit { position:relative; display:inline-block; flex:none; line-height:0; }
.ui-avatar-edit.busy .ui-avatar { opacity:.55; }
.ui-avatar-file { display:none; }
.ui-avatar-plus {
  position:absolute; right:-6px; bottom:-6px; width:24px; height:24px; padding:0; border-radius:50%;
  display:grid; place-items:center; cursor:pointer; color:var(--surface, #fff); background:var(--ink);
  border:2px solid var(--surface, #fff); box-shadow:0 1px 3px rgba(0,0,0,.18); transition:transform .15s;
}
.ui-avatar-plus:hover:not(:disabled) { transform:scale(1.08); }
.ui-avatar-plus:disabled { cursor:default; }
.ui-avatar-plus svg { width:12px; height:12px; }
.ui-avatar-spin { width:10px; height:10px; border-radius:50%; border:2px solid currentColor; border-right-color:transparent; animation:ui-avatar-spin .8s linear infinite; }
@keyframes ui-avatar-spin { to { transform:rotate(360deg); } }
</style>
