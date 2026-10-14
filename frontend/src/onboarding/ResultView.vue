<script setup lang="ts">
// One first call's answer, drawn the way that answer is read (see extract.ts). Every row is readable;
// a long answer shows its first rows and folds the rest behind "Show all".
import { computed, ref } from 'vue'
import { compact, type View } from './extract'

const props = defineProps<{ data: View }>()
const expanded = ref(false)
const all = computed<any[]>(() => ((props.data as any).rows as any[] | undefined) || [])
// videos fill one row of six, posts three rows of two
const fold = computed(() => (props.data.view === 'videos' || props.data.view === 'social' ? 6 : 5))
const rows = computed(() => (expanded.value ? all.value : all.value.slice(0, fold.value)))
const hidden = computed(() => all.value.length - rows.value.length)
const initials = (name: string) => name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase()
const d = computed(() => props.data as any)
const maxVol = computed(() => (props.data.view === 'keywords' ? Math.max(...props.data.rows.map((r) => r.vol), 1) : 1))
</script>

<template>
<div class="v-wrap" :data-view="d.view">
  <div v-if="d.view==='person'" class="v-person rin">
    <span class="ob-avatar ob-lg">{{initials(d.person.name)}}</span>
    <div class="vp-main">
      <b>{{d.person.name}}</b>
      <span v-if="d.person.title || d.person.company" class="vp-role">{{[d.person.title, d.person.company].filter(Boolean).join(' · ')}}</span>
      <span class="vp-mail"><span class="ob-mono">{{d.person.email}}</span><span class="pill" :class="d.person.verified ? 'good' : 'low'">{{d.person.verified ? 'verified' : 'found'}}</span></span>
    </div>
  </div>

  <ul v-else-if="d.view==='people'" class="v-list">
    <li v-for="(r, i) in rows" :key="i" class="rin" :style="{'--i':i}">
      <span class="ob-avatar">{{initials(r.name)}}</span>
      <span class="vl-main"><b>{{r.name}}</b><span>{{r.title}}</span></span>
      <span v-if="r.tag" class="vl-side ob-mono">{{r.tag}}</span>
    </li>
  </ul>

  <div v-else-if="d.view==='company'" class="v-company rin">
    <div class="vc-head"><span class="ob-avatar sq">{{d.company.name[0]}}</span><div><b>{{d.company.name}}</b><span class="ob-mono">{{d.company.domain}}</span></div></div>
    <dl class="vc-facts">
      <div v-for="([k, v], i) in d.company.facts" :key="k" :class="{'ob-wide': v.length > 40}"><dt>{{k}}</dt><dd>{{v}}</dd></div>
    </dl>
  </div>

  <ul v-else-if="d.view==='keywords'" class="v-kw">
    <li v-for="(r, i) in rows" :key="r.kw" class="rin" :style="{'--i':i}">
      <span class="kw">{{r.kw}}</span>
      <span class="kw-bar"><span :style="{width: Math.max(3, r.vol / maxVol * 100) + '%'}"></span></span>
      <span class="kw-n ob-mono">{{r.vol.toLocaleString('en-US')}}/mo</span>
      <span class="kw-m ob-mono">{{[r.kd != null ? 'KD ' + r.kd : '', r.cpc ? '$' + r.cpc.toFixed(2) : ''].filter(Boolean).join(' · ')}}</span>
    </li>
  </ul>

  <ol v-else-if="d.view==='serp'" class="v-serp">
    <li v-for="(r, i) in rows" :key="i" :class="'rin'+(r.you ? ' ob-you' : '')" :style="{'--i':i}">
      <span class="sp-n ob-mono">{{r.pos}}</span>
      <span class="sp-main"><span class="sp-site"><span class="fav">{{r.site[0].toUpperCase()}}</span>{{r.site}}</span><span class="sp-t">{{r.title}}</span></span>
    </li>
    <li v-if="d.missing" class="rin sp-missing" :style="{'--i':d.rows.length}">
      <span class="sp-n ob-mono">–</span>
      <span class="sp-main"><span class="sp-site"><span class="fav">{{d.missing[0].toUpperCase()}}</span>{{d.missing}}</span><span class="sp-t">Not in the top {{d.rows.length}}</span></span>
    </li>
  </ol>

  <ul v-else-if="d.view==='maps'" class="v-list">
    <li v-for="(r, i) in rows" :key="i" class="rin" :style="{'--i':i}">
      <span class="ob-avatar sq pin" aria-hidden="true"></span>
      <span class="vl-main"><b>{{r.name}}</b><span>
        <template v-if="r.rating != null"><span class="stars" :style="{'--v': (r.rating / 5 * 100) + '%'}" :aria-label="r.rating + ' out of 5'"></span> {{r.rating}} <span class="ob-muted">({{r.reviews.toLocaleString('en-US')}})</span><template v-if="r.category"> · </template></template>{{r.category}}
      </span></span>
      <span class="vl-side"><span v-if="r.website" class="ob-mono">{{r.website}}</span><span v-else class="pill low">no website</span></span>
    </li>
  </ul>

  <div v-else-if="d.view==='social'" class="v-posts">
    <article v-for="(r, i) in rows" :key="i" class="post rin" :style="{'--i':i}">
      <header><span class="plat" :class="r.platform==='Reddit' ? 'rd' : ''">{{r.platform==='Reddit' ? 'r/' : '𝕏'}}</span><b>{{r.author}}</b><span class="ob-muted ob-mono">{{r.date}}</span></header>
      <p>{{r.text}}</p>
      <footer class="ob-mono">{{r.likes}}</footer>
    </article>
  </div>

  <div v-else-if="d.view==='videos'" class="v-videos">
    <figure v-for="(r, i) in rows" :key="i" class="vid rin" :style="{'--i':i}">
      <span class="vid-img"><img v-if="r.img" :src="r.img" alt="" loading="lazy" referrerpolicy="no-referrer" @error="($event.target as HTMLElement).style.visibility='hidden'"><span class="vid-views ob-mono">▶ {{compact(r.views)}}</span></span>
      <figcaption><span>{{r.desc}}</span><small class="ob-mono">{{r.author}}</small></figcaption>
    </figure>
  </div>

  <div v-else-if="d.view==='scrape'" class="v-scrape rin">
    <div class="vs-url ob-mono"><span class="fav">{{d.url.replace(/^https?:\/\//, '')[0].toUpperCase()}}</span>{{d.url.replace(/^https?:\/\//, '')}}<span class="ob-muted">→ {{d.rows.length}} rows</span></div>
    <div class="table-wrap"><table class="rtable">
      <thead><tr><th v-for="c in d.cols" :key="c" scope="col">{{c}}</th></tr></thead>
      <tbody><tr v-for="(row, i) in rows" :key="i" class="rin" :style="{'--i':i}"><td v-for="(c, j) in row" :key="j" :class="j === 1 ? 'ob-mono' : 'wrap'">{{c}}</td></tr></tbody>
    </table></div>
  </div>


  <button v-if="hidden > 0 || expanded" type="button" class="linkbtn ob-showall" :aria-expanded="expanded" @click="expanded = !expanded">
    {{expanded ? 'Show fewer' : 'Show all ' + all.length}}
  </button>
</div>
</template>
