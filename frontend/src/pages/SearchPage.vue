<script>
import { useDashboard } from '../state/context'
import { Pile, poseTransform, tileSize } from '../state/pile'
import { groupBest } from '../state/find.js'

// /search: every platform in the catalog is a tile, dropped under gravity (state/pile.ts, Matter.js)
// into a pile on the floor of the page. A described job (GET /catalog/find, see state/find.js) makes
// the platforms its keyword recall touched hop while the judge reads them; the platforms that fit
// then leave the pile and fly to their answer cards. The next search drops them back in.
//
// The page is exactly one viewport tall: the pile's floor is the bottom of the screen, and a long
// answer scrolls inside its own panel, never the page.
const EXAMPLES = [
  'Find the emails of CTOs at Series A fintech startups in Berlin',
  'Which competitors are running Facebook ads right now?',
  'Why is my blog losing Google traffic?',
  'Apple stock closing prices for last year',
  'Make a 10 second product video from a photo',
  'What are people on Reddit saying about our new pricing?',
]
const LAND = 'transform .8s cubic-bezier(.3,1.2,.4,1)'

export default {
  setup: useDashboard,
  data(){ return { text:'', examples:EXAMPLES, size:48, floor:220, reduced:false, landed:[], dragging:null, flung:null } },
  computed: {
    platforms(){ return this.plats.list.filter(p=>(p.category||'Other')!=='Other'); },
    // One card per platform the answer landed on, best fit first; each lists its jobs.
    cards(){
      if(this.find.phase!=='done') return [];
      return groupBest(this.findGroups, this.find.verdict, g=>g.platform,
        (g, slug)=>({slug, label:this.platShort(g.platform_label||g.platform)}), 'groups').slice(0,12);
    },
    reading(){ return this.findBusy ? new Set(this.findCandidatePlatforms) : new Set(); },
    readingList(){ return this.platforms.map(p=>p.slug).filter(s=>this.reading.has(s)); },
  },
  watch: {
    'plats.list'(){ this.$nextTick(()=>this.fit(true)); },
    'find.phase'(v){
      clearInterval(this.pokeTimer); clearInterval(this.scanTimer); this.scanTo(null);
      if(v==='recall' || v==='idle') this.dropLanded();
      if(v==='reading' && !this.reduced){
        this.pokeTimer=setInterval(()=>{
          const pick=this.readingList.filter(()=>Math.random()<0.35).slice(0,5);
          this.pile?.poke(pick);
        }, 420);
        let i=-1;
        this.scanTimer=setInterval(()=>{ const l=this.readingList; if(l.length) this.scanTo(l[i=(i+1)%l.length]); }, 120);
      }
    },
    cards(){ this.$nextTick(this.land); },
  },
  created(){ this.els={}; this.slotAt={}; },
  mounted(){
    this.reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
    document.documentElement.classList.add('sp-lock');
    this.pile=new Pile(poses=>{
      for(const [id,p] of poses){ const el=this.els[id]; if(!el) continue; el.style.transform=poseTransform(p, this.size); el.style.visibility='visible'; }
    });
    this.onResize=()=>{ clearTimeout(this.resizeTimer); this.resizeTimer=setTimeout(()=>this.fit(false), 120); };
    addEventListener('resize', this.onResize);
    // Anything that moves the answer (its content growing, the meta line changing) re-seats the
    // tiles already on their cards; the flight itself is not interrupted.
    this.ro=new ResizeObserver(()=>{ if(this.landed.length) this.place(false); });
    this.ro.observe(this.$el.querySelector('.sp-top'));
    this.loadPlatforms();
    this.mountField();
    this.$nextTick(()=>this.fit(true));
    const q=new URLSearchParams(location.search).get('q');
    if(q){ this.text=q; this.ask(); }
  },
  beforeUnmount(){
    document.documentElement.classList.remove('sp-lock');
    removeEventListener('resize', this.onResize);
    this.ro?.disconnect();
    cancelAnimationFrame(this.fieldRaf); this.field?.dispose();
    clearInterval(this.pokeTimer); clearInterval(this.scanTimer); clearTimeout(this.resizeTimer); clearTimeout(this.flungTimer);
    cancelAnimationFrame(this.scrollRaf);
    this.pile?.destroy();
  },
  methods: {
    ask(){
      // An empty box submits its placeholder: the example is an invitation, not decoration.
      if(!this.text.trim()) this.text=this.examples[0];
      const q=this.text.trim();
      history.replaceState(history.state, '', '/search?q='+encodeURIComponent(q));
      this.findRun(q);
    },
    pick(ex){ this.text=ex; this.ask(); },
    // Back to the empty page: the answer goes, its tiles fall back into the pile.
    exit(){
      this.text='';
      history.replaceState(history.state, '', '/search');
      this.findExit();
      this.$nextTick(()=>this.$el.querySelector('#sp-q')?.focus());
    },
    // The landing page's first-screen glyph field (/media/landing/hero-particles.js), mounted on
    // this page area and driven from this page's frame loop, so both first screens share one
    // implementation. Without WebGL the field declines and the gradient behind it remains.
    mountField(){
      const mount=()=>{
        if(this._isUnmounted || !this.$refs.stage) return;
        this.field=window.tregMountField?.(this.$refs.stage);
        if(!this.field) return;
        const loop=t=>{ this.field.tick(t); this.fieldRaf=requestAnimationFrame(loop); };
        this.fieldRaf=requestAnimationFrame(loop);
      };
      if(window.tregMountField) return mount();
      const script=document.createElement('script');
      script.src='/media/landing/hero-particles.js'; script.onload=mount;
      document.head.appendChild(script);
    },
    // The scan light moves several times a second while the judge reads: set on the element, not
    // through a reactive field, so it does not re-render every tile on every step.
    scanTo(slug){
      this.els[this.scanned]?.classList.remove('scan');
      this.scanned=slug;
      this.els[slug]?.classList.add('scan');
    },
    // The answer panel scrolls its landed tiles along; one re-seat per frame, however fast it scrolls.
    onPanelScroll(){
      if(this.scrollRaf) return;
      this.scrollRaf=requestAnimationFrame(()=>{ this.scrollRaf=0; this.place(false); });
    },
    tileRef(slug, el){
      if(!el){ delete this.els[slug]; return; }
      if(this.els[slug]===el) return;
      this.els[slug]=el;
      const pose=this.pile?.poseOf(slug);
      if(pose){ el.style.transform=poseTransform(pose, this.size); el.style.visibility='visible'; }
    },
    // Tiles in the pile can be picked up and thrown; a press that barely moves is a click and
    // opens the platform. Tiles on an answer card are plain links.
    press(p, e){
      if(e.button!==0) return;
      const sb=this.$refs.stage.getBoundingClientRect();
      const start={x:e.clientX, y:e.clientY};
      const held=this.pile.grab(p.slug, e.clientX-sb.left, e.clientY-sb.top);
      let moved=false;
      if(held){ e.preventDefault(); this.dragging=p.slug; }
      const move=ev=>{
        if(Math.hypot(ev.clientX-start.x, ev.clientY-start.y)>5) moved=true;
        if(held) this.pile.drag(ev.clientX-sb.left, ev.clientY-sb.top);
      };
      const up=()=>{
        removeEventListener('pointermove', move); removeEventListener('pointerup', up); removeEventListener('pointercancel', up);
        if(held){ this.pile.release(); this.flung=p.slug; clearTimeout(this.flungTimer); this.flungTimer=setTimeout(()=>{ this.flung=null; }, 1500); }
        this.dragging=null;
        if(!moved) this.findGoDashboard(p.slug);
      };
      addEventListener('pointermove', move); addEventListener('pointerup', up); addEventListener('pointercancel', up);
    },
    // Size the page to the viewport, size the tiles to the page, and (re)build the pile. The first
    // build drops the tiles from above so the pile forms on screen; a resize settles it unseen.
    fit(first){
      const stage=this.$refs.stage; if(!stage || !this.pile || !this.platforms.length) return;
      if(first && this.built) return;
      stage.style.height=Math.max(520, innerHeight-stage.getBoundingClientRect().top-scrollY)+'px';
      const W=stage.clientWidth, H=stage.clientHeight;
      const size=tileSize(W, H, this.platforms.length, W<640 ? 0.22 : 0.25);
      const rebuild=!this.built || size!==this.size;
      this.size=size; this.pile.size=size;
      // Room for the settled pile under the question box: the tiles' area, loosely packed, across the width.
      this.floor=Math.round(this.platforms.length*size*size/(0.62*W) + size*0.8);
      this.pile.bounds(W, H);
      if(rebuild){
        this.pile.clear();
        const inPile=this.platforms.filter(p=>!this.landed.includes(p.slug));
        if(this.reduced || this.built){ inPile.forEach(p=>this.pile.add(p.slug)); this.pile.settle(); }
        // An answer can land before the last tile has dropped (a shared ?q= link, a cached judge):
        // a tile already on its card is not dropped into the pile behind it.
        else inPile.forEach((p,i)=>setTimeout(()=>{ if(!this.landed.includes(p.slug)) this.pile.add(p.slug); }, i*16));
        this.built=true;
      }
      this.$nextTick(()=>this.place(false));
    },
    // The answer arrived: each fitting platform's tile leaves the pile from where it lies and flies
    // to the logo slot of its card.
    land(){
      const slots=this.measureSlots(), flying=[];
      // Every tile to its start pose first, one layout for all of them, then every flight at once.
      this.cards.forEach((c,i)=>{
        const el=this.els[c.slug]; if(!el || !slots[c.slug] || this.landed.includes(c.slug)) return;
        const pose=this.pile.remove(c.slug);
        const from=pose || {x:slots[c.slug].x, y:this.$refs.stage.clientHeight, angle:0};
        el.style.transition='none'; el.style.transform=poseTransform(from, this.size);
        el.style.visibility='visible';
        flying.push([el, i]);
        this.landed.push(c.slug);
      });
      if(flying.length) this.$refs.stage.getBoundingClientRect();
      for(const [el, i] of flying){
        el.style.transition=this.reduced ? 'none' : LAND;
        el.style.transitionDelay=this.reduced ? '0s' : (100+i*70)+'ms';
      }
      this.place(true, slots);
    },
    // Keep landed tiles on their slots (after a scroll of the answer panel or a resize); a slot
    // scrolled out of the panel hides its tile rather than letting it float over the page.
    place(animated, slots=this.measureSlots()){
      const stage=this.$refs.stage; if(!stage) return;
      const panel=this.$refs.panel?.getBoundingClientRect(), sb=stage.getBoundingClientRect();
      for(const slug of this.landed){
        const el=this.els[slug], s=slots[slug]; if(!el || !s) continue;
        if(!animated){ el.style.transition='none'; el.style.transitionDelay='0s'; }
        el.style.transform=`translate(${s.x}px,${s.y}px) scale(${s.w/this.size})`;
        const inView=!panel || (s.y+sb.top>=panel.top-4 && s.y+sb.top+s.w<=panel.bottom+4);
        el.style.visibility=inView ? 'visible' : 'hidden';
        this.slotAt[slug]=s;
      }
    },
    measureSlots(){
      const stage=this.$refs.stage, out={}; if(!stage) return out;
      const sb=stage.getBoundingClientRect();
      for(const el of stage.querySelectorAll('[data-slot]')){ const b=el.getBoundingClientRect(); out[el.dataset.slot]={x:b.left-sb.left, y:b.top-sb.top, w:b.width}; }
      return out;
    },
    // A new question: the last answer's tiles fall back into the pile from where they sat.
    dropLanded(){
      for(const slug of this.landed){
        const el=this.els[slug], s=this.slotAt[slug];
        if(el){ el.style.transition='none'; el.style.transitionDelay='0s'; }
        this.pile.add(slug, s ? s.x : undefined, s ? s.y : undefined);
      }
      this.landed=[];
    },
    tileClass(p){
      return {read:this.reading.has(p.slug), landed:this.landed.includes(p.slug), held:this.dragging===p.slug || this.flung===p.slug,
        dim:(this.findBusy && !this.reading.has(p.slug)) || (this.find.phase==='done' && this.cards.length && !this.landed.includes(p.slug))};
    },
    pct(p){ return p==null ? '' : Math.round(p*100)+'%'; },
  },
}
</script>

<template>
<div class="sp" :class="{answered:find.phase==='done' && cards.length}" ref="stage">
  <section class="sp-top">
    <div v-if="find.phase==='idle' || findBusy" class="sp-hero" :class="{quiet:findBusy}">
      <span class="sp-count">{{platforms.length}} platforms<template v-if="toolCountText"> · {{toolCountText}} tools</template></span>
      <h1 class="hero-h1">What does your agent<br><span>need to do?</span></h1>
    </div>

    <div v-else-if="find.phase==='error'" class="sp-panel sp-msg"><p>{{find.error}}</p>
      <button class="btn sm" type="button" @click="ask()">Try again</button></div>

    <div v-else-if="!cards.length" class="sp-panel sp-msg">
      <p><b>Nothing in the catalog does this yet.</b>
        <template v-if="find.verdict==='none'"> We read {{find.read}} candidate{{find.read===1?'':'s'}} and none fit.</template>
        Tell us what you need and it steers what gets added next.</p>
      <button class="btn sm primary" type="button" @click="findRequestTool()">Request this tool</button>
    </div>

    <div v-else class="sp-panel" ref="panel" @scroll.passive="onPanelScroll">
      <!-- The answer's one action sits on its title line, where the eye lands when the cards appear. -->
      <div class="sp-panel-h">
        <span v-if="find.verdict==='strong'" class="sp-title">Best fit for <b>{{find.q}}</b></span>
        <span v-else-if="find.verdict==='closest'" class="sp-title warn">No strong fit for <b>{{find.q}}</b>. Closest matches:</span>
        <span v-else class="sp-title">Keyword matches for <b>{{find.q}}</b>. Matching by meaning is unavailable right now.</span>
        <span class="sp-actions">
          <button class="sp-share" type="button" @click="findShare()">{{findCopied==='share'?'Link copied':'Share'}}</button>
          <button class="sp-copy" type="button" @click="findCopyAll()">
            <svg v-if="findCopied!=='all'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
            <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>
            {{findCopied==='all' ? 'Copied. Paste it to your agent' : 'Copy for your agent'}}</button>
        </span>
      </div>
      <div class="sp-cards">
        <article v-for="c in cards" :key="c.slug" class="sp-card" :class="{weak:findWeak(c)}">
          <header>
            <span class="sp-slot" :data-slot="c.slug" aria-hidden="true"></span>
            <button class="sp-plat" type="button" @click="findGoDashboard(c.slug)">{{c.label}}</button>
            <span v-if="c.p!=null" class="sp-fit">{{pct(c.p)}}</span>
          </header>
          <ul>
            <li v-for="g in c.groups.slice(0,3)" :key="g.key">
              <button type="button" @click="findGoDashboard(g.platform)" :title="g.rows.map(r=>r.id).join(', ')">
                <span class="sp-job">{{g.label}}</span>
                <span class="sp-m">{{g.rows.length}} provider{{g.rows.length===1?'':'s'}}<template v-if="findPrice(g)"> · {{findPrice(g)}}</template></span>
              </button>
            </li>
          </ul>
        </article>
      </div>
    </div>
  </section>

  <form class="sp-ask" @submit.prevent="ask()">
    <div class="sp-in">
      <label class="rd-sr-only" for="sp-q">Describe the job</label>
      <!-- Esc here, not on the window: Esc on the sign-in dialog must close the dialog only. -->
      <input id="sp-q" v-model="text" autocomplete="off" :placeholder="examples[0]" @keydown.esc="findActive && exit()">
      <button v-if="findActive || text" class="sp-x" type="button" aria-label="Clear the search" @click="exit()">×</button>
      <button class="sp-go" :class="{busy:findBusy}" type="submit" aria-label="Find tools">
        <svg v-if="!findBusy" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
      </button>
    </div>
    <p class="sp-meta" aria-live="polite">
      <template v-if="find.phase==='recall'">Looking through the catalog…</template>
      <template v-else-if="find.phase==='reading'"><i class="dot"></i>{{find.candidates.length}} candidates on {{findCandidatePlatforms.length}} platforms. Reading them for your job…</template>
    </p>
    <div class="sp-chips" v-if="find.phase==='idle' || find.phase==='done' && !cards.length">
      <button v-for="ex in examples.slice(1)" :key="ex" class="sp-chip" type="button" @click="pick(ex)">{{ex}}</button>
    </div>
  </form>

  <div class="sp-floor" :style="{height:floor+'px'}" aria-hidden="true"></div>
  <button v-for="p in platforms" :key="p.slug" :ref="el=>tileRef(p.slug, el)" class="sp-tile" :class="tileClass(p)"
          :style="{width:size+'px', height:size+'px'}" type="button" tabindex="-1" aria-hidden="true" :title="platShort(p.label)" @pointerdown="press(p, $event)">
    <img v-if="!platLogoBad[p.slug]" :src="'/logos/platforms/'+p.slug+'.svg'" alt="" draggable="false" @error="platLogoBad[p.slug]=true">
    <span v-else class="sp-i" :style="{background:platTileBg(p.slug)}">{{platInitial(p)}}</span>
  </button>
</div>
</template>

<style>
html.sp-lock,html.sp-lock body{overflow:hidden;overscroll-behavior:none}
/* The landing page's hero field, prepended into the page area by hero-particles.js. */
.sp > .hero-particles{position:absolute;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;contain:strict}
</style>

<style scoped>
/* Same visual language as the landing page (src/treg/web/landing.html): warm paper, white surfaces
   lifted by soft shadows rather than borders, pill controls, Geist Pixel display type. */
.sp{--l-bg:#f4f4f1;--l-surface:#fff;--l-ink:#1a1a1a;--l-muted:#7c7c7c;--l-muted2:#989898;--l-line:#2626231a;--l-line2:#25252233;
  --l-inverse:#1a1a1a;--l-inverse-ink:#f8f8f7;--l-shadow-sm:0 1px 2px #00000014;
  --l-shadow-md:0 1px 2px -1px #0000000a,0 4px 6px -1px #0000000f;
  --l-shadow-lg:0 1px 2px -1px #0000000a,0 4px 6px -1px #0000000f,0 8px 16px #0000000a;--l-ease:cubic-bezier(.2,.72,.25,1);
  position:relative;display:flex;flex-direction:column;align-items:center;gap:18px;overflow:hidden;padding:88px 20px 0;box-sizing:border-box;
  min-height:520px;background:var(--l-bg);color:var(--l-ink);font-family:"Suisse Intl","Inter","Segoe UI",system-ui,sans-serif}
/* The landing hero's soft green light, under its glyph field. */
.sp::before{content:"";position:absolute;inset:0 0 auto;height:75%;pointer-events:none;z-index:0;
  background:radial-gradient(ellipse at 50% 20%,#dce8d585,transparent 65%)}
[data-theme="dark"] .sp{--l-bg:#151412;--l-surface:#1e1d1b;--l-ink:#f2efe8;--l-muted:#a39f97;--l-muted2:#8a867e;--l-line:#ffffff14;--l-line2:#ffffff26;
  --l-inverse:#f2efe8;--l-inverse-ink:#151412;--l-shadow-sm:0 1px 2px #0006;--l-shadow-md:0 1px 2px #0006,0 4px 10px -2px #0008;
  --l-shadow-lg:0 1px 2px #0006,0 8px 18px -4px #000a}
.sp-top{position:relative;z-index:3;width:100%;max-width:1160px;flex:1;min-height:0;display:flex;align-items:center;justify-content:center}
.sp.answered .sp-top{align-items:flex-end}
.sp-hero{display:flex;flex-direction:column;align-items:center;gap:26px;text-align:center;transition:opacity .3s}
.sp-hero.quiet{opacity:.5}
.sp-count{display:inline-block;font-family:var(--mono);font-size:12px;letter-spacing:.08em;color:var(--l-muted);
  border:1px solid var(--l-line2);border-radius:999px;padding:5px 15px;background:var(--l-surface)}
.sp-hero h1{margin:0;font-family:var(--display,"Geist Pixel",monospace);font-weight:400;letter-spacing:0;font-size:clamp(30px,4.4vw,56px);line-height:1.16}
.sp-hero h1 span{color:var(--l-muted)}
.sp-panel{width:100%;max-height:100%;overflow:auto;box-sizing:border-box;padding:6px 6px 10px;display:flex;flex-direction:column;gap:14px;overscroll-behavior:contain}
.sp-panel-h{display:flex;align-items:center;justify-content:space-between;gap:12px 20px;flex-wrap:wrap;font-size:14px;color:var(--l-muted)}
.sp-panel-h b{color:var(--l-ink);font-weight:500}
.sp-title{min-width:0}
.sp-title.warn{color:#ba6603}
.sp-actions{display:flex;align-items:center;gap:16px;flex:none}
.sp-share{border:0;background:none;padding:0;font:inherit;font-size:13.5px;font-weight:500;color:var(--l-muted);cursor:pointer}
.sp-share:hover{color:var(--l-ink)}
.sp-copy{display:inline-flex;align-items:center;gap:8px;border:0;border-radius:999px;padding:11px 22px;cursor:pointer;
  background:var(--l-inverse);color:var(--l-inverse-ink);font:inherit;font-size:14px;font-weight:550;letter-spacing:.01em;
  box-shadow:var(--l-shadow-sm);transition:box-shadow .24s var(--l-ease),transform .12s var(--l-ease)}
.sp-copy:hover{box-shadow:var(--l-shadow-lg);transform:translateY(-1px)}
.sp-msg{max-width:760px;flex-direction:row;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;
  background:var(--l-surface);border-radius:15px;box-shadow:var(--l-shadow-md);padding:16px 20px;font-size:14px;color:var(--l-muted)}
.sp-msg p{margin:0}
.sp-msg b{color:var(--l-ink);font-weight:500}
.sp-msg .btn{border-radius:999px}
.sp-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,290px),1fr));gap:14px}
/* Fade only: the landing tiles measure the card slots as the cards appear, so the cards must not move. */
.sp-card{background:var(--l-surface);border-radius:15px;box-shadow:var(--l-shadow-md);padding:14px 16px;display:flex;flex-direction:column;gap:8px;
  transition:box-shadow .24s var(--l-ease);animation:sp-in .45s .3s both}
.sp-card:hover{box-shadow:var(--l-shadow-lg)}
@keyframes sp-in{from{opacity:0}}
.sp-card header{display:flex;align-items:center;gap:10px}
.sp-slot{flex:none;width:40px;height:40px}
.sp-plat{flex:1;min-width:0;text-align:left;border:0;background:none;padding:0;font:inherit;font-size:15px;font-weight:550;color:var(--l-ink);cursor:pointer}
.sp-plat:hover{text-decoration:underline;text-underline-offset:3px}
.sp-fit{font-family:var(--mono);font-size:11.5px;padding:2px 9px;border-radius:999px;background:var(--l-inverse);color:var(--l-inverse-ink);font-variant-numeric:tabular-nums}
.sp-card.weak .sp-fit{background:none;color:var(--l-muted);border:1px solid var(--l-line2)}
.sp-card ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.sp-card li button{width:100%;display:flex;justify-content:space-between;gap:10px;align-items:baseline;text-align:left;border:0;border-top:1px solid var(--l-line);
  background:none;padding:8px 0;font:inherit;font-size:13px;color:var(--l-ink);cursor:pointer}
.sp-card li button:hover .sp-job{text-decoration:underline;text-underline-offset:3px}
.sp-job{min-width:0}
.sp-m{flex:none;font-family:var(--mono);font-size:11px;color:var(--l-muted);white-space:nowrap}
.sp-ask{width:min(660px,100%);display:flex;flex-direction:column;align-items:center;gap:12px;position:relative;z-index:6}
.sp-in{width:100%;position:relative}
.sp-in input{width:100%;box-sizing:border-box;font:inherit;font-size:16px;color:var(--l-ink);background:var(--l-surface);
  border:1px solid var(--l-line2);border-radius:999px;padding:15px 104px 15px 24px;box-shadow:var(--l-shadow-sm);
  transition:border-color .12s var(--l-ease),box-shadow .24s var(--l-ease)}
.sp-in input::placeholder{color:var(--l-muted2)}
.sp-in input:hover{box-shadow:var(--l-shadow-md)}
.sp-in input:focus,.sp-in input:focus-visible{outline:none;border-color:var(--l-muted);box-shadow:var(--l-shadow-md)}
.sp-x{position:absolute;right:58px;top:50%;transform:translateY(-50%);width:32px;height:32px;border:0;border-radius:999px;background:none;
  color:var(--l-muted);font-size:20px;line-height:1;cursor:pointer}
.sp-x:hover{color:var(--l-ink);background:var(--l-line)}
.sp-go{position:absolute;right:7px;top:50%;transform:translateY(-50%);width:40px;height:40px;border-radius:999px;border:0;
  background:var(--l-inverse);color:var(--l-inverse-ink);display:grid;place-items:center;cursor:pointer;box-shadow:var(--l-shadow-sm)}
.sp-go.busy::after{content:"";width:15px;height:15px;border-radius:50%;border:2px solid color-mix(in srgb,currentColor 30%,transparent);border-top-color:currentColor;animation:sp-spin .8s linear infinite}
@keyframes sp-spin{to{transform:rotate(360deg)}}
.sp-meta{margin:0;min-height:34px;display:flex;align-items:center;justify-content:center;gap:10px;flex-wrap:wrap;font-family:var(--mono);font-size:12px;color:var(--l-muted)}
.sp-meta .dot{width:7px;height:7px;border-radius:50%;background:var(--l-ink);display:inline-block}
.sp-chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;max-width:760px}
.sp-chip{font:inherit;font-size:13px;font-weight:500;border:1px solid var(--l-line2);background:var(--l-surface);color:var(--l-ink);border-radius:999px;padding:7px 15px;cursor:pointer;
  transition:border-color .12s var(--l-ease),box-shadow .24s var(--l-ease)}
.sp-chip:hover{border-color:var(--l-muted);box-shadow:var(--l-shadow-sm)}
/* The floor the pile rests on is the bottom of the page; this spacer only keeps the question box
   and its examples above where the pile settles. */
.sp-floor{flex:none;width:100%}
.sp-tile{position:absolute;left:0;top:0;z-index:2;visibility:hidden;box-sizing:border-box;padding:0;border-radius:22%;border:1px solid var(--l-line);background:#fff;
  display:grid;place-items:center;cursor:grab;transform-origin:0 0;will-change:transform;user-select:none;touch-action:none;
  box-shadow:var(--l-shadow-sm);transition:opacity .4s,filter .4s}
.sp-tile img{width:58%;height:58%;object-fit:contain;pointer-events:none}
.sp-tile .sp-i{width:100%;height:100%;border-radius:inherit;display:grid;place-items:center;color:#fff;font-weight:600}
.sp-tile.read{box-shadow:var(--l-shadow-lg)}
.sp-tile.scan{box-shadow:0 0 0 2px var(--l-bg),0 0 0 3.5px var(--l-ink)}
.sp-tile.dim{opacity:.4;filter:grayscale(1)}
.sp-tile.landed{z-index:7;box-shadow:none;cursor:pointer}
.sp-tile.held{z-index:8;cursor:grabbing;opacity:1;filter:none;box-shadow:var(--l-shadow-lg)}
@media (prefers-reduced-motion:reduce){.sp-card,.sp-hero{transition:none;animation:none}}
</style>
