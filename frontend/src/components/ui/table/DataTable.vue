<script setup lang="ts" generic="Row">
// A table described by its columns, on the shadcn primitives: shadcn's "Data Table" pattern without
// TanStack. The page owns the rows and the sort (v-model:sort), exactly as a TanStack table's state
// lives in the page; this component owns the layout rules every table here kept getting wrong:
// - widths are per column, so every row lines up (a row per grid could not);
// - a truncated cell ends in an ellipsis and carries its full text, never paints over its neighbour;
// - a heading can explain itself (`tip`), and a sortable one says which way it sorts;
// - below 760px a row becomes a card (`mobile` per column) instead of a table to scroll sideways.
// Cells render `cell-<key>` slots when given, else the column's `value`.
import { computed } from 'vue'
import Table from './Table.vue'
import TableHeader from './TableHeader.vue'
import TableBody from './TableBody.vue'
import TableRow from './TableRow.vue'
import TableHead from './TableHead.vue'
import TableCell from './TableCell.vue'
import TableEmpty from './TableEmpty.vue'
import type { Column, Sort } from './types'

const props = withDefaults(defineProps<{
  columns: Column<Row>[]
  rows: Row[]
  rowKey: (row: Row) => string
  sort?: Sort | null
  selected?: string | null
  /** Rows answer a click (and Enter): the page decides what that opens. */
  interactive?: boolean
  rowClass?: (row: Row) => string | Record<string, boolean> | undefined
  variant?: 'lined' | 'plain'
  surface?: boolean
  empty?: string
}>(), { sort: null, selected: null, interactive: false, variant: 'lined', surface: false, empty: 'Nothing to show.' })

const emit = defineEmits<{ 'update:sort': [Sort], 'row-click': [Row] }>()

function sortBy(col: Column<Row>) {
  const first = col.sortFirst || 'asc'
  const dir = props.sort && props.sort.key === col.key ? (props.sort.dir === 'asc' ? 'desc' : 'asc') : first
  emit('update:sort', { key: col.key, dir })
}

function ariaSort(col: Column<Row>) {
  if (!col.sortable) return undefined
  if (!props.sort || props.sort.key !== col.key) return 'none'
  return props.sort.dir === 'asc' ? 'ascending' : 'descending'
}

const widths = computed(() => props.columns.map(c => ({
  ...(c.width ? { width: c.width, maxWidth: c.width } : {}),
  ...(c.minWidth ? { minWidth: c.minWidth } : {}),
})))

function text(col: Column<Row>, row: Row) {
  const v = col.value ? col.value(row) : (row as Record<string, unknown>)[col.key]
  return v == null ? '' : String(v)
}
</script>

<template>
  <Table :variant="variant" :surface="surface" class="ui-data-table">
    <colgroup><col v-for="(c, i) in columns" :key="c.key" :style="widths[i]"></colgroup>
    <TableHeader>
      <TableRow>
        <TableHead v-for="c in columns" :key="c.key" :align="c.align" :aria-sort="ariaSort(c)">
          <slot :name="'head-' + c.key" :column="c">
            <button v-if="c.sortable" type="button" class="ui-sort" :class="{ on: sort && sort.key === c.key }"
                    :data-tip="c.tip" :data-dir="sort && sort.key === c.key ? sort.dir : undefined" @click="sortBy(c)">{{ c.header }}</button>
            <span v-else :class="{ 'ui-tip': c.tip }" :data-tip="c.tip" :tabindex="c.tip ? 0 : undefined">{{ c.header }}</span>
          </slot>
        </TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      <TableRow v-for="row in rows" :key="rowKey(row)" :interactive="interactive" :selected="selected === rowKey(row)"
                :class="rowClass && rowClass(row)" :tabindex="interactive ? 0 : undefined"
                @click="interactive && emit('row-click', row)" @keydown.enter="interactive && emit('row-click', row)">
        <TableCell v-for="(c, i) in columns" :key="c.key" :align="c.align" :style="widths[i]"
                   :data-label="c.header" :data-mobile="c.mobile || 'field'" :data-wrap="c.wrap ? '' : undefined">
          <span v-if="c.truncate" class="ui-trunc" :style="c.width ? { maxWidth: c.width } : undefined" :title="text(c, row)"><slot :name="'cell-' + c.key" :row="row" :value="text(c, row)">{{ text(c, row) }}</slot></span>
          <slot v-else :name="'cell-' + c.key" :row="row" :value="text(c, row)">{{ text(c, row) }}</slot>
        </TableCell>
      </TableRow>
      <TableEmpty v-if="!rows.length" :colspan="columns.length"><slot name="empty">{{ empty }}</slot></TableEmpty>
    </TableBody>
  </Table>
</template>
