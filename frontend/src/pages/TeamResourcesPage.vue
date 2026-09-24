<script>
import { useDashboard } from '../state/context'
export default { setup: useDashboard }
</script>

<template>
<div v-if="teamResourceErr" class="banner err" style="max-width:900px;margin-bottom:14px;display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
            <span>{{teamResourceErr}}</span><button class="btn sm ico" @click="teamResourceErr=''" aria-label="Dismiss">✕</button>
          </div>
          <div class="tut-head">
            <div><h1>Team resources</h1>
              <div class="tabs" style="margin:8px 0 4px">
                <button @click="go('tools')">Skills &amp; tools</button>
                <button v-if="canRegister" @click="go('secrets')">Secrets</button>
                <button class="active">Team resources</button>
              </div>
              <p class="sub" style="margin:0">Reusable items created for this team through treg's platform providers. BYOK account resources stay with their provider account.</p></div>
            <div class="tut-actions"><button class="btn sm" :disabled="teamResourceBusy" @click="loadTeamResources">{{teamResourceBusy?'…':'Refresh'}}</button></div>
          </div>
          <div class="field" style="max-width:900px;flex-wrap:wrap;margin:12px 0">
            <select class="msel" v-model="teamResourceProvider" @change="teamResourcePage=1">
              <option value="">All providers</option><option v-for="p in teamResourceProviders" :key="p" :value="p">{{p}}</option>
            </select>
            <select class="msel" v-model="teamResourceKind" @change="teamResourcePage=1">
              <option value="">All resource types</option><option v-for="k in teamResourceKinds" :key="k" :value="k">{{k}}</option>
            </select>
            <span class="sub" style="margin:0">{{filteredTeamResources.length}} resource{{filteredTeamResources.length===1?'':'s'}}</span>
          </div>
          <p v-if="teamResourceBusy" class="sub">Loading team resources…</p>
          <div v-else-if="pagedTeamResources.length" class="ttable-wrap"><table class="ttable">
            <thead><tr><th>Name</th><th>Type</th><th>Provider</th><th>Created by</th><th>Created</th><th></th></tr></thead>
            <tbody><tr v-for="r in pagedTeamResources" :key="r.id">
              <td class="tn"><b>{{r.display_name||'Untitled resource'}}</b><span class="mono muted" style="font-size:11px">{{r.upstream_id}}</span></td>
              <td class="ta"><span class="chip">{{r.kind}}</span></td>
              <td>{{r.provider}}</td>
              <td class="th">{{r.created_by?short(r.created_by):'—'}}</td>
              <td class="th">{{r.created_at?when(r.created_at):'—'}}</td>
              <td class="tx">
                <button class="btn sm" @click="copyTeamResourceId(r)">{{teamResourceCopied===r.id?'✓ copied':'Copy ID'}}</button>
                <button v-if="r.provider==='fishaudio'&&r.kind==='voice'" class="btn sm" @click="useFishVoice(r)">Use in TTS</button>
                <button v-if="r.provider==='fishaudio'&&r.kind==='voice'" class="btn sm" @click="beginRenameFishVoice(r)">Rename</button>
                <button v-if="r.provider==='fishaudio'&&r.kind==='voice'" class="btn sm danger" @click="beginDeleteFishVoice(r)">Delete</button>
              </td>
            </tr></tbody>
          </table></div>
          <div v-else class="mk-empty" style="margin-top:14px">{{teamResources.length?'Nothing matches these filters.':'No team resources yet. Create a private voice through Fish Audio and it will appear here.'}}</div>
          <div v-if="teamResourcePageCount>1" style="display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-top:12px">
            <span class="sub" style="margin:0">Page {{teamResourceCurrentPage}} of {{teamResourcePageCount}}</span>
            <button class="btn sm" :disabled="teamResourceCurrentPage<=1" @click="teamResourcePage=teamResourceCurrentPage-1">← Previous</button>
            <button class="btn sm" :disabled="teamResourceCurrentPage>=teamResourcePageCount" @click="teamResourcePage=teamResourceCurrentPage+1">Next →</button>
          </div>
</template>
