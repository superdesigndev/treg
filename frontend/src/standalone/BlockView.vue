<script setup lang="ts">
import { computed, ref } from 'vue'
import { label } from './form'
import { download, safeHref, shortUrl, toCsv, type Block } from './render'

const props = defineProps<{ block: Block, nested?: boolean }>()
const title = computed(() => label(props.block.label))
const sortBy = ref(-1)
const sortDir = ref(1)
const open = ref<Set<string>>(new Set())
const LONG = 90        // characters past which a cell is clamped to a few lines until clicked

function toggle(key: string) {
  const s = new Set(open.value)
  if (s.has(key)) s.delete(key); else s.add(key)
  open.value = s
}
const href = (v: string) => (/^https?:\/\//i.test(v) ? safeHref(v) : null)

const rows = computed(() => {
  const b = props.block
  if (b.kind !== 'table') return []
  if (sortBy.value < 0) return b.rows
  const i = sortBy.value
  return [...b.rows].sort((x, y) => {
    const a = x[i], c = y[i]
    const na = Number(a.replace(/,/g, '')), nc = Number(c.replace(/,/g, ''))
    const cmp = a !== '' && c !== '' && Number.isFinite(na) && Number.isFinite(nc) ? na - nc : a.localeCompare(c)
    return cmp * sortDir.value
  })
})

function sort(i: number) {
  if (sortBy.value === i) sortDir.value = -sortDir.value
  else { sortBy.value = i; sortDir.value = 1 }
}

function csv() {
  const b = props.block
  if (b.kind === 'table') download(`${b.label}.csv`, toCsv(b.columns, rows.value), 'text/csv')
}

async function copy(text: string) {
  try { await navigator.clipboard.writeText(text) } catch { /* the value stays on screen */ }
}
</script>

<template>
  <div v-if="block.kind === 'tiles'" class="sa-tiles">
    <button v-for="t in block.items" :key="t.label" type="button" class="sa-tile" :title="`Copy ${t.text}`" @click="copy(t.text)">
      <span class="sa-tile-label">{{ label(t.label) }}</span>
      <span class="sa-tile-value">{{ t.text }}</span>
    </button>
  </div>
  <section v-else class="sa-block" :class="{ nested }">
    <header class="sa-block-head">
      <h3>{{ title }}</h3>
      <div class="sa-block-actions">
        <button v-if="block.kind === 'table'" class="sa-btn sm" type="button" @click="csv">Download CSV</button>
        <button v-if="block.kind === 'value'" class="sa-btn sm" type="button" @click="copy(block.text)">Copy</button>
      </div>
    </header>
    <p v-if="block.kind === 'empty'" class="sa-muted">No value</p>
    <p v-else-if="block.kind === 'value'" class="sa-value">{{ block.text }}</p>
    <p v-else-if="block.kind === 'link'"><a :href="block.href" target="_blank" rel="noopener noreferrer">{{ block.href }}</a></p>
    <a v-else-if="block.kind === 'image'" :href="block.src" target="_blank" rel="noopener noreferrer">
      <img class="sa-image" :src="block.src" :alt="title" loading="lazy" referrerpolicy="no-referrer"/>
    </a>
    <ul v-else-if="block.kind === 'list'" class="sa-list"><li v-for="(item, i) in block.items" :key="i">{{ item }}</li></ul>
    <div v-else-if="block.kind === 'table'">
      <p class="sa-muted sa-count">{{ block.rows.length }} row{{ block.rows.length === 1 ? '' : 's' }}</p>
      <div class="sa-table-wrap">
      <table class="sa-table">
        <thead><tr>
          <th v-for="(c, i) in block.columns" :key="c">
            <button type="button" @click="sort(i)">{{ c }}<span v-if="sortBy === i">{{ sortDir > 0 ? ' ↑' : ' ↓' }}</span></button>
          </th>
        </tr></thead>
        <tbody><tr v-for="(r, i) in rows" :key="i">
          <td v-for="(v, j) in r" :key="j" :class="{ num: /^-?[\d,.]+%?$/.test(v) }">
            <a v-if="href(v)" :href="href(v)!" target="_blank" rel="noopener noreferrer" :title="v">{{ shortUrl(v) }}</a>
            <span v-else-if="v.length > LONG" class="sa-cell" :class="{ open: open.has(`${i}-${j}`) }" :title="open.has(`${i}-${j}`) ? '' : v"
                  role="button" tabindex="0" @click="toggle(`${i}-${j}`)" @keydown.enter="toggle(`${i}-${j}`)">{{ v }}</span>
            <template v-else>{{ v }}</template>
          </td>
        </tr></tbody>
      </table>
      </div>
    </div>
    <div v-else-if="block.kind === 'section'" class="sa-section">
      <BlockView v-for="b in block.blocks" :key="b.label" :block="b" nested/>
    </div>
    <pre v-else class="sa-json">{{ block.text }}</pre>
  </section>
</template>
