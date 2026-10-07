<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import BlockView from './BlockView.vue'
import { blocks, download } from './render'

const props = defineProps<{ output: unknown, fields: string[], name: string }>()
const raw = ref(false)
const full = ref(false)
const closeBtn = ref<HTMLButtonElement | null>(null)
const shown = computed(() => blocks(props.output, props.fields))
const json = computed(() => JSON.stringify(props.output, null, 2))
const copied = ref(false)

async function copy() {
  try { await navigator.clipboard.writeText(json.value); copied.value = true; setTimeout(() => (copied.value = false), 1500) } catch { /* shown below */ }
}

// Expand: the same result over the whole window, for a table too wide to read in place.
function onKey(e: KeyboardEvent) { if (e.key === 'Escape') full.value = false }
watch(full, on => {
  document.body.style.overflow = on ? 'hidden' : ''
  if (on) { window.addEventListener('keydown', onKey); nextTick(() => closeBtn.value?.focus()) }
  else window.removeEventListener('keydown', onKey)
})
onBeforeUnmount(() => { window.removeEventListener('keydown', onKey); document.body.style.overflow = '' })
</script>

<template>
  <div class="sa-result">
    <div class="sa-result-bar">
      <div class="sa-seg" role="group" aria-label="Result view">
        <button type="button" :class="{ on: !raw }" @click="raw = false">Result</button>
        <button type="button" :class="{ on: raw }" @click="raw = true">JSON</button>
      </div>
      <div class="sa-block-actions">
        <button class="sa-btn sm" type="button" @click="copy">{{ copied ? 'Copied' : 'Copy JSON' }}</button>
        <button class="sa-btn sm" type="button" @click="download(`${name}.json`, json, 'application/json')">Download JSON</button>
        <button class="sa-btn sm" type="button" aria-label="Expand the result" title="Expand" @click="full = true">Expand</button>
      </div>
    </div>
    <pre v-if="raw" class="sa-json">{{ json }}</pre>
    <div v-else class="sa-blocks"><BlockView v-for="b in shown" :key="b.label" :block="b"/></div>

    <Teleport to="body">
      <div v-if="full" class="sa-overlay" role="dialog" aria-modal="true" :aria-label="`${name} result`" @click.self="full = false">
        <div class="sa-overlay-card">
          <div class="sa-result-bar">
            <div class="sa-seg" role="group" aria-label="Result view">
              <button type="button" :class="{ on: !raw }" @click="raw = false">Result</button>
              <button type="button" :class="{ on: raw }" @click="raw = true">JSON</button>
            </div>
            <div class="sa-block-actions">
              <button class="sa-btn sm" type="button" @click="copy">{{ copied ? 'Copied' : 'Copy JSON' }}</button>
              <button ref="closeBtn" class="sa-btn sm" type="button" @click="full = false">Close <span class="sa-muted">Esc</span></button>
            </div>
          </div>
          <div class="sa-overlay-body">
            <pre v-if="raw" class="sa-json sa-json-full">{{ json }}</pre>
            <div v-else class="sa-blocks sa-blocks-full"><BlockView v-for="b in shown" :key="b.label" :block="b"/></div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
