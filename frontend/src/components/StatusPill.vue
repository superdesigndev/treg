<script setup lang="ts">
import { RUN_TONE } from '../workbench/format'

const props = defineProps<{ status: string, label?: string }>()
const tone = () => RUN_TONE[props.status] || 'plain'
</script>

<template>
<span class="status-pill" :class="tone()">{{ label || status.replace(/_/g, ' ') }}</span>
</template>

<style>
.status-pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:1px 9px;font-size:11.5px;font-weight:500;white-space:nowrap;background:var(--panel2);color:var(--muted)}
.status-pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.status-pill.ok{color:var(--green);background:color-mix(in srgb,var(--green) 12%,transparent)}
.status-pill.live{color:var(--teal);background:color-mix(in srgb,var(--teal) 12%,transparent)}
.status-pill.live::before{animation:status-pill-blink 1.2s infinite}
.status-pill.warn{color:var(--amber);background:color-mix(in srgb,var(--amber) 14%,transparent)}
.status-pill.bad{color:var(--red);background:color-mix(in srgb,var(--red) 12%,transparent)}
@keyframes status-pill-blink{50%{opacity:.3}}
@media (prefers-reduced-motion:reduce){.status-pill.live::before{animation:none}}
</style>
