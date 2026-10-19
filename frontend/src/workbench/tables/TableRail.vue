<script setup lang="ts">
import { computed } from 'vue'
import Inspector from '../../components/Inspector.vue'
import StatusPill from '../../components/StatusPill.vue'
import type { Run, Table } from '../api/client'
import { ago, count, usd } from '../format'

const props = defineProps<{ table: Table, runs: Run[] }>()
const filled = computed(() => props.table.columns.filter(c => c.filled_by).length)
const source = computed(() => {
  const s = props.table.source as Record<string, any> | null
  if (!s) return ''
  if (s.file) return `imported from ${s.file}`
  const body = s.body ? Object.values(s.body).filter(v => typeof v === 'string').join(', ') : ''
  return [s.tool, body, s.page ? `page ${s.page}` : ''].filter(Boolean).join(' · ')
})
</script>

<template>
<Inspector title="This table">
  <dl class="kv" style="margin:0">
    <dt>Rows</dt><dd class="num">{{ table.row_count.toLocaleString() }}</dd>
    <dt>Columns</dt><dd class="num">{{ table.columns.length }}<template v-if="filled"> · {{ filled }} filled by tools</template></dd>
    <dt v-if="table.bound_by?.length">Bound by</dt><dd v-if="table.bound_by?.length"><template v-for="(b, i) in table.bound_by" :key="b"><a :href="`#tools/i/${b}`">{{ b }}</a><template v-if="i < table.bound_by.length - 1">, </template></template></dd>
    <dt v-if="source">Source</dt><dd v-if="source">{{ source }}</dd>
  </dl>
  <div v-if="runs.length">
    <h4>Recent runs</h4>
    <div class="wb-timeline">
      <div v-for="r in runs.slice(0, 6)" :key="r.id" class="ev">
        <time>{{ ago(r.started_at) }}</time>
        <span><a :href="`#workbench/runs/${r.id}`">{{ r.install || r.tool }}</a> · {{ r.started_by }} · {{ count(r.progress.completed, 'row') }} · {{ usd(r.spent_micro) }} <StatusPill :status="r.status" /></span>
      </div>
    </div>
  </div>
  <p class="wb-hint">Click a cell to see what filled it. Click a column header to run, edit or remove it.</p>
</Inspector>
</template>
