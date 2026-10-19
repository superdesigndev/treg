<script setup lang="ts">
import { ref } from 'vue'

defineProps<{ cli: string, mcp: string }>()
const copied = ref(false)
async function copy(text: string) {
  try { await navigator.clipboard.writeText(text); copied.value = true; setTimeout(() => (copied.value = false), 1200) } catch {}
}
</script>

<template>
<p class="wb-agent">
  <span>Your agent does this with</span>
  <code>{{ mcp }}</code>
  <span>or</span>
  <code>{{ cli }}</code>
  <button type="button" class="btn sm" @click="copy(cli)">{{ copied ? 'Copied' : 'Copy' }}</button>
</p>
</template>

<style>
.wb-agent{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px;margin:16px auto 0;max-width:720px;font-size:12.5px;color:var(--muted)}
.wb-agent code{color:var(--ink);background:var(--surface,var(--panel));border:1px solid var(--line);border-radius:6px;padding:2px 7px;font-size:12px}
</style>
