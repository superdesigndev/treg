<script>
import { useDashboard } from '../state/context'
import OwnToolsHeader from '../components/OwnToolsHeader.vue'
import ProviderLogo from '../components/ProviderLogo.vue'
import FilterBox from '../components/FilterBox.vue'
import { DataTable } from '../components/ui/table'

const COLUMNS = [
  { key: 'name', header: 'Name', mobile: 'primary', wrap: true },
  { key: 'kind', header: 'Type' },
  { key: 'provider', header: 'Provider' },
  { key: 'by', header: 'Added by' },
  { key: 'created', header: 'Created' },
  { key: 'acts', header: '', align: 'right', mobile: 'wide' },
]
export default { components: { OwnToolsHeader, ProviderLogo, FilterBox, DataTable }, setup: useDashboard, data: () => ({ columns: COLUMNS }) }
</script>

<template>
<div class="pl own">
  <OwnToolsHeader current="resources">
    Reusable items created for this team through treg's platform providers. Resources made with your own account stay with that account.
  </OwnToolsHeader>

  <div v-if="teamResourceErr" class="banner err cn-banner"><span>{{teamResourceErr}}</span><button class="btn sm ico" @click="teamResourceErr=''" aria-label="Dismiss">✕</button></div>

  <div class="cn-filters">
    <FilterBox v-model="q" label="Search team resources" placeholder="Search team resources" :register="el => setElement('search', el)" />
    <select class="pl-select" v-model="teamResourceProvider" @change="teamResourcePage=1" aria-label="Provider">
      <option value="">All providers</option><option v-for="p in teamResourceProviders" :key="p" :value="p">{{p}}</option>
    </select>
    <select class="pl-select" v-model="teamResourceKind" @change="teamResourcePage=1" aria-label="Resource type">
      <option value="">All resource types</option><option v-for="k in teamResourceKinds" :key="k" :value="k">{{k}}</option>
    </select>
    <button class="pl-btn ghost pl-push" :disabled="teamResourceBusy" @click="loadTeamResources">{{teamResourceBusy?'Refreshing…':'Refresh'}}</button>
  </div>

  <p v-if="teamResourceBusy" class="pl-empty">Loading team resources…</p>
  <section v-else-if="pagedTeamResources.length" class="pl-sec">
    <h2 class="pl-h"><span>Resources</span><i></i><em>{{filteredTeamResources.length}}</em></h2>
    <DataTable :columns="columns" :rows="pagedTeamResources" :row-key="r => String(r.id)" surface>
      <template #cell-name="{ row: r }"><b>{{r.display_name||'Untitled resource'}}</b><span class="pl-sub mono" :title="r.upstream_id">{{r.upstream_id}}</span></template>
      <template #cell-kind="{ row: r }"><span class="muted">{{r.kind}}</span></template>
      <template #cell-provider="{ row: r }"><span class="pl-by"><ProviderLogo :service="r.provider" />{{r.provider}}</span></template>
      <template #cell-by="{ row: r }"><span class="muted">{{r.created_by ? short(r.created_by) : '-'}}</span></template>
      <template #cell-created="{ row: r }"><span class="muted">{{r.created_at ? when(r.created_at) : '-'}}</span></template>
      <template #cell-acts="{ row: r }"><span class="cn-links">
        <button v-if="r.provider==='fishaudio'&&r.kind==='voice'" @click="useFishVoice(r)">Use in TTS</button>
        <button @click="copyTeamResourceId(r)">{{teamResourceCopied===r.id?'Copied':'Copy ID'}}</button>
        <template v-if="r.provider==='fishaudio'&&r.kind==='voice'"><button @click="beginRenameFishVoice(r)">Rename</button>
        <button class="cn-del" @click="beginDeleteFishVoice(r)">Delete</button></template>
      </span></template>
    </DataTable>
    <div v-if="teamResourcePageCount>1" class="own-pager">
      <span class="pl-meta">Page {{teamResourceCurrentPage}} of {{teamResourcePageCount}}</span>
      <button class="pl-btn sm ghost" :disabled="teamResourceCurrentPage<=1" @click="teamResourcePage=teamResourceCurrentPage-1">Previous</button>
      <button class="pl-btn sm ghost" :disabled="teamResourceCurrentPage>=teamResourcePageCount" @click="teamResourcePage=teamResourceCurrentPage+1">Next</button>
    </div>
  </section>
  <p v-else class="pl-empty">{{teamResources.length?'Nothing matches these filters.':'No team resources yet. Create a private voice through Fish Audio and it will appear here.'}}</p>
</div>
</template>
