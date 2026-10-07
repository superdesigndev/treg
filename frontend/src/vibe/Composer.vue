<script setup lang="ts">
// The message box: grows as you type, Enter sends, Shift+Enter breaks the line. A long paste or a
// dropped file becomes an attachment the agent reads; a CSV can become the tool's data.csv instead.
import { nextTick, ref, watch } from 'vue'
import type { Attachment } from './types'

const props = defineProps<{ busy: boolean, canData: boolean }>()
const emit = defineEmits<{ send: [text: string, attachments: Attachment[]], stop: [], data: [text: string, name: string] }>()
const text = defineModel<string>({ required: true })
const files = ref<Attachment[]>([])
const area = ref<HTMLTextAreaElement | null>(null)
const over = ref(false)
const note = ref('')
const LONG_PASTE = 2000
const MAX = 200_000

function grow() {
  const el = area.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 260) + 'px'
}
watch(text, () => nextTick(grow))

function total() { return files.value.reduce((n, f) => n + (f.text || '').length, 0) }

function add(name: string, body: string) {
  note.value = ''
  if (total() + body.length > MAX) { note.value = 'Attachments are limited to 200 KB per message.'; return }
  files.value.push({ name, text: body, size: body.length })
}

function onPaste(e: ClipboardEvent) {
  const t = e.clipboardData?.getData('text') || ''
  if (t.length < LONG_PASTE) return
  e.preventDefault()
  const kind = /^\s*[[{]/.test(t) ? 'json' : t.split('\n', 2)[0].includes(',') ? 'csv' : 'txt'
  add(`pasted-${files.value.length + 1}.${kind}`, t)
}

async function onDrop(e: DragEvent) {
  over.value = false
  for (const f of Array.from(e.dataTransfer?.files || [])) {
    if (f.size > 5_000_000) { note.value = `${f.name} is over 5 MB.`; continue }
    add(f.name, await f.text())
  }
}

function send() {
  const t = text.value.trim()
  if ((!t && !files.value.length) || props.busy) return
  emit('send', t || 'Here is a file.', files.value.map(f => ({ name: f.name, text: f.text })))
  files.value = []; note.value = ''
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send() }
}

defineExpose({ focus: () => area.value?.focus() })
</script>

<template>
  <form class="vb-compose" :class="{ over }" @submit.prevent="send" @dragover.prevent="over = true" @dragleave="over = false" @drop.prevent="onDrop">
    <div v-if="files.length" class="vb-chips">
      <span v-for="(f, i) in files" :key="i" class="vb-chip">
        <code>{{ f.name }}</code> <span class="sa-muted">{{ Math.max(1, Math.round((f.size || 0) / 1024)) }} KB</span>
        <button v-if="canData && /\.csv$/i.test(f.name)" type="button" class="vb-link" @click="emit('data', f.text || '', f.name); files.splice(i, 1)">Use as data.csv</button>
        <button type="button" :aria-label="`Remove ${f.name}`" @click="files.splice(i, 1)">×</button>
      </span>
    </div>
    <div class="vb-compose-row">
      <textarea ref="area" v-model="text" rows="1" placeholder="Describe your tool, or answer the agent…" aria-label="Message" @keydown="onKey" @paste="onPaste"/>
      <button v-if="busy" class="sa-btn" type="button" @click="emit('stop')">Stop</button>
      <button v-else class="sa-btn primary" type="submit" :disabled="!text.trim() && !files.length">Send</button>
    </div>
    <p class="vb-hint sa-muted">{{ note || 'Enter to send · Shift+Enter for a new line · paste a sample or drop a file to attach' }}</p>
  </form>
</template>
