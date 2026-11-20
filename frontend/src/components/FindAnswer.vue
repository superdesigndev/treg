<script>
import { useDashboard } from '../state/context'
import { DataTable } from './ui/table'

// The answer to a described job (state/find.js): one row per job, best fit first, weaker fits in a
// lighter tone. A table (components/ui/table), so every row's columns line up whatever a price says.
// An auto answer (a typing pause) sits above the page it did not replace, so while it reads, and when
// it has nothing, it is one line: an empty answer never takes over the page. A shelf's answer read
// that shelf only, so an empty one says so and offers the whole catalog instead of calling it a gap.
// Clearing the search box is how you leave.
export default {
  components: { DataTable },
  setup: useDashboard,
  computed: {
    nothing(){ return this.find.verdict==='none' || (this.find.verdict==='keyword' && !this.findGroups.length); },
    empty(){ return this.nothing || !this.findGroups.length; },
    // An auto answer that found nothing worth a table: one line, not a panel.
    quietNothing(){ return this.find.auto && this.empty; },
    columns(){ return [
      {key:'what', header:'Tool', mobile:'primary', wrap:true},
      {key:'provs', header:'Providers', mobile:'hide'},
      {key:'price', header:'Price', align:'right', width:'180px', truncate:true, value:g=>this.findPrice(g)},
      {key:'fit', header:'Fit', align:'right', width:'76px', mobile:'hide',
       tip:'How well the relevance judge thinks this does what you described.'},
      {key:'copy', header:'', align:'right', width:'132px', mobile:'hide'}]; },
  },
}
</script>

<template>
<section class="fa" :class="{auto:find.auto}" aria-live="polite" :aria-busy="findBusy">
  <p v-if="findBusy && find.auto" class="fa-line"><i class="fa-dot" aria-hidden="true"></i>Looking for tools that do <b>{{find.q}}</b>…</p>

  <div v-else-if="findBusy" aria-label="Finding tools">
    <div v-for="i in 3" :key="i" class="fa-skel"><span></span><span></span><span></span></div>
  </div>

  <p v-else-if="find.phase==='error'" :class="find.auto ? 'fa-line' : 'fa-note'">{{find.error}}
    <button class="fa-link" type="button" @click="findRun(find.q)">Try again</button></p>

  <p v-else-if="find.phase==='done' && find.scope && empty" class="fa-line">
    Nothing in {{platLabel}} for <b>“{{find.q}}”</b>.
    <button class="fa-link" type="button" @click="findEverywhere()">Search all tools</button></p>

  <p v-else-if="find.phase==='done' && quietNothing" class="fa-line">
    <template v-if="find.verdict==='none' && find.reason">{{findNoneText()}}</template>
    <template v-else>No tool in the catalog does <b>{{find.q}}</b> yet.</template></p>

  <p v-else-if="find.phase==='done' && nothing" class="fa-note">
    <template v-if="find.verdict==='none' && find.reason">{{findNoneText()}}
      <template v-if="find.reason==='gap'"> <button class="fa-link" type="button" @click="findRequestTool()">Request it</button> to move it up.</template></template>
    <template v-else>Nothing in the catalog does this yet.
      <button class="fa-link" type="button" @click="findRequestTool()">Request it</button> and it steers what we add next.</template></p>

  <template v-else-if="find.phase==='done'">
    <div class="fa-head">
      <span>{{findGroups.length}} {{find.verdict==='keyword' ? 'keyword match' : 'tool'}}{{findGroups.length===1 ? '' : (find.verdict==='keyword' ? 'es' : 's')}} for <b>{{find.q}}</b></span>
      <button class="fa-link" type="button" @click="findCopyAll()">
        {{findCopied==='all' ? 'Copied' : 'Copy for your agent'}}</button>
    </div>
    <p v-if="find.verdict==='closest'" class="fa-sub">Nothing fits closely. These come nearest.
      <button v-if="!find.scope" class="fa-link" type="button" @click="findRequestTool()">Request a better tool</button>
      <button v-else class="fa-link" type="button" @click="findEverywhere()">Search all tools</button></p>

    <DataTable :columns="columns" :rows="findGroups" :row-key="g => g.key" interactive
               :row-class="g => ({ weak: findWeak(g) })" @row-click="g => findOpen(g, findGroups.indexOf(g)+1)">
      <template #cell-what="{ row: g }"><span class="fa-main">
        <span class="fa-logo" :class="{gen:platLogoBad[g.platform]}"
              :style="platLogoBad[g.platform] ? {background:platTileBg(g.platform)} : null">
          <img v-if="!platLogoBad[g.platform]" :src="'/logos/platforms/'+g.platform+'.svg'" alt="" @error="platLogoBad[g.platform]=true">
          <span v-else>{{platInitial({label:g.platform_label, slug:g.platform})}}</span>
        </span>
        <span class="fa-what"><b>{{g.label}}</b><small v-if="!find.scope">{{platShort(g.platform_label)}}</small></span>
      </span></template>
      <template #cell-provs="{ row: g }"><span class="fa-provs" :title="g.rows.map(r=>r.provider_display||r.provider).join(', ')">
        <span class="fa-stack"><img v-for="p in findProviders(g).slice(0,3)" :key="p" :src="'/logos/'+p+'.svg'" alt=""
             @error="$event.target.style.visibility='hidden'"></span>
        {{findProvidersText(g)}}</span></template>
      <template #cell-price="{ value }"><span class="fa-price">{{value}}</span></template>
      <template #cell-fit="{ row: g }"><span v-if="g.p!=null" class="fa-fit" :title="findFitTitle(g)">
        <i :style="{width:Math.round(g.p*100)+'%'}"></i></span></template>
      <template #cell-copy="{ row: g }"><button class="fa-copy" type="button" @click.stop="findCopy([g], g.key)" @keydown.enter.stop>
        {{findCopied===g.key ? 'Copied' : 'Copy for agent'}}</button></template>
    </DataTable>
  </template>
</section>
</template>

<style scoped>
.fa{margin:0 0 30px;font-family:var(--sans)}
.fa-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:0 0 8px;font-size:13.5px;color:var(--muted)}
.fa-head b{color:var(--ink);font-weight:500}
.fa-sub,.fa-note{margin:0 0 8px;font-size:13.5px;color:var(--muted)}
.fa-note{padding:14px 0}
/* An auto answer's one line: while it reads, and when it found nothing. */
.fa.auto{margin-bottom:22px}
.fa-line{margin:0;padding:4px 0;font-size:13.5px;line-height:1.5;color:var(--muted)}
.fa-line b{color:var(--ink);font-weight:500}
.fa-dot{display:inline-block;vertical-align:middle;margin:0 9px 2px 0;width:6px;height:6px;border-radius:50%;
  background:var(--muted);animation:fa-pulse 1s ease-in-out infinite alternate}
@keyframes fa-pulse{from{opacity:.25}}
.fa-link{border:0;background:none;padding:0;font:inherit;font-size:13px;color:var(--ink);text-decoration:underline;text-underline-offset:3px;
  text-decoration-color:var(--line2,var(--line));cursor:pointer}
.fa-link:hover{text-decoration-color:currentColor}
/* The layout is the table's (components/ui/table); these style what sits in its cells. */
:deep(.ui-tbody .ui-tr){animation:fa-in .3s both}
@keyframes fa-in{from{opacity:0}}
.fa-main{display:flex;align-items:center;gap:14px;min-width:0}
.fa-logo{flex:0 0 36px;width:36px;height:36px;border-radius:10px;background:#fff;border:1px solid var(--line);display:grid;place-items:center;color:#fff;font-weight:600;font-size:14px}
.fa-logo img{width:21px;height:21px;object-fit:contain}
.fa-what{display:flex;flex-direction:column;gap:1px;min-width:0}
.fa-what b{font-weight:500;font-size:14.5px;line-height:1.3}
.fa-what small{font-size:12.5px;color:var(--muted)}
.fa-provs{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);white-space:nowrap}
.fa-stack{display:flex;padding-left:6px}
.fa-stack img{width:20px;height:20px;margin-left:-6px;border-radius:6px;background:#fff;border:1.5px solid var(--bg);object-fit:contain;padding:1px}
/* A rate can be a phrase ("$0.025/started 10 emails"): the price column is 180px on every row, and
   anything longer ends in an ellipsis with the whole rate on hover (the DataTable's `truncate`). */
.fa-price{font-family:var(--mono);font-size:12px;color:var(--muted)}
.fa-fit{display:block;width:56px;margin-left:auto;height:4px;border-radius:2px;background:var(--line);overflow:hidden}
.fa-fit i{display:block;height:100%;background:var(--ink);border-radius:2px}
.weak .fa-what b,.weak .fa-logo{opacity:.62}
.weak .fa-fit i{background:var(--muted)}
.fa-copy{border:1px solid var(--line2,var(--line));background:var(--surface,var(--panel));white-space:nowrap;
  color:var(--ink);border-radius:8px;padding:5px 11px;font:inherit;font-size:12.5px;cursor:pointer;opacity:0;transition:opacity .15s}
.ui-tr:hover .fa-copy,.fa-copy:focus-visible{opacity:1}
.fa-skel{display:grid;grid-template-columns:36px minmax(0,1fr) 150px;gap:16px;align-items:center;padding:12px 8px;border-bottom:1px solid var(--line)}
.fa-skel span{height:12px;border-radius:6px;background:linear-gradient(90deg,var(--line),var(--hover,var(--panel2)),var(--line));background-size:200% 100%;animation:fa-sh 1.2s linear infinite}
.fa-skel span:first-child{height:36px;border-radius:10px}
@keyframes fa-sh{to{background-position:-200% 0}}
@media (hover:none){.fa-copy{opacity:1}}
@media (prefers-reduced-motion:reduce){:deep(.ui-tbody .ui-tr),.fa-skel span,.fa-dot{animation:none}}
</style>
