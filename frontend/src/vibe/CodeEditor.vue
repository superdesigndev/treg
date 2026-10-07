<script setup lang="ts">
// One file of the draft in a real editor: highlighting, line numbers, JSON errors in place, and the
// hub validator's refusal shown on the line it names (`problemLine`).
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { basicSetup } from 'codemirror'
import { EditorState, Compartment } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { json, jsonParseLinter } from '@codemirror/lang-json'
import { javascript } from '@codemirror/lang-javascript'
import { markdown } from '@codemirror/lang-markdown'
import { linter, lintGutter, forceLinting, type Diagnostic } from '@codemirror/lint'
import { oneDark } from '@codemirror/theme-one-dark'

const props = defineProps<{ lang: 'json' | 'javascript' | 'markdown' | 'text', problemLine?: number | null,
  problemText?: string, readonly?: boolean }>()
const value = defineModel<string>({ required: true })
const host = ref<HTMLElement | null>(null)
let view: EditorView | null = null
const langSlot = new Compartment()
const themeSlot = new Compartment()
const dark = () => document.documentElement.dataset.theme === 'dark'
let themeWatch: MutationObserver | null = null
const problem = { line: null as number | null, text: '' }

function language() {
  if (props.lang === 'json') return [json(), linter(jsonParseLinter(), { delay: 300 })]
  if (props.lang === 'javascript') return [javascript()]
  if (props.lang === 'markdown') return [markdown()]
  return []
}

// The validator's refusal, as a diagnostic on its line.
const validatorLint = linter((v): Diagnostic[] => {
  if (!problem.line || problem.line > v.state.doc.lines) return []
  const line = v.state.doc.line(problem.line)
  return [{ from: line.from, to: line.to, severity: 'error', message: problem.text, source: 'hub' }]
}, { delay: 0 })

onMounted(() => {
  view = new EditorView({
    parent: host.value!,
    state: EditorState.create({
      doc: value.value,
      extensions: [
        basicSetup, lintGutter(), validatorLint, langSlot.of(language()), themeSlot.of(dark() ? oneDark : []),
        EditorState.readOnly.of(!!props.readonly),
        EditorView.lineWrapping,
        EditorView.updateListener.of(u => { if (u.docChanged) value.value = u.state.doc.toString() }),
        EditorView.theme({ '&': { height: '100%' }, '.cm-scroller': { fontFamily: 'var(--mono)', fontSize: '12px' } }),
      ],
    }),
  })
  sync()
  // The page's theme can change while open: follow it.
  themeWatch = new MutationObserver(() => view?.dispatch({ effects: themeSlot.reconfigure(dark() ? oneDark : []) }))
  themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
})

function sync() {
  problem.line = props.problemLine ?? null
  problem.text = props.problemText || ''
  if (view) forceLinting(view)
}

watch(() => value.value, v => {
  if (view && v !== view.state.doc.toString()) view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: v } })
})
watch(() => props.lang, () => view?.dispatch({ effects: langSlot.reconfigure(language()) }))
watch(() => [props.problemLine, props.problemText], sync)
onBeforeUnmount(() => { themeWatch?.disconnect(); view?.destroy() })
</script>

<template>
  <div ref="host" class="vb-editor"/>
</template>

<style>
.vb-editor { flex:1; min-height:320px; border:1px solid var(--line2); border-radius:10px; overflow:hidden; background:var(--surface); display:flex; }
.vb-editor .cm-editor { flex:1; min-width:0; }
.vb-editor .cm-editor.cm-focused { outline:none; }
.vb-editor .cm-gutters { background:var(--panel2); border-right:1px solid var(--line); color:var(--muted); }
.vb-editor .cm-activeLine, .vb-editor .cm-activeLineGutter { background:var(--hover); }
[data-theme="dark"] .vb-editor .cm-content { caret-color:var(--ink); }
</style>
