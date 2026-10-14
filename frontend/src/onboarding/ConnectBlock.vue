<script setup lang="ts">
// Give the same tool to the agent: one tab per way an agent connects. Paste works for any agent;
// the terminal command installs the CLI and the MCP server everywhere it can; Cursor installs the MCP
// server from a link; Claude.ai and ChatGPT add treg as a connector over OAuth; Grok Bot has a plugin.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { command, iconUrl, setupText } from '../agent-setup/data'

// `say` is the task's sentence with one blank ({}); `value` fills it. The blank is an input here
// too, the same value as the run's input above: change either and both follow.
const props = defineProps<{ base: string; team: string; token: string; say: string; value: string }>()
const emit = defineEmits<{ copied: [method: string]; 'update:value': [value: string] }>()
const sayParts = computed(() => { const i = props.say.indexOf('{}'); return i < 0 ? [props.say, ''] : [props.say.slice(0, i), props.say.slice(i + 2)] })
const taskSentence = computed(() => props.say.replace('{}', props.value.trim()))

type Method = 'paste' | 'terminal' | 'cursor' | 'connector' | 'grok'
const METHODS: Record<Method, { label: string; sub: string; icon?: string }> = {
  paste: { label: "Paste into your agent's chat", sub: 'any agent', icon: 'claudecode-color' },
  terminal: { label: 'Terminal', sub: 'one command', icon: 'codex-color' },
  cursor: { label: 'Cursor', sub: 'one click', icon: 'cursor' },
  connector: { label: 'Claude.ai / ChatGPT', sub: 'connector', icon: 'claude-color' },
  grok: { label: 'Grok Bot', sub: 'plugin', icon: '/logos/agents/grokbot.png' },
}
const ORDER: Method[] = ['paste', 'terminal', 'cursor', 'connector', 'grok']
const GROK_PLUGIN = 'https://x.ai/bot/plugin/55647425'
const BOX_ICONS = ['claudecode-color', 'codex-color', 'cursor', 'claude-color']

const method = ref<Method>('paste')
const showToken = ref(false)
const menuOpen = ref(false)
const copied = ref('')
const failed = ref('')
const primary = ORDER.slice(0, 3)
const more = ORDER.slice(3)

const base = computed(() => props.base.replace(/\/$/, ''))
const mcpUrl = computed(() => base.value + '/mcp/')
const masked = computed(() => props.token ? props.token.slice(0, 14) + '••••••••••••••••' : '<YOUR_TOKEN>')
const tokenShown = computed(() => (showToken.value ? props.token : masked.value))
const pasteText = computed(() => setupText(command(base.value), props.team, props.token) + ', then ' + taskSentence.value)
const pasteHead = computed(() => setupText(command(base.value), props.team, '\u0000').split('\u0000')[0])
const terminalText = computed(() => `curl -fsSL ${base.value}/install.sh | sh -s -- --token ${props.token}`)
const cursorLink = computed(() => {
  const config = { url: mcpUrl.value, headers: { Authorization: 'Bearer ' + props.token } }
  return 'cursor://anysphere.cursor-deeplink/mcp/install?name=treg&config=' + encodeURIComponent(btoa(JSON.stringify(config)))
})
const capSentence = computed(() => taskSentence.value.charAt(0).toUpperCase() + taskSentence.value.slice(1))
const COPY: Record<string, () => string> = {
  paste: () => pasteText.value, terminal: () => terminalText.value, task: () => capSentence.value, mcp: () => mcpUrl.value,
}

const copyLabel = (key: string) => (copied.value === key ? 'Copied ✓' : failed.value === key ? 'Select and copy' : 'Copy')

async function copy(key: string, target: string) {
  let ok = false
  try { await navigator.clipboard.writeText(COPY[key]()); ok = true } catch { ok = false }
  if (ok) {
    failed.value = ''; copied.value = key; emit('copied', key)
    setTimeout(() => { if (copied.value === key) copied.value = '' }, 1500)
  } else {
    // No alert: reveal the token, select the text, and say so.
    showToken.value = true; copied.value = ''; failed.value = key
    await nextTick()
    const el = document.getElementById(target)
    if (el) { const r = document.createRange(); r.selectNodeContents(el); const s = getSelection(); s?.removeAllRanges(); s?.addRange(r) }
    setTimeout(() => { if (failed.value === key) failed.value = '' }, 3000)
  }
}

const tabs = ref<HTMLElement | null>(null)
const ind = ref<HTMLElement | null>(null)
const activeId = computed(() => (primary.includes(method.value) ? 'ob-tab-' + method.value : 'ob-tab-more'))
function place(animate: boolean) {
  const btn = tabs.value?.querySelector<HTMLElement>('#' + activeId.value)
  const el = ind.value
  if (!btn || !el || !btn.offsetWidth) return
  if (!animate) el.classList.add('noanim')
  el.style.width = btn.offsetWidth + 'px'
  el.style.height = btn.offsetHeight + 'px'
  el.style.transform = `translate(${btn.offsetLeft}px, ${btn.offsetTop}px)`
  if (!animate) { void el.offsetWidth; el.classList.remove('noanim') }
}
function pick(m: Method) { menuOpen.value = false; method.value = m }
watch(method, () => nextTick(() => place(true)))
const onResize = () => place(false)
const onDoc = (e: MouseEvent) => { if (menuOpen.value && !(e.target as HTMLElement).closest('.mseg-wrap')) menuOpen.value = false }
onMounted(() => {
  nextTick(() => place(false)); document.fonts?.ready.then(() => place(false))
  addEventListener('resize', onResize); document.addEventListener('click', onDoc)
})
onBeforeUnmount(() => { removeEventListener('resize', onResize); document.removeEventListener('click', onDoc) })

function onTabKey(e: KeyboardEvent) {
  const btns = [...(tabs.value?.querySelectorAll<HTMLElement>('.mtab') || [])]
  const i = btns.indexOf(document.activeElement as HTMLElement)
  if (i < 0) return
  if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    e.preventDefault(); menuOpen.value = false
    const next = btns[(i + (e.key === 'ArrowRight' ? 1 : btns.length - 1)) % btns.length]
    next.focus()
    if (next.dataset.method) pick(next.dataset.method as Method)
  } else if (e.key === 'ArrowDown' && btns[i].id === 'ob-tab-more') { e.preventDefault(); menuOpen.value = true }
}
const mark = (m: Method) => iconUrl(METHODS[m].icon)
const hide = (e: Event) => { (e.target as HTMLElement).style.visibility = 'hidden' }
</script>

<template>
<div>
  <div class="mseg-wrap">
    <div ref="tabs" class="mseg" role="tablist" aria-label="Connection method" @keydown="onTabKey">
      <span ref="ind" class="seg-ind" aria-hidden="true"></span>
      <button v-for="m in primary" :id="'ob-tab-'+m" :key="m" type="button" class="mtab" role="tab" :data-method="m"
        :aria-selected="method===m" :tabindex="method===m ? 0 : -1" aria-controls="ob-panel" @click="pick(m)">
        <span class="mtab-ico" aria-hidden="true"><img :src="mark(m)" alt="" @error="hide"></span>
        <span class="mtab-t"><span class="mtab-l">{{METHODS[m].label}}</span></span>
      </button>
      <button id="ob-tab-more" type="button" class="mtab ob-more" role="tab" aria-haspopup="menu" :aria-expanded="menuOpen"
        :aria-selected="more.includes(method)" :tabindex="more.includes(method) ? 0 : -1" aria-controls="ob-panel" @click="menuOpen=!menuOpen">
        <span class="mtab-ico" aria-hidden="true">
          <img v-if="more.includes(method)" :src="mark(method)" alt="" @error="hide"><span v-else class="ni">···</span>
        </span>
        <span class="mtab-t">
          <span class="mtab-l">{{more.includes(method) ? METHODS[method].label : 'More'}}<svg class="caret" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
        </span>
      </button>
    </div>
    <div v-if="menuOpen" class="more-menu" role="menu" aria-label="More connection methods" @keydown.esc="menuOpen=false">
      <button v-for="m in more" :key="m" type="button" role="menuitem" :aria-current="method===m" @click="pick(m)">
        <span class="mtab-ico" aria-hidden="true"><img :src="mark(m)" alt="" @error="hide"></span>
        <span class="mi-t"><span>{{METHODS[m].label}}</span><small>{{METHODS[m].sub}}</small></span>
      </button>
    </div>
  </div>

  <div class="panel-wrap"><Transition name="ob-panel" mode="out-in">
    <div :key="method" id="ob-panel" class="panel" role="tabpanel" :aria-labelledby="activeId">
      <div class="givebox">
        <div class="gb-h">Give this to your agent<span class="agicons" aria-hidden="true">
          <template v-if="method==='cursor' || method==='grok'"><img :src="iconUrl(METHODS[method].icon, 'dark')" alt="" @error="hide"></template>
          <template v-else><img v-for="ic in BOX_ICONS" :key="ic" :src="iconUrl(ic, 'dark')" alt="" @error="hide"></template>
        </span></div>

        <div v-if="method==='grok'" class="gb-cmd center">
          <a class="ob-btn oneclick" :href="GROK_PLUGIN" target="_blank" rel="noopener"><span class="glyph" aria-hidden="true"><svg viewBox="0 0 290 290"><rect width="140.5" height="140.5" rx="20"/><rect x="149.5" y="149.5" width="140.5" height="140.5" rx="20"/></svg></span>Install the treg plugin</a>
        </div>
        <div v-if="method==='paste' || method==='grok'" class="gb-cmd multi">
          <span class="ps" aria-hidden="true">$</span>
          <pre id="ob-sel-paste">{{pasteHead}}<span class="tokchip" :class="{full: showToken}">{{tokenShown}}</span>, then <span class="task">{{sayParts[0]}}<input class="task-in" :value="value" :size="Math.max(4, value.length)" aria-label="Task input" spellcheck="false" @input="emit('update:value', ($event.target as HTMLInputElement).value)">{{sayParts[1]}}</span></pre>
          <div class="gb-actions">
            <button type="button" class="ob-btn ob-copy ob-primary" :class="{copied: copied==='paste', failed: failed==='paste'}" @click="copy('paste','ob-sel-paste')"><span class="ob-lbl">{{copyLabel('paste')}}</span></button>
          </div>
        </div>

        <div v-if="method==='terminal'" class="gb-cmd">
          <span class="ps" aria-hidden="true">$</span>
          <pre id="ob-sel-terminal">curl -fsSL {{base}}/install.sh | sh -s -- --token <span class="tokchip" :class="{full: showToken}">{{tokenShown}}</span></pre>
          <div class="gb-actions">
            <button type="button" class="ob-btn ob-copy ob-primary" :class="{copied: copied==='terminal', failed: failed==='terminal'}" @click="copy('terminal','ob-sel-terminal')"><span class="ob-lbl">{{copyLabel('terminal')}}</span></button>
          </div>
        </div>

        <div v-if="method==='cursor'" class="gb-cmd center">
          <a class="ob-btn oneclick" :href="cursorLink" @click="emit('copied','cursor')"><span class="glyph" aria-hidden="true"><svg viewBox="0 0 290 290"><rect width="140.5" height="140.5" rx="20"/><rect x="149.5" y="149.5" width="140.5" height="140.5" rx="20"/></svg></span>Add to Cursor</a>
        </div>

        <ol v-if="method==='connector'" class="gb-steps">
          <li><span>Open <b>Settings → Connectors → Add custom connector</b>.</span></li>
          <li><span>Paste the connector URL.</span>
            <button type="button" class="urlchip" :class="{copied: copied==='mcp'}" @click="copy('mcp','ob-sel-mcp')"><code id="ob-sel-mcp">{{mcpUrl}}</code><span class="ob-lbl">{{copyLabel('mcp')}}</span></button></li>
          <li><span>Click <b>Connect</b> and approve.</span></li>
        </ol>

        <div v-if="method!=='paste' && method!=='grok'" class="gb-then">
          <p><span class="ob-lead">Then ask your agent:</span> <span id="ob-sel-task" class="task">{{sayParts[0]}}<input class="task-in" :value="value" :size="Math.max(4, value.length)" aria-label="Task input" spellcheck="false" @input="emit('update:value', ($event.target as HTMLInputElement).value)">{{sayParts[1]}}</span></p>
          <button type="button" class="ob-btn ob-copy ob-ghost" :class="{copied: copied==='task'}" @click="copy('task','ob-sel-task')"><span class="ob-lbl">{{copyLabel('task')}}</span></button>
        </div>
      </div>
    </div>
  </Transition></div>
</div>
</template>
