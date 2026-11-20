<script>
import { useDashboard } from '../state/context'
import OwnToolsHeader from '../components/OwnToolsHeader.vue'
import LoadFromMachine from '../components/LoadFromMachine.vue'
import { DataTable } from '../components/ui/table'

const COLUMNS = [
  { key: 'name', header: 'Name', mobile: 'primary' },
  { key: 'kind', header: 'Kind' },
  { key: 'owner', header: 'Added by' },
  { key: 'acts', header: '', align: 'right' },
]
export default { components: { OwnToolsHeader, LoadFromMachine, DataTable }, setup: useDashboard, data: () => ({ columns: COLUMNS }) }
</script>

<template>
<div class="pl own">
  <OwnToolsHeader current="secrets">
    The credentials your own tools inject. Values are encrypted server-side and never shown.
    Keys for catalog providers live on <a href="#connections" @click.prevent="go('connections')">Connections</a><template v-if="secrets.length>ownSecrets.length"> ({{secrets.length-ownSecrets.length}} there now)</template>.
  </OwnToolsHeader>

  <div v-if="orgMsg" class="banner cn-banner"><span>{{orgMsg}}</span><button class="btn sm ico" @click="orgMsg=''" aria-label="Dismiss">✕</button></div>
  <div v-if="secretErr" class="banner err cn-banner"><span>{{secretErr}}</span><button class="btn sm ico" @click="secretErr=''" aria-label="Dismiss">✕</button></div>

  <section v-if="ownSecrets.length" class="pl-sec">
    <h2 class="pl-h"><span>Secrets</span><i></i><em>{{ownSecrets.length}}</em></h2>
    <DataTable :columns="columns" :rows="ownSecrets" :row-key="s => String(s.id)" surface>
      <template #cell-name="{ row: s }"><b class="mono">{{s.name}}</b></template>
      <template #cell-kind="{ row: s }"><span class="muted">{{s.kind}}</span></template>
      <template #cell-owner="{ row: s }"><span class="muted">{{s.owner ? short(s.owner) : '-'}}</span></template>
      <template #cell-acts="{ row: s }"><span class="cn-links"><button class="cn-del" :class="{armed:confirmDelSecret===s.id}" @click="deleteSecret(s)">
        {{confirmDelSecret===s.id ? 'Click again to delete' : 'Delete'}}</button></span></template>
    </DataTable>
  </section>

  <section class="pl-sec">
    <h2 class="pl-h"><span>Add {{secretRows.length>1?'secrets':'a secret'}}</span><i></i></h2>
    <div class="pl-card own-form">
      <p class="own-form-tip">Paste a whole <span class="mono">.env</span> into the name field and it splits into rows.</p>
          <div v-for="(row,i) in secretRows" :key="i" class="field" style="flex-wrap:wrap;margin-bottom:8px">
            <input v-model="row.name" placeholder="name, e.g. STRIPE_KEY" style="min-width:150px" @paste="pasteEnv($event,i,'name')" @keyup.enter="addSecrets"/>
            <input v-model="row.value" :type="row.kind==='param'?'text':'password'" :placeholder="row.kind==='param'?'value, e.g. a project id (not secret)':'value (encrypted server-side)'" style="min-width:190px" @paste="pasteEnv($event,i,'value')" @keyup.enter="addSecrets"/>
            <select v-model="row.kind" class="msel"><option>env</option><option>oauth</option><option>secret_file</option><option value="param">param (non-secret)</option></select>
            <button v-if="secretRows.length>1" class="btn sm ico" @click="secretRows.splice(i,1)" title="Remove row">✕</button>
            <!-- The ladder matches by NAME EQUALITY, so the hint either confirms a name the catalog
                 will find, or offers the exact one when the row is a near miss (APOLLO_API_KEY). -->
            <span v-if="secretNameHint(row)" class="sub" style="flex-basis:100%;font-size:11.5px;margin:-2px 0 0">
              <template v-if="secretNameHint(row).ok">✓ {{secretNameHint(row).text}}</template>
              <template v-else>{{secretNameHint(row).text}}
                <a href="#" @click.prevent="row.name=secretNameHint(row).service">rename to “{{secretNameHint(row).service}}”</a></template>
            </span>
          </div>
          <p class="sub" v-if="secretRows.some(r=>r.kind==='param')" style="margin:2px 0 8px">A param is non-secret config (project id, org id), injected alongside a credential into CLI env or an HTTP query.</p>
      <div class="own-form-a">
        <button class="pl-btn" @click="addSecrets" :disabled="secretBusy||!secretRowsReady">{{secretBusy?'…':(secretRowsReady>1?('Add '+secretRowsReady+' secrets'):'Add secret')}}</button>
        <button class="pl-btn ghost" @click="secretRows.push({name:'',value:'',kind:'env'})">Another row</button>
        <button v-if="secretRows.length>1" class="pl-btn ghost" @click="secretRows=[{name:'',value:'',kind:'env'}]">Clear</button>
      </div>
    </div>
  </section>

  <LoadFromMachine v-if="!ownSecrets.length" title="Or load them all from your machine">
    Bulk-load your keys (and skills): each lands here encrypted, referenced by name.
  </LoadFromMachine>
</div>
</template>
