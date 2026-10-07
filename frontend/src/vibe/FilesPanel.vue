<script setup lang="ts">
// The files beside the chat: a real editor per file, every version kept (restore any, see what each
// publish was), and the test and publish buttons, which land in the conversation like the agent's asks.
import { computed, ref, watch } from 'vue'
import { api, when } from '../standalone/api'
import { build, initial } from '../standalone/form'
import InputForm from '../standalone/InputForm.vue'
import CodeEditor from './CodeEditor.vue'
import DiffView from './DiffView.vue'
import { fileText } from './diff'
import { fileOf, lineOf } from './fieldLine'
import type { Conversation, Draft } from './types'

const props = defineProps<{ conv: Conversation | null, team: string, busy: boolean, hasApp: boolean,
  agentProblem?: { at: number, problem: { field: string, rule: string } | null } }>()
const emit = defineEmits<{ draft: [d: Draft], act: [kind: string, extra: Record<string, unknown>], restored: [] }>()

type Key = 'manifest' | 'script' | 'check' | 'readme' | 'data'
const FILES: { key: Key, label: string, lang: 'json' | 'javascript' | 'markdown' | 'text' }[] = [
  { key: 'manifest', label: 'recipe.json', lang: 'json' },
  { key: 'script', label: 'run.js', lang: 'javascript' },
  { key: 'check', label: 'check.json', lang: 'json' },
  { key: 'readme', label: 'README.md', lang: 'markdown' },
  { key: 'data', label: 'data.csv', lang: 'text' },
]

const pane = ref<'files' | 'history' | 'test'>('files')
const tab = ref<Key>('manifest')
const edits = ref<Record<string, string>>({})
// The files the maker changed and has not saved. Only these are sent on Save, and only these keep
// their text when the agent writes: its changes to the other files show at once.
const changed = ref<Set<string>>(new Set())
const dirty = computed(() => changed.value.size > 0)
const problem = ref<{ field: string, rule: string } | null>(null)
const checked = ref(false)
const saving = ref(false)
const versions = ref<any[]>([])
const viewing = ref<{ n: number, before: any, after: any } | null>(null)
const testValues = ref<Record<string, unknown>>({})
const testErrors = ref<Record<string, string>>({})

const draft = computed<Draft>(() => props.conv?.draft || {})
const inputs = computed(() => draft.value.manifest?.inputs || {})
const visible = computed(() => FILES.filter(f => (f.key !== 'script' || draft.value.manifest?.script || draft.value.script)
  && (f.key !== 'data' || draft.value.data)))
const current = computed(() => FILES.find(f => f.key === tab.value)!)
const problemLine = computed(() => problem.value && fileOf(problem.value.field) === tab.value
  ? lineOf(edits.value[tab.value] || '', problem.value.field) ?? 1 : null)

function load(force = false) {
  if (force) changed.value = new Set()
  edits.value = Object.fromEntries(FILES.map(f => [f.key,
    changed.value.has(f.key) ? edits.value[f.key] : fileText(draft.value as any, f.key)]))
}
function loadTestValues() {
  testValues.value = initial(inputs.value)
  const sample = draft.value.check?.inputs || draft.value.check?.cases?.[0]?.inputs
  if (sample) for (const [k, v] of Object.entries(sample)) testValues.value[k] = typeof v === 'string' ? v : JSON.stringify(v)
}

watch(() => props.conv?.id, () => { load(true); loadTestValues(); problem.value = null; checked.value = false; viewing.value = null; if (pane.value === 'history') loadVersions() })
watch(draft, () => { load(); if (!Object.keys(testValues.value).length) loadTestValues() }, { deep: true })
// The agent's own write was validated: show that verdict, not an older one.
watch(() => props.agentProblem?.at, () => { if (props.agentProblem) { problem.value = props.agentProblem.problem; checked.value = true } })
watch(visible, v => { if (!v.some(f => f.key === tab.value)) tab.value = 'manifest' })

function onEdit(key: string, v: string) {
  if (edits.value[key] === v) return
  edits.value[key] = v; checked.value = false
  const next = new Set(changed.value)
  if (v === fileText(draft.value as any, key)) next.delete(key); else next.add(key)
  changed.value = next
}

function parsed(): { files: Draft, bad: string } {
  const files: any = {}
  for (const f of FILES) {
    if (!changed.value.has(f.key)) continue
    const raw = (edits.value[f.key] || '').trim()
    if (!raw) continue
    if (f.lang === 'json') {
      try { files[f.key] = JSON.parse(raw) } catch { return { files, bad: `${f.label} is not valid JSON` } }
    } else files[f.key] = edits.value[f.key]
  }
  return { files, bad: '' }
}

async function save(): Promise<boolean> {
  if (!props.conv) return false
  const { files, bad } = parsed()
  if (bad) { problem.value = { field: 'file', rule: bad }; checked.value = true; return false }
  saving.value = true
  try {
    const r = await api(`/vibe/sessions/${props.conv.id}/draft`, { method: 'PUT', json: files })
    changed.value = new Set()
    emit('draft', r.draft)
    problem.value = r.problem; checked.value = true
    return true
  } finally { saving.value = false }
}

async function validate() {
  if (!props.conv) return
  if (dirty.value) { await save(); return }
  problem.value = (await api(`/vibe/sessions/${props.conv.id}/validate`, { method: 'POST' })).problem
  checked.value = true
  if (problem.value) tab.value = fileOf(problem.value.field) as Key
}

async function loadVersions() {
  if (!props.conv) return
  versions.value = (await api(`/vibe/sessions/${props.conv.id}/versions`)).versions
}

async function view(n: number) {
  if (viewing.value?.n === n) { viewing.value = null; return }
  const v = await api(`/vibe/sessions/${props.conv!.id}/versions/${n}`)
  viewing.value = { n, before: v.prev, after: v.files }
}

async function restore(n: number) {
  if (!props.conv || !confirm(`Restore draft ${n}? Your current files stay in the history.`)) return
  const r = await api(`/vibe/sessions/${props.conv.id}/versions/${n}/restore`, { method: 'POST' })
  changed.value = new Set()
  emit('draft', r.draft)
  problem.value = r.problem; checked.value = true
  emit('restored')
  await loadVersions()
}

async function testRun() {
  if (dirty.value && !(await save())) return
  const b = build(inputs.value, testValues.value)
  testErrors.value = b.errors
  if (Object.keys(b.errors).length) return
  emit('act', 'test', { inputs: b.body })
}

async function publish() {
  if (dirty.value && !(await save())) return
  if (!confirm('Publish this tool? Its check.json runs once for real on your team\'s balance.')) return
  emit('act', 'publish', {})
}

async function removeData() {
  if (!props.conv || !confirm('Remove data.csv from the draft?')) return
  const r = await api(`/vibe/sessions/${props.conv.id}/draft`, { method: 'PUT', json: { data: '' } })
  emit('draft', r.draft); problem.value = r.problem; checked.value = true
}

const author: Record<string, string> = { agent: 'the agent', maker: 'you', load: 'loaded', restore: 'restored' }

defineExpose({ saveIfDirty: async () => (dirty.value ? save() : true), showFile: (k: Key) => { pane.value = 'files'; tab.value = k } })
</script>

<template>
  <aside class="vb-files">
    <div class="sa-seg vb-panes" role="group" aria-label="Panel">
      <button type="button" :class="{ on: pane === 'files' }" @click="pane = 'files'">Files</button>
      <button type="button" :class="{ on: pane === 'history' }" @click="pane = 'history'; loadVersions()">History</button>
      <button type="button" :class="{ on: pane === 'test' }" @click="pane = 'test'">Test &amp; publish</button>
    </div>
    <p v-if="!conv" class="sa-muted">Files appear here as the agent writes them.</p>

    <template v-else-if="pane === 'files'">
      <div class="vb-tabs" role="tablist">
        <button v-for="f in visible" :key="f.key" type="button" role="tab" :aria-selected="tab === f.key"
                :class="{ on: tab === f.key, bad: problem && fileOf(problem.field) === f.key }" @click="tab = f.key">{{ f.label }}</button>
      </div>
      <CodeEditor :key="`${conv.id}-${tab}`" :model-value="edits[tab] || ''" :lang="current.lang"
                  :problem-line="problemLine" :problem-text="problem ? `${problem.field}: ${problem.rule}` : ''"
                  @update:model-value="v => onEdit(tab, v)"/>
      <div class="vb-actions">
        <button class="sa-btn sm" type="button" :disabled="!dirty || saving || busy" :title="busy ? 'The agent is working; save when it is done' : ''" @click="save">{{ saving ? 'Saving…' : 'Save' }}</button>
        <button class="sa-btn sm" type="button" @click="validate">Validate</button>
        <button v-if="tab === 'data'" class="sa-btn sm" type="button" @click="removeData">Remove data.csv</button>
        <span v-if="dirty" class="sa-muted vb-small">Unsaved changes</span>
        <span v-else-if="checked && !problem" class="sa-ok vb-small">Valid</span>
      </div>
      <p v-if="checked && problem" class="sa-error-text">
        <button type="button" class="vb-link" @click="tab = fileOf(problem.field) as Key"><code>{{ problem.field }}</code></button>: {{ problem.rule }}
      </p>
    </template>

    <template v-else-if="pane === 'history'">
      <p class="sa-muted vb-small">Every change, by you or the agent. Restoring adds a new version; nothing is lost.</p>
      <ul class="vb-versions">
        <li v-for="(v, i) in versions" :key="v.n">
          <div class="vb-version-row">
            <span><b>Draft {{ v.n }}</b><span v-if="i === 0" class="vb-badge">current</span>
              <span v-if="v.published_version" class="vb-badge ok">published v{{ v.published_version }}</span></span>
            <span class="sa-muted vb-small">{{ ['load', 'restore'].includes(v.author) && v.note ? v.note : (author[v.author] || v.author) + (v.note ? ` · ${v.note}` : '') }} · {{ when(v.at) }}</span>
          </div>
          <div class="vb-actions">
            <button class="sa-btn sm" type="button" @click="view(v.n)">{{ viewing?.n === v.n ? 'Hide changes' : 'Changes' }}</button>
            <button v-if="i > 0" class="sa-btn sm" type="button" :disabled="busy" @click="restore(v.n)">Restore</button>
          </div>
          <DiffView v-if="viewing && viewing.n === v.n" :before="viewing.before" :after="viewing.after"/>
        </li>
      </ul>
      <p v-if="!versions.length" class="sa-muted vb-small">No versions yet.</p>
    </template>

    <template v-else>
      <h3>Test run</h3>
      <p class="sa-muted vb-small">Runs the draft for real on {{ team }}'s balance. Nothing is published. The result shows in the conversation.</p>
      <InputForm v-model="testValues" :inputs="inputs" :errors="testErrors" :disabled="busy"/>
      <div class="vb-actions">
        <button class="sa-btn sm primary" type="button" :disabled="busy || !draft.manifest" @click="testRun">Test run</button>
        <label class="sa-muted vb-small vb-auto"><input type="checkbox" :checked="conv.auto_test" @change="emit('act', 'auto', { on: ($event.target as HTMLInputElement).checked })"/>
          Let the agent test-run without asking</label>
      </div>
      <h3 class="vb-gap">Publish</h3>
      <p class="sa-muted vb-small">check.json runs once for real; the tool goes live on pass, as <code>{{ team }}.{{ draft.manifest?.name || '…' }}</code>.</p>
      <div class="vb-actions">
        <button class="sa-btn sm primary" type="button" :disabled="busy || !draft.manifest" @click="publish">{{ conv.tool_id ? 'Publish a new version' : 'Publish' }}</button>
      </div>
      <template v-if="conv.tool_id">
        <h3 class="vb-gap">App</h3>
        <p class="sa-muted vb-small">A web page where people fill a form and run the tool as their own team.</p>
        <div class="vb-actions">
          <button v-if="!hasApp" class="sa-btn sm" type="button" :disabled="busy" @click="emit('act', 'app', {})">Turn on the app</button>
          <button class="sa-btn sm" type="button" :disabled="busy" @click="emit('act', 'password-card', {})">{{ hasApp ? 'Password…' : 'Set a password…' }}</button>
        </div>
      </template>
      <p class="sa-muted vb-small vb-gap">The agent's model budget is treg's; test runs and the publish check are charged to {{ team }} as usual.</p>
    </template>
  </aside>
</template>
