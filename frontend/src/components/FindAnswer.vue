<script>
import { useDashboard } from '../state/context'

const SHOWN = 8   // rows before "Show more"

// The Catalog page's answer to a described job (state/find.js): one quiet list, one row per job.
// Strong fits first at full weight, weaker ones after them in a lighter tone. No labels for the
// buckets: the order and the fit bar already say it. Clearing the search box is how you leave.
export default {
  setup: useDashboard,
  data(){ return { all:false } },
  computed: {
    shown(){ return this.all ? this.findGroups : this.findGroups.slice(0, SHOWN); },
    nothing(){ return this.find.verdict==='none' || (this.find.verdict==='keyword' && !this.findGroups.length); },
  },
  watch: { 'find.q'(){ this.all=false; } },
  methods: {
    providers(g){ return [...new Set(g.rows.map(r=>r.provider))]; },
  },
}
</script>

<template>
<section class="fa" aria-live="polite" :aria-busy="findBusy">
  <div v-if="findBusy" class="fa-list" aria-label="Finding tools">
    <div v-for="i in 3" :key="i" class="fa-skel"><span></span><span></span><span></span></div>
  </div>

  <p v-else-if="find.phase==='error'" class="fa-note">{{find.error}}
    <button class="fa-link" type="button" @click="findRun(find.q)">Try again</button></p>

  <p v-else-if="find.phase==='done' && nothing" class="fa-note">
    Nothing in the catalog does this yet.
    <button class="fa-link" type="button" @click="findRequestTool()">Request it</button> and it steers what we add next.</p>

  <template v-else-if="find.phase==='done'">
    <div class="fa-head">
      <span>{{findGroups.length}} {{find.verdict==='keyword' ? 'keyword match' : 'tool'}}{{findGroups.length===1 ? '' : (find.verdict==='keyword' ? 'es' : 's')}} for <b>{{find.q}}</b></span>
      <button class="fa-link" type="button" @click="findCopyAll()">
        {{findCopied==='all' ? 'Copied' : 'Copy for your agent'}}</button>
    </div>
    <p v-if="find.verdict==='closest'" class="fa-sub">Nothing fits closely. These come nearest.
      <button class="fa-link" type="button" @click="findRequestTool()">Request a better tool</button></p>

    <ul class="fa-list">
      <li v-for="g in shown" :key="g.key" class="fa-row" :class="{weak:findWeak(g)}">
        <button class="fa-main" type="button" @click="findOpen(g)">
          <span class="fa-logo" :class="{gen:platLogoBad[g.platform]}"
                :style="platLogoBad[g.platform] ? {background:platTileBg(g.platform)} : null">
            <img v-if="!platLogoBad[g.platform]" :src="'/logos/platforms/'+g.platform+'.svg'" alt="" @error="platLogoBad[g.platform]=true">
            <span v-else>{{platInitial({label:g.platform_label, slug:g.platform})}}</span>
          </span>
          <span class="fa-what"><b>{{g.label}}</b><small>{{platShort(g.platform_label)}}</small></span>
          <span class="fa-provs" :title="g.rows.map(r=>r.provider_display||r.provider).join(', ')">
            <span class="fa-stack"><img v-for="p in providers(g).slice(0,3)" :key="p" :src="'/logos/'+p+'.svg'" alt=""
                 @error="$event.target.style.visibility='hidden'"></span>
            {{providers(g).length}} provider{{providers(g).length===1?'':'s'}}
          </span>
          <span class="fa-price">{{findPrice(g)}}</span>
          <span v-if="g.p!=null" class="fa-fit" :title="'Fit for this job: '+Math.round(g.p*100)+'%'">
            <i :style="{width:Math.round(g.p*100)+'%'}"></i></span>
        </button>
        <button class="fa-copy" type="button" @click="findCopy([g], g.key)">
          {{findCopied===g.key ? 'Copied' : 'Copy for agent'}}</button>
      </li>
    </ul>
    <button v-if="findGroups.length>shown.length" class="fa-link fa-more" type="button" @click="all=true">
      Show {{findGroups.length-shown.length}} more</button>
  </template>
</section>
</template>

<style scoped>
.fa{margin:0 0 30px;font-family:var(--sans)}
.fa-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:0 0 8px;font-size:13.5px;color:var(--muted)}
.fa-head b{color:var(--ink);font-weight:500}
.fa-sub,.fa-note{margin:0 0 8px;font-size:13.5px;color:var(--muted)}
.fa-note{padding:14px 0}
.fa-link{border:0;background:none;padding:0;font:inherit;font-size:13px;color:var(--ink);text-decoration:underline;text-underline-offset:3px;
  text-decoration-color:var(--line2,var(--line));cursor:pointer}
.fa-link:hover{text-decoration-color:currentColor}
.fa-list{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
.fa-row{position:relative;border-bottom:1px solid var(--line);animation:fa-in .3s both}
.fa-row:nth-child(2){animation-delay:30ms}.fa-row:nth-child(3){animation-delay:60ms}.fa-row:nth-child(4){animation-delay:90ms}
.fa-row:nth-child(5){animation-delay:120ms}.fa-row:nth-child(n+6){animation-delay:150ms}
@keyframes fa-in{from{opacity:0}}
.fa-main{width:100%;display:grid;grid-template-columns:36px minmax(0,1fr) 150px 120px 56px;align-items:center;gap:16px;
  padding:12px 140px 12px 8px;border:0;background:none;text-align:left;color:var(--ink);cursor:pointer;border-radius:10px;font:inherit}
.fa-main:hover{background:var(--hover,rgba(0,0,0,.035))}
.fa-logo{width:36px;height:36px;border-radius:10px;background:#fff;border:1px solid var(--line);display:grid;place-items:center;color:#fff;font-weight:600;font-size:14px}
.fa-logo img{width:21px;height:21px;object-fit:contain}
.fa-what{display:flex;flex-direction:column;gap:1px;min-width:0}
.fa-what b{font-weight:500;font-size:14.5px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fa-what small{font-size:12.5px;color:var(--muted)}
.fa-provs{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);white-space:nowrap}
.fa-stack{display:flex;padding-left:6px}
.fa-stack img{width:20px;height:20px;margin-left:-6px;border-radius:6px;background:#fff;border:1.5px solid var(--bg);object-fit:contain;padding:1px}
.fa-price{font-family:var(--mono);font-size:12px;color:var(--muted);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.fa-fit{height:4px;border-radius:2px;background:var(--line);overflow:hidden}
.fa-fit i{display:block;height:100%;background:var(--ink);border-radius:2px}
.fa-row.weak .fa-what b,.fa-row.weak .fa-logo{opacity:.62}
.fa-row.weak .fa-fit i{background:var(--muted)}
.fa-copy{position:absolute;right:8px;top:50%;transform:translateY(-50%);border:1px solid var(--line2,var(--line));background:var(--surface,var(--panel));
  color:var(--ink);border-radius:8px;padding:5px 11px;font:inherit;font-size:12.5px;cursor:pointer;opacity:0;transition:opacity .15s}
.fa-row:hover .fa-copy,.fa-copy:focus-visible{opacity:1}
.fa-more{margin-top:10px}
.fa-skel{display:grid;grid-template-columns:36px minmax(0,1fr) 150px;gap:16px;align-items:center;padding:12px 8px;border-bottom:1px solid var(--line)}
.fa-skel span{height:12px;border-radius:6px;background:linear-gradient(90deg,var(--line),var(--hover,var(--panel2)),var(--line));background-size:200% 100%;animation:fa-sh 1.2s linear infinite}
.fa-skel span:first-child{height:36px;border-radius:10px}
@keyframes fa-sh{to{background-position:-200% 0}}
@media (max-width:760px){
  .fa-main{grid-template-columns:36px minmax(0,1fr) auto;padding-right:8px}
  .fa-provs,.fa-fit{display:none}
  .fa-copy{display:none}
}
@media (hover:none){.fa-copy{opacity:1}}
@media (prefers-reduced-motion:reduce){.fa-row,.fa-skel span{animation:none}}
</style>
