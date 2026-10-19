<script setup lang="ts">
defineProps<{ title: string, closable?: boolean }>()
defineEmits<{ close: [] }>()
</script>

<template>
<aside class="inspector">
  <header class="inspector-head">
    <slot name="head"><strong>{{ title }}</strong></slot>
    <button v-if="closable" type="button" class="inspector-close" aria-label="Close" @click="$emit('close')">✕</button>
  </header>
  <div class="inspector-body"><slot /></div>
  <footer v-if="$slots.foot" class="inspector-foot"><slot name="foot" /></footer>
</aside>
</template>

<style>
.inspector{width:380px;flex:none;border-radius:var(--r);background:var(--surface);box-shadow:var(--shadow-md),0 0 0 1px var(--line);overflow:hidden;display:flex;flex-direction:column;min-height:0;max-height:calc(100vh - 160px);position:sticky;top:16px}
.inspector-head{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line);font-size:14px}
.inspector-head strong{flex:1;font-weight:500;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.inspector-close{border:0;background:none;color:var(--muted);padding:4px 6px;cursor:pointer;border-radius:6px}
.inspector-close:hover{color:var(--ink);background:var(--hover)}
.inspector-body{flex:1;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:14px}
.inspector-foot{border-top:1px solid var(--line);padding:12px 14px;display:flex;flex-direction:column;gap:8px}
.inspector h4{margin:0;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
.inspector .kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:6px 0 0;font-size:13px}
.inspector .kv dt{color:var(--muted)}
.inspector .kv dd{margin:0;overflow-wrap:anywhere}
.inspector .wb-hint{font-size:12.5px;color:var(--muted);margin:0}
.inspector pre{margin:6px 0 0;padding:10px 12px;border-radius:var(--rb);background:var(--panel2);font:12.5px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word;max-height:40vh;overflow:auto;color:var(--ink)}
@media (max-width:980px){.inspector{width:100%;position:static;max-height:none}}
</style>
