<script setup lang="ts">
import type { Row, Schemas } from '../workbench/api/client'

type ColumnMetadata = Schemas['ColumnMetadata']
type Attempt = NonNullable<NonNullable<Row['meta']>[string]['latest_attempt']>

const props = defineProps<{
  columns: ColumnMetadata[]
  rows: Row[]
  picked?: { row: string, column: string } | null
  addColumn?: boolean
}>()
const emit = defineEmits<{ pick: [row: string, column: string], 'add-column': [] }>()

function attempt(row: Row, col: ColumnMetadata): Attempt | null {
  if (!col.filled_by || !row.meta) return null
  const own = row.meta[col.id]
  if (own?.latest_attempt) return own.latest_attempt
  const group = Object.values(row.meta).find(m => m.value_source?.install === col.filled_by || m.latest_attempt)
  return group?.latest_attempt || null
}

function show(value: unknown): string {
  if (value == null) return ''
  if (value === true) return '✓'
  if (value === false) return '✗'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

const STATE_LABEL: Record<string, string> = { queued: 'Queued', running: 'Running', no_result: 'No result', failed: 'Error' }

function chip(row: Row, col: ColumnMetadata): { label: string, tone: string } | null {
  if (row.cells[col.id] != null) return null
  const a = attempt(row, col)
  if (!a || a.status === 'succeeded') return null
  return { label: STATE_LABEL[a.status] || a.status, tone: a.status }
}

const isPicked = (row: Row, col: ColumnMetadata) => props.picked?.row === row.id && props.picked?.column === col.id
</script>

<template>
<div class="data-grid-wrap" role="region" aria-label="Rows">
  <table class="data-grid">
    <thead>
      <tr>
        <th class="num" scope="col">#</th>
        <th v-for="c in columns" :key="c.id" scope="col" :class="{ job: c.filled_by }">
          <span class="label">{{ c.name }}</span>
          <span v-if="c.type && c.type !== 'text'" class="type">{{ c.type.replace(/_/g, ' ') }}</span>
        </th>
        <th v-if="addColumn" class="add" scope="col"><button type="button" @click="emit('add-column')">+ Add column</button></th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="(r, i) in rows" :key="r.id">
        <td class="num">{{ i + 1 }}</td>
        <td v-for="c in columns" :key="c.id" class="cell" :class="{ picked: isPicked(r, c) }" :title="show(r.cells[c.id])"
            @click="emit('pick', r.id, c.id)">
          <span v-if="chip(r, c)" class="chip" :class="chip(r, c)!.tone">{{ chip(r, c)!.label }}</span>
          <span v-else-if="r.cells[c.id] == null" class="empty">·</span>
          <template v-else>{{ show(r.cells[c.id]) }}</template>
        </td>
        <td v-if="addColumn" class="add"></td>
      </tr>
    </tbody>
  </table>
</div>
</template>

<style>
.data-grid-wrap{width:100%;min-width:0;max-height:calc(100vh - 160px);overflow:auto;background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow-sm),0 0 0 1px var(--line)}
.data-grid{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;font-size:13px}
.data-grid th,.data-grid td{border-bottom:1px solid var(--line);border-right:1px solid var(--line);padding:0 10px;height:34px;text-align:left;max-width:260px;min-width:120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.data-grid th{position:sticky;top:0;background:var(--panel);font-weight:500;z-index:2;user-select:none;color:var(--muted)}
.data-grid th.job{color:var(--ink)}
.data-grid th.job .label::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--teal);margin-right:6px;vertical-align:0}
.data-grid th .type{margin-left:6px;font-size:10px;font-weight:400;color:var(--muted);background:var(--panel2);border-radius:999px;padding:1px 6px}
.data-grid td.num,.data-grid th.num{color:var(--muted);font-variant-numeric:tabular-nums;min-width:44px;width:44px;text-align:right}
.data-grid tbody tr:hover td{background:var(--hover)}
.data-grid td.cell{cursor:pointer}
.data-grid td.picked{outline:2px solid var(--teal);outline-offset:-2px}
.data-grid td .empty{color:var(--muted2)}
.data-grid .add{min-width:140px;border-right:0;border-left:1px dashed var(--line)}
.data-grid th.add button{border:0;background:none;color:var(--teal);font:inherit;font-weight:500;padding:4px 0;cursor:pointer}
.data-grid .chip{display:inline-flex;align-items:center;font-size:11px;padding:1px 8px;border-radius:999px;background:var(--panel2);color:var(--muted)}
.data-grid .chip.queued,.data-grid .chip.running{background:color-mix(in srgb,var(--teal) 12%,transparent);color:var(--teal)}
.data-grid .chip.failed{background:color-mix(in srgb,var(--red) 10%,transparent);color:var(--red)}
.data-grid .chip.no_result{color:var(--amber);background:color-mix(in srgb,var(--amber) 12%,transparent)}
</style>
