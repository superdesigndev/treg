<script setup lang="ts">
// The maker's conversations: search, pinned first, then grouped by the tool they built; drafts last.
import { computed, nextTick, ref } from 'vue'
import type { Session } from './types'

const props = defineProps<{ sessions: Session[], currentId: number | null }>()
const emit = defineEmits<{ open: [id: number], new: [], edit: [], remove: [s: Session], rename: [s: Session, title: string], pin: [s: Session] }>()
const q = ref('')

// Short: the time today, the day otherwise.
function when(iso: string): string {
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z')
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}
const editing = ref<number | null>(null)
const draftTitle = ref('')
const input = ref<HTMLInputElement[] | null>(null)

const shown = computed(() => {
  const t = q.value.trim().toLowerCase()
  return props.sessions.filter(s => !t || s.title.toLowerCase().includes(t) || (s.tool_id || '').toLowerCase().includes(t))
})
const groups = computed(() => {
  const out: { label: string, items: Session[] }[] = []
  const pinned = shown.value.filter(s => s.pinned)
  if (pinned.length) out.push({ label: 'Pinned', items: pinned })
  const byTool = new Map<string, Session[]>()
  const drafts: Session[] = []
  for (const s of shown.value.filter(s => !s.pinned)) {
    if (s.tool_id) byTool.set(s.tool_id, [...(byTool.get(s.tool_id) || []), s])
    else drafts.push(s)
  }
  for (const [tool, items] of byTool) out.push({ label: tool, items })
  if (drafts.length) out.push({ label: 'Drafts', items: drafts })
  return out
})

function startRename(s: Session) {
  editing.value = s.id; draftTitle.value = s.title
  nextTick(() => input.value?.[0]?.focus())
}
function finishRename(s: Session) {
  if (editing.value !== s.id) return
  editing.value = null
  if (draftTitle.value.trim() && draftTitle.value.trim() !== s.title) emit('rename', s, draftTitle.value.trim())
}
</script>

<template>
  <nav class="vb-list" aria-label="Conversations">
    <div class="vb-new-row">
      <button class="sa-btn primary vb-new" type="button" @click="emit('new')"><span aria-hidden="true">+</span> New chat</button>
      <button class="vb-link vb-edit-link" type="button" @click="emit('edit')">Edit a tool</button>
    </div>
    <input v-if="sessions.length > 4" v-model="q" class="vb-search" type="search" placeholder="Search conversations" aria-label="Search conversations"/>
    <section v-for="g in groups" :key="g.label" class="vb-group">
      <h4 class="vb-group-label" :title="g.label">{{ g.label }}</h4>
      <ul>
        <li v-for="s in g.items" :key="s.id" :class="{ on: currentId === s.id }">
          <input v-if="editing === s.id" ref="input" v-model="draftTitle" class="vb-rename" maxlength="80" aria-label="Conversation name"
                 @keydown.enter.prevent="finishRename(s)" @keydown.esc="editing = null" @blur="finishRename(s)"/>
          <button v-else type="button" class="vb-item" @click="emit('open', s.id)" @dblclick="startRename(s)">
            <span class="vb-item-title">{{ s.title || 'New tool' }}</span>
            <span class="sa-muted vb-item-meta">
              <span class="vb-badge" :class="s.tool_id ? 'ok' : ''">{{ s.tool_id ? 'published' : 'draft' }}</span>
              <span v-if="s.busy" class="vb-badge">working</span>
              {{ when(s.updated_at) }}
            </span>
          </button>
          <span class="vb-item-actions">
            <button type="button" :aria-label="s.pinned ? `Unpin ${s.title}` : `Pin ${s.title}`" :title="s.pinned ? 'Unpin' : 'Pin'" @click="emit('pin', s)">{{ s.pinned ? '★' : '☆' }}</button>
            <button type="button" :aria-label="`Rename ${s.title}`" title="Rename" @click="startRename(s)">✎</button>
            <button type="button" :aria-label="`Delete ${s.title}`" title="Delete" @click="emit('remove', s)">×</button>
          </span>
        </li>
      </ul>
    </section>
    <p v-if="!sessions.length" class="sa-muted vb-empty">No conversations yet.</p>
    <p v-else-if="!shown.length" class="sa-muted vb-empty">Nothing matches.</p>
  </nav>
</template>
