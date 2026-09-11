/* Standalone Arena: anonymous browsing, authenticated spending, one-click voting with visible vendors. */
(() => {
  'use strict';
  if (!window.Vue) return;
  const SIGNUP_SETUP = 'treg.arena.signup-setup.v1';
  const DRAFT = 'treg.arena.draft.v1';
  const ACTIVE = 'treg.arena.active.v1'; // Cleanup only: saved runs now reopen through their URL.
  const NAMES = {'treg':'Email verification waterfall','millionverifier':'MillionVerifier','contactout':'ContactOut','trykitt':'Kitt','fiber-ai':'Fiber','pdl':'People Data Labs','branddev':'Brand.dev',
    'thecompaniesapi':'TheCompaniesAPI','companyenrich':'CompanyEnrich','leadmagic':'LeadMagic',
    'findymail':'Findymail','leadsforge':'Leadsforge','icypeas':'Icypeas','predictleads':'PredictLeads',
    'apollo':'Apollo','exa':'Exa','aviato':'Aviato','hunter':'Hunter','tomba':'Tomba','lusha':'Lusha'};
  const LABELS = {q:'Describe the people',country:'Country code',count:'Matches returned',people:'People',companies:'Companies',full_name:'Full name',domain:'Company domain',linkedin_url:'LinkedIn URL',name:'Company name',
    email:'Email',first_name:'First name',last_name:'Last name',company_domain:'Company domain',
    line_type:'Line type',country_code:'Country code',valid:'Valid mailbox',verified:'Verified',
    employees:'Employees',founded:'Founded',website:'Website',description:'Description',industry:'Industry',
    company:'Company',title:'Job title',location:'Location',phone:'Phone',confidence:'Confidence',status:'Verdict',score:'Score'};
  const read = key => {try{return JSON.parse(sessionStorage.getItem(key)||'null');}catch{return null;}};
  const write = (key,value) => {try{sessionStorage.setItem(key,JSON.stringify(value));}catch{}};
  const remove = key => {try{sessionStorage.removeItem(key);}catch{}};

  // Horizontal overflow contains native sticky headers. Offset the real header on page scroll
  // instead, preserving column alignment without making the results a vertical scroll box.
  const StickyHeader={
    mounted(table){
      let frame=0,pinnedRow=null,pinnedOffset=0;
      const update=()=>{
        frame=0;
        const head=table.tHead;if(!head)return;
        // Mobile results become cards; pinning their overview covers the expanded answer.
        if(window.matchMedia?.('(max-width:600px)').matches){
          head.style.setProperty('--header-offset','0px');
          pinnedRow?.style.removeProperty('--entry-offset');pinnedRow=null;pinnedOffset=0;
          return;
        }
        const parent=table.closest('.entry-detail-panel')?.closest('.batch-matrix')||table.closest('.vendor-detail-panel')?.closest('.batch-scoreboard')?.querySelector(':scope > table');
        const selectedRow=':scope > tbody > .matrix-overview.picked, :scope > tbody > .vendor-summary.picked';
        const top=(parent?.tHead?.getBoundingClientRect().height||0)+(parent?.querySelector(selectedRow)?.getBoundingClientRect().height||0);
        const bounds=table.getBoundingClientRect(),height=head.getBoundingClientRect().height;
        head.style.setProperty('--header-offset',Math.max(0,Math.min(top-bounds.top,bounds.height-height))+'px');
        const row=table.querySelector(selectedRow);
        if(row!==pinnedRow){pinnedRow?.style.removeProperty('--entry-offset');pinnedRow=row;pinnedOffset=0;}
        if(row){
          const rect=row.getBoundingClientRect(),originalTop=rect.top-pinnedOffset;
          const end=row.nextElementSibling.getBoundingClientRect().bottom;
          pinnedOffset=Math.max(0,Math.min(height-originalTop,end-originalTop-rect.height));
          row.style.setProperty('--entry-offset',pinnedOffset+'px');
        }
      };
      const schedule=()=>{if(!frame)frame=requestAnimationFrame(update);};
      const resize=new ResizeObserver(schedule);resize.observe(table);
      window.addEventListener('scroll',schedule,{passive:true});
      window.addEventListener('resize',schedule);
      table._updateStickyHeader=schedule;
      table._stopStickyHeader=()=>{cancelAnimationFrame(frame);resize.disconnect();window.removeEventListener('scroll',schedule);window.removeEventListener('resize',schedule);};
      schedule();
    },
    updated(table){table._updateStickyHeader?.();},
    unmounted(table){table._stopStickyHeader?.();}
  };

  // Spreadsheet TSV and CSV, including quoted delimiters and embedded newlines.
  function pasteRows(text,columnCount){
    const delimiter=text.includes('\t')||columnCount===1&&!text.trim().startsWith('"')?'\t':',',rows=[];let row=[],value='',quoted=false;
    for(let i=0;i<text.length;i++){
      const c=text[i];
      if(c==='"'){if(quoted&&text[i+1]==='"'){value+='"';i++;}else quoted=!quoted;}
      else if(!quoted&&(c===delimiter||c==='\n'||c==='\r')){
        row.push(value.trim());value='';
        if(c!==delimiter){if(row.some(Boolean))rows.push(row);row=[];if(c==='\r'&&text[i+1]==='\n')i++;}
      }else value+=c;
    }
    if(quoted)throw new Error('The pasted list has an unclosed quote. Check the last row.');
    row.push(value.trim());if(row.some(Boolean))rows.push(row);
    return rows;
  }

  const ArenaFighters = {
    props:{entries:{type:Array,default:()=>[]},mode:String,requestable:Boolean,active:Boolean,selectable:Boolean,disabled:Boolean,selected:{type:Array,default:()=>[]},choosable:Boolean,enabledProviders:{type:Array,default:()=>[]},awards:{type:Object,default:()=>({})}},
    template:'#arena-fighters-template',
    emits:['change-mode','toggle-provider','request-vendor'],
    methods:{
      chooseMode(mode){if(this.selectable&&!this.disabled&&!this.active&&mode!==this.mode)this.$emit('change-mode',mode);},
      enabled(entry){return !this.choosable||this.enabledProviders.includes(entry.provider);},
      chooseProvider(entry){if(this.choosable&&!this.disabled&&!this.active)this.$emit('toggle-provider',entry.provider);},
      name(provider){return NAMES[provider]||provider;},
      rejected(entry){return (entry.rating?.value || (entry.report?'down':''))==='down';},
      state(entry){
        if(this.choosable&&!this.active&&!this.enabled(entry))return 'excluded';
        if(this.rejected(entry))return 'defeated';
        if(this.selected.includes(entry.id))return 'champion';
        if(entry.state==='running')return this.active?'fighting':'paused';
        return ({hit:'won',miss:'defeated',error:'error',timeout:'error',interrupted:'paused',cancelled:'paused',skipped:'benched',not_attempted:'benched',queued:'waiting'})[entry.state]||'ready';
      },
      label(entry){return entry.batchLabel||({excluded:'Excluded',champion:'Winner',won:'Hit!',defeated:this.rejected(entry)?'Thumbs down':'No match',error:entry.state==='timeout'?'Timed out':'Error',paused:'Stopped',benched:'Not called',waiting:'Waiting',fighting:'Fighting…',ready:'Ready'})[this.state(entry)];}
    }
  };

  const TaskIcon={props:['task'],computed:{path(){return ({
    'people.search':'M14 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM3 21v-2a6 6 0 0 1 8-5.65M20 17a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm-1 2 3 3',
    'people.company.search':'M3 21V4h11v6M6 8h1m3 0h1M6 12h1m-1 4h1m-1 4h1M21 21v-1a5 5 0 0 0-10 0v1m8-9a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
    'companies.similar':'M3 3h7v7H3zM14 14h7v7h-7zM14 3h7v7h-7zM6.5 14v3.5H10m0 0-2-2m2 2-2 2',
    'people.email.find':'M11 19H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5M2 6l10 7 10-7M20 17a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm-1 2 3 3',
    'people.phone.find':'M21 16.5v3a2 2 0 0 1-2.2 2A18 18 0 0 1 2.5 5.2 2 2 0 0 1 4.5 3h3l1.5 5-2 1.5a13 13 0 0 0 7.5 7.5l1.5-2Z',
    'people.enrich':'M14 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM3 21v-2a7 7 0 0 1 11-5.74M19 14v8m-4-4h8',
    'companies.enrich':'M3 21V3h13v9M7 7h1m4 0h1M7 11h1m4 0h1M7 15h1m-1 4h1M19 14v8m-4-4h8',
    'people.identity.resolve':'M10 13a5 5 0 0 0 7 .1l4-4a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7-.1l-4 4a5 5 0 0 0 7 7l2-2',
    'people.phone.verify':'M5 3h4l2 5-3 2a12 12 0 0 0 6 6l2-3 5 2v4c-8 3-18-7-16-16M15 5l2 2 4-4',
    'people.email.verify':'M12 3 3 7v6c0 5 9 9 9 9s9-4 9-9V7ZM8 12l3 3 5-6'
  })[this.task]||'M4 4h16v16H4z';}},template:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="path"/></svg>'};
  const ArenaTaskTabs={
    components:{TaskIcon},props:['groups','value','disabled'],emits:['change'],
    data:()=>({resizeObserver:null,overflowLeft:false,overflowRight:false}),
    computed:{
      tasks(){const all=this.groups.flatMap(g=>g.tasks),order=['people.email.find','people.phone.find','people.enrich','companies.enrich','people.email.verify','people.phone.verify','people.identity.resolve','people.search','people.company.search','companies.similar'];return order.map(id=>all.find(t=>t.id===id)).filter(Boolean);},
    },
    watch:{value:'reveal',tasks:'reveal'},
    methods:{
      updateOverflow(){
        const host=this.$refs.scroller;
        this.overflowLeft=host.scrollLeft>1;
        this.overflowRight=host.scrollWidth-host.clientWidth-host.scrollLeft>1;
      },
      scrollTabs(direction){
        const host=this.$refs.scroller;
        host.scrollLeft+=direction*Math.max(160,host.clientWidth*.65);
        this.updateOverflow();
      },
      async reveal(){
        await this.$nextTick();
        const host=this.$refs.scroller,tab=host.querySelector('[aria-selected="true"]');
        if(tab&&host.scrollWidth>host.clientWidth){
          const box=host.getBoundingClientRect(),item=tab.getBoundingClientRect(),inset=36;
          if(item.left<box.left+inset)host.scrollLeft-=box.left+inset-item.left;
          else if(item.right>box.right-inset)host.scrollLeft+=item.right-box.right+inset;
        }
        this.updateOverflow();
      },
      label(task){return ({'people.email.find':'Find work email','people.enrich':'Enrich person','companies.enrich':'Enrich company','people.phone.find':'Find phone number','people.identity.resolve':'Find LinkedIn profile','people.company.search':'Find people at company'})[task.id]||task.label;},
      select(id){if(!this.disabled&&id!==this.value)this.$emit('change',id);},
      keydown(event){
        if(this.disabled)return;
        const tabs=[...event.currentTarget.querySelectorAll('[role="tab"]')],index=tabs.indexOf(event.target.closest('[role="tab"]'));
        const next=({ArrowRight:(index+1)%tabs.length,ArrowLeft:(index-1+tabs.length)%tabs.length,Home:0,End:tabs.length-1})[event.key];
        if(next!==undefined&&tabs[next]){event.preventDefault();tabs[next].focus();this.select(tabs[next].dataset.task);}
      }
    },
    mounted(){this.resizeObserver=new ResizeObserver(this.reveal);this.resizeObserver.observe(this.$refs.scroller);},
    beforeUnmount(){this.resizeObserver?.disconnect();},
    template:`<div class="composer-tab-scroll"><div class="composer-tab-viewport"><div ref="scroller" class="composer-tabs" role="tablist" aria-label="Use case" @keydown="keydown" @scroll.passive="updateOverflow"><button v-for="task in tasks" :key="task.id" type="button" role="tab" :id="'arena-task-'+task.id" :data-task="task.id" aria-controls="task-inputs" :aria-selected="value===task.id" :tabindex="value===task.id?0:-1" :disabled="disabled" :class="{active:value===task.id}" @click="select(task.id)"><task-icon :task="task.id"/><span>{{label(task)}}</span></button></div><div v-if="overflowLeft" class="tab-overflow tab-overflow-left"><button type="button" aria-label="Scroll use cases left" @click="scrollTabs(-1)"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m10 4-4 4 4 4"/></svg></button></div><div v-if="overflowRight" class="tab-overflow tab-overflow-right"><button type="button" aria-label="Scroll use cases right" @click="scrollTabs(1)"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 4 4 4-4 4"/></svg></button></div></div></div>`
  };

  Vue.createApp({
    directives:{stickyHeader:StickyHeader},
    components:{ArenaTaskTabs,TregTryItOut:TregAgentSetup.TryItOut,TregAgentPicker:TregAgentSetup.AgentPicker,TregSetupInstructions:TregAgentSetup.SetupInstructions,ArenaFighters,ArenaResultTable:{directives:{stickyHeader:StickyHeader},inject:['arena'],props:{rows:{type:Array,required:true},entryView:Boolean},methods:{resultLabel(r){return this.arena.providerName(r.provider)+(this.entryView?' · '+this.arena.entryLabel(this.arena.runEntries[r.entry_index||0]):'');}},template:'#arena-result-table-template'}},
    provide(){return {arena:this};},
    data:()=>({benchmark:(location.pathname||'').endsWith('/people-search-bench'),benchCategories:[],benchCategory:'',benchLoading:false,benchError:'',leaderboard:(location.pathname||'').endsWith('/leaderboard'),setupStep:1,setupTeamName:'',setupExampleCopied:'',setupAgentId:'claude-code',setupToken:null,setupShowToken:false,setupCopied:false,setupError:'',setupLoading:false,setupSequence:0,tasks:[],taskId:'people.email.find',variant:0,inputs:{},extraInputs:[],showAllEntries:false,selectedEntry:null,selectedVendor:'',resultFilter:'all',mode:'waterfall',autoVerify:true,verificationHintHidden:false,verificationQuotes:{},verificationPending:{},
      user:null,teams:[],team:'',balance:null,meta:{},intercomStarted:false,intercomIdentity:'',busy:false,error:'',run:null,quote:null,history:[],customServices:false,services:[],
      insights:null,insightsState:'idle',insightsTimer:null,statsView:'rate',chartFocus:null,metricTooltip:null,requestDone:false,requestBusy:false,vendorPromptCopied:false,vendorPromptError:'',requestError:'',requestQuery:'',requestForm:{capability:'',note:'',contact:''},
      manualQuotes:{},manualPending:{},reporting:'',reportDrafts:{},pricing:false,quoteTimer:null,quoteSequence:0,pricedKey:'',expandedResults:[],email:'',code:'',emailStep:'email',devCode:'',authBusy:false,authError:'',
      newTeamName:'',pendingSubmit:false,pollTimer:null,pollFailures:0,scrollOnComplete:'',runTeam:'',booted:false,draftRestored:false,historySequence:0,historyLoading:false,historyHasMore:false,linkedRunPending:false,urlPopHandler:null}),
    watch:{
      'run.id'(){this.selectedVendor='';},
      quoteKey(){this.scheduleQuote();},
      running(value){if(!value)this.scheduleQuote();},
      user(){this.scheduleQuote();}
    },
    computed:{
      setupFeatured(){return ['claude-code','codex','openclaw','hermes'].map(id=>TregAgentSetup.agents.find(a=>a.id===id));},
      setupOtherCount(){return TregAgentSetup.agents.length+TregAgentSetup.moreAgents.length-this.setupFeatured.length;},
      setupAgent(){return [...TregAgentSetup.agents,...TregAgentSetup.moreAgents].find(a=>a.id===this.setupAgentId)||TregAgentSetup.agents[0];},
      setupCommand(){return TregAgentSetup.command(this.meta.public_url||location.origin);},
      inputRows(){return [this.inputs,...this.extraInputs];},
      visibleInputs(){return this.showAllEntries?this.inputRows:this.inputRows.slice(0,3);},
      runEntries(){return this.run?.identities||[this.run?.identity||{}];},
      isBatch(){return this.runEntries.length>1;},
      runCostSummary(){
        const results=this.run?.results||[];
        const found=new Set(results.filter(r=>r.state==='hit'&&this.ratingValue(r)!=='down').map(r=>r.entry_index??0)).size;
        const pending=!!this.run?.charge_pending||results.some(r=>this.hasAttempt(r)&&r.charged_micro==null);
        const total=Number.isFinite(this.run?.charged_micro)?this.run.charged_micro:null;
        return {total,found,pending,average:!pending&&found&&total!==null?total/found:null};
      },
      vendorResults(){return (this.run?.results||[]).filter(r=>r.provider===this.selectedVendor&&this.hasAttempt(r));},
      hasBatchFeedback(){return this.batchVendors.some(v=>v.up>0||v.down>0);},
      visibleResults(){return this.isBatch?this.run.results.filter(r=>r.entry_index===this.selectedEntry):this.run?.results||[];},
      matrixEntries(){return this.runEntries.map((identity,index)=>({identity,index,results:(this.run?.results||[]).filter(r=>(r.entry_index||0)===index)})).filter(e=>{
        const hits=e.results.filter(r=>r.state==='hit'&&this.ratingValue(r)!=='down');
        const answer=r=>this.discoveryRun()?this.searchMatches(r).map(match=>JSON.stringify(match)).sort():this.primaryFields.map(k=>r.output[k]??null);
        return this.resultFilter==='all'||this.resultFilter==='found'&&hits.length>0||this.resultFilter==='unresolved'&&!hits.length||this.resultFilter==='disagreement'&&new Set(hits.map(r=>JSON.stringify(answer(r)))).size>1;
      });},
      batchVendors(){
        if(!this.isBatch)return [];
        const groups=new Map();
        for(const r of this.run.results){if(!groups.has(r.provider))groups.set(r.provider,[]);groups.get(r.provider).push(r);}
        return [...groups].map(([provider,results])=>{
          const hits=results.filter(r=>r.state==='hit'),kept=hits.filter(r=>this.ratingValue(r)!=='down');
          const attempted=results.filter(r=>!['not_attempted','skipped','queued'].includes(r.state));
          const complete=results.every(r=>['hit','miss','error','timeout'].includes(r.state));
          const timings=kept.map(r=>r.duration_ms).filter(Number.isFinite).sort((a,b)=>a-b);
          const cost=results.every(r=>Number.isFinite(r.charged_micro))?results.reduce((sum,r)=>sum+r.charged_micro,0):null;
          const state=results.some(r=>r.state==='running')?'running':results.some(r=>r.state==='queued')?'queued':kept.length?'hit':attempted.length?'miss':'not_attempted';
          const total=this.runEntries.length;
          return {id:provider,provider,state,total,complete,found:hits.length,kept:kept.length,attempted:attempted.length,
            up:results.filter(r=>this.ratingValue(r)==='up').length,down:results.filter(r=>this.ratingValue(r)==='down').length,
            cost,costPerKept:kept.length&&cost!==null?cost/kept.length:null,
            median:timings.length===kept.length&&timings.length?(timings[Math.floor((timings.length-1)/2)]+timings[Math.floor(timings.length/2)])/2:null,
            batchLabel:this.discoveryRun()?hits.length+'/'+(this.run.mode==='waterfall'?attempted.length:total)+' queries matched':this.run.mode==='waterfall'?hits.length+' found · '+attempted.length+' tried':Math.round(hits.length/total*100)+'% · '+hits.length+'/'+total+' found'};
        });
      },
      batchAwards(){
        const awards={};if(!this.isBatch||this.run.state!=='completed'||this.run.mode!=='compare')return awards;
        const eligible=this.batchVendors.filter(v=>v.kept>0);
        // A partial vendor cohort cannot win a completed benchmark.
        if(this.batchVendors.some(v=>!v.complete||v.attempted!==v.total))return awards;
        for(const [field,label,maximize] of [['kept','Coverage',true],['costPerKept','Cheapest',false],['median','Fastest',false]]){
          if(!eligible.length||!eligible.every(v=>Number.isFinite(v[field])))continue;
          const best=(maximize?Math.max:Math.min)(...eligible.map(v=>v[field]));
          for(const v of eligible)if(v[field]===best)(awards[v.id]||=[]).push(label);
        }return awards;
      },
      quoteKey(){return JSON.stringify({team:this.team,capability:this.taskId,auto_verify:this.autoVerify&&!!this.verificationTask,identities:this.identities(),mode:this.mode,providers:this.customServices?this.services:null});},
      readyQuote(){return this.quote&&this.pricedKey===this.quoteKey?this.quote:null;},
      runButtonLabel(){if(this.running)return 'Running…';if(this.busy)return 'Starting…';if(this.pricing)return 'Pricing…';const q=this.readyQuote,n=this.inputRows.length,run=n>1?'Run '+n+' entries':'Run';if(q)return this.mode==='waterfall'?(q.affordable?run+' from ':'Top up · from ')+this.usd(q.required_micro):(q.affordable?run:'Top up')+' · ~'+this.usd(q.required_micro);return n>1?run:this.mode==='waterfall'?'Run waterfall':'Battle';},
      benchSelected(){return this.benchCategories.find(c=>c.id===this.benchCategory)||this.benchCategories[0]||null;},
      benchRows(){return [...(this.benchSelected?.rows||[])].sort((a,b)=>b.score-a.score);},
      verificationTask(){return ({'people.email.find':'people.email.verify','people.phone.find':'people.phone.verify'})[this.taskId]||null;},
      verificationPreview(){return this.verificationEstimate(this.verificationTask);},
      discovery(){return !!this.currentTask.discovery;},
      maxEntries(){return this.currentTask.max_entries||50;},
      jobGroups(){const groups=[{label:'Discover',ids:['people.search','people.company.search','companies.similar']},{label:'Enrich',ids:['people.email.find','people.phone.find','people.enrich','companies.enrich','people.identity.resolve']},{label:'Verify',ids:['people.email.verify','people.phone.verify']}];return groups.map(g=>({...g,tasks:g.ids.map(id=>this.tasks.find(t=>t.id===id)).filter(Boolean)})).filter(g=>g.tasks.length);},
      currentTask(){return this.tasks.find(t=>t.id===this.taskId)||{description:'Choose a task to get started.',variants:[],providers:[],fields:[]};},
      inputKeys(){return this.currentTask.variants[this.variant]||[];},
      insightInput(){return this.inputKeys.length===2&&this.inputKeys.includes('full_name')&&this.inputKeys.includes('domain')?'name_domain':this.inputKeys.length===1?this.inputKeys[0]:'';},
      insightRows(){
        // Always use the public task cohort, independent of paid quotes and avatar selection.
        const rows=this.insights?.rows||[];
        return (this.currentTask.provider_previews?.[this.variant]||[]).map(p=>{
          const sample=rows.find(r=>r.task===this.taskId&&r.input===this.insightInput&&r.endpoint===p.endpoint_id);
          const audit=(this.insights?.verification?.rows||[]).find(r=>r.task===this.taskId&&r.input===this.insightInput&&r.endpoint===p.endpoint_id);
          return {...p,sample,audit};
        }).sort((a,b)=>this.providerName(a.provider).localeCompare(this.providerName(b.provider)));
      },
      supportsVerifiedRate(){return ['people.email.find','people.phone.find'].includes(this.taskId);},
      showVerifiedRateColumn(){return this.supportsVerifiedRate&&(this.taskId!=='people.phone.find'||this.chartRows.some(p=>Number.isFinite(p.verifiedRate)));},
      vendorListingPrompt(){return 'Read https://treg.to/vendor-listing.md and add our API to the treg catalog, then open a PR.';},
      verifiedRateLabel(){return this.taskId==='people.email.find'?'Email validity rate':'Phone format validity';},
      verifiedExplanation(){return this.taskId==='people.phone.find'?'Percentage of sampled returned phone numbers with completed checks that passed the verifier’s number-format check. This does not confirm a live line, deliverability or ownership.':'Percentage of sampled returned emails with completed checks that both verifiers marked valid. Risky, unknown and conflicting verdicts do not count as valid.';},
      verificationWindow(){const v=this.insights?.verification;if(!v)return 'No published pilot';const date=x=>new Date(x).toLocaleDateString('en-US',{timeZone:'UTC'});return 'Sample '+date(v.sample_since)+'–'+date(v.sample_until)+' · Checked '+date(v.checked_at)+' · UTC';},
      insightVerification(){return ['people.email.verify','people.phone.verify'].includes(this.taskId);},
      insightWindow(){if(!this.insights)return '';const date=s=>new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric',year:'numeric',timeZone:'UTC'}).format(new Date(s));const observed=this.insights.observed_since&&this.insights.observed_until;return (observed?'Recorded ':'Window ')+date(observed?this.insights.observed_since:this.insights.since)+' – '+date(observed?this.insights.observed_until:this.insights.until)+' · UTC';},
      insightReviewedOn(){return this.insights?.updated_at?new Intl.DateTimeFormat('en-US',{dateStyle:'medium',timeStyle:'short',timeZone:'UTC'}).format(new Date(this.insights.updated_at))+' UTC':'';},
      availableProviders(){
        const catalog=this.currentTask.provider_previews?.[this.variant]||[],quoted=this.readyQuote?.providers||[];
        return [...quoted,...catalog.filter(p=>!quoted.some(q=>q.provider===p.provider))];
      },
      enabledProviders(){return this.customServices?this.services:this.availableProviders.map(p=>p.provider);},
      resultFighters(){if(this.isBatch)return this.batchVendors;return this.showProviderPreview||this.running?this.run.results:[...this.run.results,...this.availableProviders.filter(p=>!this.run.results.some(r=>r.provider===p.provider))];},
      previewProviders(){if(this.leaderboard)return this.insightRows;const rows=this.readyQuote?.providers||(this.currentTask.provider_previews?.[this.variant]||[]).map(p=>({...p,estimate_micro:p.estimate_micro==null?null:p.estimate_micro*this.inputRows.length}));const selected=this.customServices?rows.filter(p=>this.services.includes(p.provider)):rows;return selected.map(p=>({...p,sample:this.insightRows.find(r=>r.endpoint_id===p.endpoint_id)?.sample,audit:this.insightRows.find(r=>r.endpoint_id===p.endpoint_id)?.audit}));},
      chartViews(){return [{id:'rate',label:this.insightVerification?'Verdict rate':'Hit rate'},...(this.supportsVerifiedRate?[{id:'verified',label:this.verifiedRateLabel}]:[]),{id:'speed',label:'Response time'},{id:'price',label:'Price'},{id:'value',label:this.insightVerification?'Price vs verdict rate':'Price vs hit rate'}];},
      chartRows(){
        const colors=['#5b8f88','#7b8fae','#b39a69','#9787a6','#8b9c76','#b98476'];
        return this.previewProviders.map(p=>({...p,
          color:colors[Math.max(0,this.insightRows.findIndex(r=>r.endpoint_id===p.endpoint_id))%colors.length],
          verifiedRate:this.verifiedRateValue(p),
          rate:p.sample?.unique_requests>=20&&Number.isFinite(p.sample.unique_rate)&&p.sample.unique_rate>=0&&p.sample.unique_rate<=100?p.sample.unique_rate:null,
          ms:p.sample?.timed_hits>=20&&Number.isFinite(p.sample.median_hit_ms)&&p.sample.median_hit_ms>=0?p.sample.median_hit_ms:null
        }));
      },
      chartBars(){const field=this.statsView==='verified'?'verifiedRate':this.statsView==='price'?'estimate_micro':this.statsView==='speed'?'ms':'rate';return this.chartRows.filter(p=>Number.isFinite(p[field])&&p[field]>=0).map(p=>({...p,value:p[field]})).sort((a,b)=>(['rate','verified'].includes(this.statsView)?b.value-a.value:a.value-b.value)||this.providerName(a.provider).localeCompare(this.providerName(b.provider)));},
      chartMax(){if(this.statsView==='price')return Math.max(1,...this.chartBars.map(p=>p.value));return this.statsView==='speed'?this.chartCeiling(Math.max(0,...this.chartRows.map(p=>p.ms||0))):100;},
      chartTimeMax(){return Math.max(1,...this.chartRows.map(p=>p.ms||0));},
      chartPoints(){return this.chartRows.filter(p=>p.rate!==null&&Number.isFinite(p.estimate_micro)&&p.estimate_micro>=0);},
      chartPriceMax(){return this.chartCeiling(Math.max(0,...this.chartPoints.map(p=>p.estimate_micro)));},
      chartDetail(){const rows=this.statsView==='table'?this.chartRows:this.statsView==='value'?this.chartPoints:this.chartBars;return rows.find(p=>p.endpoint_id===this.chartFocus)||null;},
      showProviderPreview(){if(!this.run)return true;if(this.running)return false;const canonical=rows=>JSON.stringify(rows.map(r=>Object.entries(r).sort()));return this.run.capability!==this.taskId||this.run.mode!==this.mode||canonical(this.identities())!==canonical(this.runEntries)||this.customServices&&this.services.slice().sort().join()!==[...new Set(this.run.results.map(r=>r.provider))].sort().join();},
      running(){return this.run?.state==='running';},
      upvotedResults(){return (this.run?.results||[]).filter(r=>this.ratingValue(r)==='up');},
      winnerIds(){if(this.isBatch)return Object.keys(this.batchAwards).filter(id=>this.batchAwards[id].includes('Coverage'));if(this.run?.state!=='completed')return [];const upvoted=this.upvotedResults;return upvoted.length===1?[upvoted[0].id]:upvoted.length>1?Object.keys(this.battleAwards):[];},
      battleAwards(){
        if(this.isBatch)return this.batchAwards;
        const awards={};
        if(!this.run||this.run.state!=='completed')return awards;
        const upvoted=this.upvotedResults;
        if(!upvoted.length&&this.run.mode!=='compare')return awards;
        const results=upvoted.length>1?upvoted:this.run.results.filter(r=>r.state==='hit'&&this.ratingValue(r)!=='down');
        if(results.length<2)return awards;
        for(const [field,label] of [['duration_ms','Fastest'],['charged_micro','Cheapest']]){
          // Unknown charges/timings cannot establish a winner; genuine zeros can.
          if(!results.every(r=>Number.isFinite(r[field])&&r[field]>=0))continue;
          const best=Math.min(...results.map(r=>r[field]));
          for(const r of results)if(r[field]===best)(awards[r.id] ||= []).push(label);
        }
        return awards;
      },
      primaryFields(){if(['people.search','people.company.search','companies.similar'].includes(this.run?.capability))return ['count'];const preferred={'people.email.find':['email','verified'],'people.enrich':['name','full_name','title','company'],'companies.enrich':['name','domain','industry'],'people.phone.find':['phone','line_type'],'people.email.verify':['valid','status'],'people.phone.verify':['valid','line_type','country_code'],'people.identity.resolve':['linkedin_url','full_name']};const fields=this.run?.fields||[];const core=fields.filter(k=>(preferred[this.run?.capability]||[]).includes(k));return core.length?core:fields.slice(0,3);},
      timelineEnd(){return Math.max(1,...(this.run?.results||[]).map(r=>(r.started_ms||0)+(r.duration_ms||0)));}
    },
    methods:{
      async loadBenchmark(){
        this.benchLoading=true;this.benchError='';
        try{
          const response=await fetch('/people-search',{credentials:'omit'});
          if(!response.ok)throw new Error('Benchmark source unavailable.');
          this.benchCategories=window.ArenaBench.parseDocument(new DOMParser().parseFromString(await response.text(),'text/html'));
          if(!this.benchCategories.some(c=>c.id===this.benchCategory))this.benchCategory=this.benchCategories.find(c=>c.id==='b2b-prospecting')?.id||this.benchCategories[0].id;
        }catch(e){this.benchCategories=[];this.benchError='Could not load the published benchmark. Please try again or open the source.';}
        finally{this.benchLoading=false;}
      },
      sectionUrl(leaderboard){return '/enrich-arena'+(leaderboard?'/leaderboard':'')+'?capability='+encodeURIComponent(this.taskId)+'&variant='+this.variant;},
      runUrl(id,team=this.runTeam||this.team){return '/enrich-arena?run='+encodeURIComponent(id)+(team?'&team='+encodeURIComponent(team):'');},
      syncUrl(runId='',replace=false){
        if(!this.booted||this.benchmark)return;
        const url=runId?this.runUrl(runId):this.sectionUrl(this.leaderboard);
        if((location.pathname||'/enrich-arena')+(location.search||'')!==url)window.history?.[replace?'replaceState':'pushState'](null,'',url);
      },
      async restoreLinkedRun(){
        const params=new URLSearchParams(location.search),id=params.get('run');
        if(!id||this.leaderboard||this.benchmark)return false;
        this.linkedRunPending=true;
        if(!this.user){await this.openLogin(false);return true;}
        const team=params.get('team');
        if(team&&!this.teams.some(t=>t.slug===team)){this.error='This saved run is not available to your account or team.';return true;}
        if(team){this.team=team;this.syncIntercom();}
        if(!this.team){this.error='This saved run is not available to your account or team.';return true;}
        await this.loadHistory(id,{replace:true});this.linkedRunPending=false;
        await this.loadBalance();await this.refreshHistory();return true;
      },
      async loadInsights(){
        if(this.insightsState==='loading')return;
        this.insightsState='loading';
        try{const data=await this.api('/arena/insights',{},'');if(data.version!==2||!Array.isArray(data.rows)||!['ready','warming'].includes(data.status)||!Number.isFinite(Date.parse(data.since))||!Number.isFinite(Date.parse(data.until))||data.updated_at!==null&&!Number.isFinite(Date.parse(data.updated_at)))throw new Error('Invalid stats response');this.insights=data;this.insightsState='ready';}
        catch{this.insightsState='error';}
      },
      insightCount(n){return new Intl.NumberFormat('en-US').format(n);},
      providerLabelParts(provider){return this.providerName(provider).split(/(?<=[a-z])(?=[A-Z])/);},
      chartCeiling(value){if(value<=0)return 1;const power=10**Math.floor(Math.log10(value)),step=[1,2,2.5,5,10].find(n=>n*power>=value);return step*power;},
      chartTick(value){return this.statsView==='price'?this.usd(value):this.statsView==='speed'?this.seconds(Math.round(value)):value+'%';},
      chartLabel(p){if(this.statsView==='verified')return this.providerName(p.provider)+': '+this.verifiedRateLabel.toLowerCase()+' '+this.verifiedRate(p)+'. '+this.verifiedRateNote(p);return this.providerName(p.provider)+': '+(this.insightVerification?'verdict rate ':'hit rate ')+this.insightRate(p)+', response time '+this.insightResponseTime(p)+', estimated price '+(p.estimate_micro==null?'unavailable':this.usd(p.estimate_micro));},
      showMetricTooltip(event){const r=event.currentTarget.getBoundingClientRect(),width=Math.min(300,window.innerWidth-24);this.metricTooltip={width,left:Math.max(12,Math.min(r.left+(r.width-width)/2,window.innerWidth-width-12)),top:Math.max(12,Math.min(r.bottom+8,window.innerHeight-140))};},
      verifiedRateValue(row){
        const a=row.audit;
        const email=this.taskId==='people.email.find',phone=this.taskId==='people.phone.find';
        if(!a||!(a.checked_n>=20)||!(email&&a.method==='email_verifier_consensus'||phone&&a.method==='phone_format'))return null;
        const rate=email?a.validity_rate:a.format_validity_rate;
        return Number.isFinite(rate)&&rate>=0&&rate<=100?rate:null;
      },
      verifiedRate(row){const rate=this.verifiedRateValue(row);return rate===null?'—':rate.toFixed(1)+'%';},
      verifiedRateNote(row){
        const a=row.audit;
        if(!a)return 'No published verification sample for this endpoint and input.';
        if(!(a.checked_n>=20))return 'Insufficient completed verification sample.';
        const v=this.insights?.verification,date=x=>x?new Date(x).toLocaleDateString('en-US',{timeZone:'UTC'}):'';
        if(this.taskId==='people.phone.find')return 'Share of sampled returned phone numbers that passed '+a.verifiers.map(p=>this.providerName(p)).join(' + ')+' number-format checks. Checks completed '+date(v?.checked_at)+'. Unresolved inputs are excluded; reachability and ownership are not verified.';
        return 'Share of sampled returned emails marked valid by '+a.verifiers.map(p=>this.providerName(p)).join(' + ')+'. Checks completed '+date(v?.checked_at)+'. '+'Risky, unknown and conflicting verdicts are not counted as valid; unfinished checks are excluded. Verifier agreement does not confirm delivery or ownership.';
      },
      insightRate(row){
        const s=row.sample;
        if(!s||s.unique_requests<20)return '—';
        return Number.isFinite(s.unique_rate)?s.unique_rate.toFixed(1)+'%':'—';
      },
      insightSampleLabel(row){
        const s=row.sample;
        if(!s?.unique_requests)return this.insights?.status==='warming'?'Processing…':this.insightsState==='ready'?'':this.insightsState==='error'?'Unavailable':'Loading…';
        return '';
      },
      insightResponseTime(row){
        const s=row.sample;
        return s&&s.timed_hits>=20&&Number.isFinite(s.median_hit_ms)&&s.median_hit_ms>=0?this.seconds(Math.round(s.median_hit_ms)):'—';
      },
      awardTitle(badge){if(this.isBatch)return ({Coverage:'Most found results after thumbs-down rejections; not verified accuracy.',Cheapest:'Total charges, including misses, divided by results kept.',Fastest:'Lowest median response time of results kept; excludes queue time.'})[badge];if(badge==='Winner')return 'Your thumbs-up winner';const cohort=this.upvotedResults.length>1?'thumbs-up results':'successful, non-downvoted results';return (badge==='Fastest'?'Lowest response time':'Lowest actual charge')+' among '+cohort;},
      entryLabel(identity){return Object.values(identity).join(' · ');},
      entryResult(entry,provider){return entry.results.find(r=>r.provider===provider);},
      hasAttempt(result){return !['not_attempted','skipped','queued'].includes(result.state);},
      toggleEntryRow(event,index){if(event.target.closest('button,a,input,select,textarea,summary'))return;this.selectEntry(index);},
      selectEntry(index,result){const collapse=this.selectedEntry===index&&(!result||this.expandedResults.includes(result.id));this.selectedVendor='';this.selectedEntry=collapse?null:index;this.reporting='';this.expandedResults=!collapse&&result?[result.id]:[];},
      selectVendor(v){if(!v.attempted)return;this.selectedVendor=this.selectedVendor===v.provider?'':v.provider;this.selectedEntry=null;this.reporting='';this.expandedResults=[];},
      toggleVendorRow(event,v){if(event.target.closest('button,a,input,select,textarea,summary'))return;this.selectVendor(v);},
      addEntry(){if(this.busy||this.running||this.inputRows.length>=this.maxEntries)return;this.extraInputs.push({});this.showAllEntries=true;this.quote=null;},
      removeEntry(index){if(this.busy||this.running||this.inputRows.length===1)return;const rows=this.inputRows;rows.splice(index,1);this.inputs=rows[0];this.extraInputs=rows.slice(1);this.quote=null;},
      pasteEntries(event,index){
        if(this.busy||this.running)return;
        const text=event.clipboardData?.getData('text/plain')||'';
        if(!/[\n\r\t]/.test(text.trim()))return;
        event.preventDefault();this.error='';
        try{
          let rows=pasteRows(text,this.inputKeys.length);
          const aliases={q:['q','query','description','searchdescription'],title:['title','jobtitle','role'],country:['country','countrycode'],company_domain:['companydomain','domain','companywebsite'],full_name:['fullname','name','personname'],domain:['domain','companydomain','companywebsite'],email:['email','emailaddress'],linkedin_url:['linkedin','linkedinurl','linkedinprofile'],name:['name','company','companyname']};
          const header=rows[0]?.map(v=>v.toLowerCase().replace(/[^a-z]/g,''))||[];
          const columns=this.inputKeys.map(k=>header.findIndex(h=>(aliases[k]||[k]).includes(h)));
          const hasHeader=columns.every(i=>i>=0);
          if(hasHeader)rows=rows.slice(1);
          if(!rows.length)return;
          if(rows.some(r=>r.length!==this.inputKeys.length)&&!hasHeader)throw new Error('Paste '+this.inputKeys.length+' columns: '+this.inputKeys.map(k=>this.fieldLabel(k)).join(' + ')+'.');
          const entries=rows.map(r=>Object.fromEntries(this.inputKeys.map((k,i)=>[k,r[hasHeader?columns[i]:i]||''])));
          if(this.inputRows.length-1+entries.length>this.maxEntries)throw new Error('Use at most '+this.maxEntries+' entries. Nothing was pasted.');
          const all=this.inputRows;all.splice(index,1,...entries);this.inputs=all[0];this.extraInputs=all.slice(1);this.showAllEntries=false;this.quote=null;this.error=this.inputError();this.saveDraft(false);
        }catch(e){this.error=e.message;}
      },
      usd(n){return n==null?'—':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:6}).format(n/1e6);},
      billingLabel(p){if(this.discovery&&p.price_type==='per_result'&&['platform','catalog'].includes(p.tier))return 'Estimate for up to '+this.currentTask.result_limit+' matches';if(p.tier&&p.tier!=='platform'&&p.tier!=='catalog')return 'Your key · no treg charge';return ({per_success:'Per successful request',per_result:'Per result',per_call:'Per request'})[p.price_type]||'Per lookup';},
      seconds(ms){return ms<1000?ms+' ms':(ms/1000).toFixed(1)+' s';},
      providerName(p){return NAMES[p]||p;},fieldLabel(k){return LABELS[k]||k.replaceAll('_',' ');},
      variantLabel(v){return v.map(k=>({q:'Search description',title:'Job title',country:'Country code',company_domain:'Company domain',full_name:'Name',domain:'company domain',linkedin_url:'LinkedIn URL',email:'Email',phone:'Phone number',name:'Company name'})[k]||k).join(' + ');},
      taskLabel(id){return this.tasks.find(t=>t.id===id)?.label||id;},
      placeholder(k){return ({phone:'+14155550100',q:'e.g. Heads of growth at B2B software companies in Chicago',title:'e.g. Head of growth',country:'e.g. US',company_domain:'e.g. acme.com',full_name:'First and last name',domain:'company.com',linkedin_url:this.taskId==='companies.enrich'?'https://linkedin.com/company/…':'https://linkedin.com/in/…',email:'name@company.com',name:'Company name'})[k]||k;},
      domainMismatch(r){const identity=this.runEntries[r.entry_index||0];if(this.run?.capability!=='people.email.find'||!identity.domain||typeof r.output.email!=='string')return '';const actual=r.output.email.split('@')[1]?.toLowerCase();const expected=identity.domain.toLowerCase();return actual&&actual!==expected&&!actual.endsWith('.'+expected)?'Email domain is '+actual+'; you searched '+expected+'.':'';},
      identityWarning(r){
        if(r.state!=='hit'||!['people.enrich','people.identity.resolve'].includes(this.run?.capability))return '';
        const name=v=>typeof v==='string'?v.normalize('NFKD').replace(/\p{M}/gu,'').toLowerCase().replace(/[^\p{L}\p{N}]+/gu,' ').trim():'';
        const profile=v=>typeof v==='string'?v.toLowerCase().replace(/^https?:\/\//,'').replace(/^www\./,'').replace(/\/$/,''):'';
        const entry=r.entry_index||0,identity=this.runEntries[entry]||{},out=r.output||{};
        if(identity.full_name&&out.full_name&&name(identity.full_name)!==name(out.full_name))return 'Returned name differs from your query. Review the original response.';
        if(identity.linkedin_url&&out.linkedin_url&&profile(identity.linkedin_url)!==profile(out.linkedin_url))return 'Returned LinkedIn profile differs from your query.';
        const peers=this.run.results.filter(p=>p.state==='hit'&&(p.entry_index||0)===entry);
        if(['full_name','linkedin_url'].some(key=>new Set(peers.map(p=>(key==='full_name'?name:profile)(p.output?.[key])).filter(Boolean)).size>1))return 'Vendors returned different identities for this entry. Review names and profiles.';
        return '';
      },
      summaryFields(r){if(r.state!=='hit')return [];return this.primaryFields.filter(k=>!(k==='verified'&&r.verification)&&!(k==='valid'&&this.run?.capability==='people.email.verify'&&r.output.status)&&r.output[k]!==null&&r.output[k]!==undefined&&r.output[k]!=='');},
      toggleResultRow(event,id){if(event.target.closest('button,a,input,select,textarea,summary'))return;this.toggleDetails(id);},
      toggleDetails(id){this.expandedResults=this.expandedResults.includes(id)?this.expandedResults.filter(x=>x!==id):[...this.expandedResults,id];},
      searchMatches(r){return Array.isArray(r.output?.people)?r.output.people:Array.isArray(r.output?.companies)?r.output.companies:[];},
      discoveryRun(){return ['people.search','people.company.search','companies.similar'].includes(this.run?.capability);},
      displayValue(v){if(Array.isArray(v))return v.length+' matches';return v===null||v===undefined||v===''?'Not returned':v===true?'Yes':v===false?'No':String(v);},
      emailVerdict(output={}){
        const status=String(output.status||'').trim().toLowerCase().replace(/[\s-]+/g,'_');
        if(['accept_all','catch_all','catchall','risky'].includes(status))return {label:'Risky',tone:'risky'};
        if(['valid','ok','deliverable'].includes(status))return {label:'Valid',tone:'valid'};
        if(['invalid','undeliverable','disposable'].includes(status))return {label:'Invalid',tone:'invalid'};
        if(status)return {label:'Unknown',tone:'unknown'};
        return output.valid===true?{label:'Valid',tone:'valid'}:output.valid===false?{label:'Invalid',tone:'invalid'}:{label:'Unknown',tone:'unknown'};
      },
      outcomeClass(r){return r.state==='hit'&&this.run?.capability==='people.email.verify'?'verdict-'+this.emailVerdict(r.output).tone:r.state;},
      verificationClass(r){const v=r.verification;return v?.state==='hit'&&v.capability!=='people.phone.verify'?'verdict-'+this.emailVerdict(v.output).tone:'';},
      outcomeLabel(r){if(r.state==='hit')return this.run?.capability==='people.email.verify'?'Verdict: '+this.emailVerdict(r.output).label:this.run?.capability==='people.phone.verify'?'Verdict returned':'Found';return ({miss:'No match',error:'Error',timeout:'Timed out',not_attempted:'Not attempted',skipped:'Skipped',running:'Running',queued:'Queued',interrupted:'Interrupted',cancelled:'Cancelled'})[r.state]||r.state;},
      emptyLabel(r){return ({miss:'No matching data returned.',error:r.upstream_status===402?'This vendor’s credits or lookup allowance are exhausted. Try another vendor.':'This service could not complete the lookup.',timeout:'This service did not finish within the deadline.',not_attempted:'',skipped:'This step was skipped.',queued:'Waiting for its turn.',running:'Looking for an answer…',cancelled:'The attempt was stopped.',interrupted:'No complete result was recorded.'})[r.state]??'No fields returned.';},
      timeLeft(r){return (r.started_ms||0)/this.timelineEnd*100;},timeWidth(r){return Math.max(.5,(r.duration_ms||0)/this.timelineEnd*100);},
      openVendorRequest(){
        if(this.requestBusy)return;
        this.requestDone=false;this.requestError='';this.vendorPromptCopied=false;this.vendorPromptError='';
        this.requestQuery='Enrich Arena: '+this.taskId;
        this.$refs.vendorRequestDialog.showModal();
      },
      async copyVendorListingPrompt(){
        this.vendorPromptCopied=false;this.vendorPromptError='';
        try{await navigator.clipboard.writeText(this.vendorListingPrompt);this.vendorPromptCopied=true;}
        catch{this.vendorPromptError='Could not copy automatically. Select and copy the prompt above.';}
      },
      closeVendorRequest(){if(!this.requestBusy)this.$refs.vendorRequestDialog.close();},
      async submitVendorRequest(){
        if(this.requestBusy)return;
        const capability=this.requestForm.capability.trim();
        if(!capability){this.requestError='Say what tool or capability you need.';return;}
        this.requestBusy=true;this.requestError='';
        try{
          await this.api('/tool-requests',{method:'POST',body:JSON.stringify({capability,query:this.requestQuery,note:this.requestForm.note,contact:this.user?'':this.requestForm.contact,source:'web'})},null);
          this.requestDone=true;this.requestForm={capability:'',note:'',contact:''};
        }catch(e){this.requestError=e.status===429?'Too many requests from here — try again later.':'Could not send: '+(e.message||'Please try again.');}
        finally{this.requestBusy=false;}
      },
      async api(path,options={},teamOverride){
        const headers={'Content-Type':'application/json',...(options.headers||{})};
        const active=teamOverride===undefined?this.team:teamOverride;
        if(active)headers['X-Treg-Org']=active;
        const response=await fetch(path,{credentials:'same-origin',...options,headers});
        let body;try{body=await response.json();}catch{body={detail:'The server returned an unreadable response.'};}
        if(!response.ok){const d=body.detail;const e=new Error(typeof d==='string'?d:d?.message||body.message||'The request could not be completed.');e.status=response.status;throw e;}
        return body;
      },
      identity(){return Object.fromEntries(this.inputKeys.map(k=>[k,(this.inputs[k]||'').trim()]));},
      identities(){return this.inputRows.map(row=>Object.fromEntries(this.inputKeys.map(k=>[k,(row[k]||'').trim()])));},
      saveDraft(pending=false){if(this.leaderboard||this.benchmark)return;write(DRAFT,{taskId:this.taskId,variant:this.variant,inputs:this.inputs,extraInputs:this.extraInputs,mode:this.mode,autoVerify:this.autoVerify,customServices:this.customServices,services:this.services,pending,at:Date.now()});},
      restoreDraft(){const d=read(DRAFT);if(!d||Date.now()-d.at>600000){remove(DRAFT);return false;}if(!this.tasks.some(t=>t.id===d.taskId))return false;this.draftRestored=true;this.taskId=d.taskId;this.autoVerify=typeof d.autoVerify==='boolean'?d.autoVerify:['people.email.find','people.phone.find'].includes(d.taskId);this.variant=d.variant;this.inputs=d.inputs||{};this.extraInputs=Array.isArray(d.extraInputs)?d.extraInputs.slice(0,49):[];this.mode=d.mode==='compare'?'compare':'waterfall';this.customServices=!!d.customServices;this.services=d.services||[];return !!d.pending;},
      fillExampleInputs(){
        const rows=this.leaderboard||this.benchmark?[]:(this.currentTask.examples?.[this.variant]||[]);
        this.inputs={...(rows[0]||{})};this.extraInputs=rows.slice(1,2).map(row=>({...row}));
      },
      resetTaskInputs(){
        this.fillExampleInputs();this.quote=null;this.customServices=false;this.services=[];
        if(this.booted){this.run=null;this.linkedRunPending=false;clearTimeout(this.pollTimer);if(!this.leaderboard)remove(ACTIVE);this.saveDraft(false);this.syncUrl();}
      },
      chooseTask(id){
        if(this.busy||this.running||!this.tasks.some(t=>t.id===id))return;
        this.taskId=id;if(this.statsView==='verified'&&!['people.email.find','people.phone.find'].includes(id))this.statsView='rate';
        this.autoVerify=['people.email.find','people.phone.find'].includes(id);this.chartFocus=null;this.variant=0;this.resetTaskInputs();
      },
      chooseVariant(i){
        if(this.busy||this.running||!Number.isInteger(i)||i<0||i>=this.currentTask.variants.length)return;
        this.variant=i;this.resetTaskInputs();
      },setMode(m){if(this.busy||this.running||this.mode===m)return;this.mode=m;this.quote=null;},
      toggleProvider(provider){
        if(this.busy||this.running||!this.availableProviders.some(p=>p.provider===provider))return;
        const selected=this.enabledProviders;
        this.services=selected.includes(provider)?selected.filter(p=>p!==provider):[...selected,provider];
        this.customServices=true;this.quote=null;this.error='';this.saveDraft(false);
      },
      track(event,props={}){window.TregTracking?.capture(event,props);},
      identify(){window.TregTracking?.identify(this.user?.email||'',this.team);},
      shutdownIntercom(){
        try{if(this.intercomStarted)window.Intercom?.('shutdown');}catch{}
        this.intercomStarted=false;this.intercomIdentity='';delete window.intercomSettings;
      },
      syncIntercom(){
        const app=this.meta.intercom_app_id,user=this.user;
        if(!app||!user){this.shutdownIntercom();return;}
        if(this.intercomStarted&&this.intercomIdentity!==user.email)this.shutdownIntercom();
        const payload={app_id:app};
        // Match /app: never identify an email without the server's signed hash.
        if(user.email&&user.intercom_user_hash){
          payload.email=user.email;payload.user_hash=user.intercom_user_hash;
          if(this.team)payload.company={id:this.team,name:this.team};
        }
        try{
          if(!window.Intercom){
            const intercom=function(){intercom.q.push(arguments);};intercom.q=[];window.Intercom=intercom;
            const appId=encodeURIComponent(app),script=document.createElement('script');script.async=true;script.src='https://widget.intercom.io/widget/'+appId;
            document.head.appendChild(script);
          }
          window.intercomSettings=payload;
          window.Intercom(this.intercomStarted?'update':'boot',payload);
          this.intercomStarted=true;this.intercomIdentity=user.email;
        }catch{} // Support chat must not block Arena when the widget is unavailable.
      },
      async loadIdentity(){
        try{this.user=await this.api('/auth/me',{},'');}catch(e){if(e.status!==401)throw e;this.user=null;this.teams=[];this.team='';this.balance=null;this.history=[];this.historySequence++;this.identify();this.syncIntercom();return;}
        this.identify();
        const orgs=await this.api('/orgs');this.teams=orgs.filter(t=>!t.demo);
        let saved='';try{saved=localStorage.getItem('treg.arena.team')||'';}catch{}
        this.team=this.teams.find(t=>t.slug===this.team)?.slug||this.teams.find(t=>t.slug===saved)?.slug||this.teams[0]?.slug||'';
        this.identify();this.syncIntercom();
        if(this.team){await this.loadBalance();if(!this.leaderboard&&!this.benchmark)await this.refreshHistory();}
      },
      async loadBalance(){const t=this.teams.find(t=>t.slug===this.team);if(!t)return;const b=await this.api('/orgs/'+t.org_id+'/balance?limit=1');this.balance=b.balance_micro;},
      async changeTeam(){this.quote=null;this.run=null;this.history=[];this.historySequence++;clearTimeout(this.pollTimer);remove(ACTIVE);this.linkedRunPending=false;this.syncUrl();this.syncIntercom();try{localStorage.setItem('treg.arena.team',this.team);this.identify();await this.loadBalance();await this.refreshHistory();}catch(e){this.error=e.message;}},
      setupIcon(icon){return TregAgentSetup.iconUrl(icon);},
      async openSetup(){
        this.setupStep=this.user&&!this.team?0:1;this.setupTeamName='';this.setupToken=null;this.setupShowToken=false;this.setupCopied=false;this.setupError='';this.setupLoading=false;this.setupSequence++;
        try{const saved=localStorage.getItem('treg-agent');if([...TregAgentSetup.agents,...TregAgentSetup.moreAgents].some(a=>a.id===saved))this.setupAgentId=saved;}catch{}
        await this.$nextTick();this.$refs.setupDialog.showModal();
      },
      closeSetup(){if(this.setupStep===0&&this.setupLoading)return;this.setupSequence++;this.setupToken=null;this.setupShowToken=false;this.setupCopied=false;this.setupLoading=false;this.$refs.setupDialog.close();},
      async createSetupTeam(){
        if(this.setupLoading||!this.user)return;
        const name=this.setupTeamName.trim();
        if(!name){this.setupError='Give your team a name.';return;}
        this.setupLoading=true;this.setupError='';
        try{
          const created=await this.api('/orgs',{method:'POST',body:JSON.stringify({name})},'');
          this.team=created.org;this.setupStep=1;
          await this.loadIdentity();
        }catch(e){this.setupError=e.message;}
        finally{this.setupLoading=false;}
      },
      async prepareSetup(){
        if(this.setupLoading)return;
        if(this.user&&!this.team){this.setupStep=0;return;}
        const sequence=++this.setupSequence,team=this.team,user=this.user;
        this.setupLoading=true;this.setupError='';this.setupToken=null;this.setupShowToken=false;this.setupCopied=false;
        try{
          try{localStorage.setItem('treg-agent',this.setupAgentId);}catch{}
          if(user&&team){
            const result=await this.api('/auth/cli-token',{},team);
            if(sequence!==this.setupSequence||team!==this.team||user!==this.user)return;
            if(!result.token)throw new Error('Could not load your setup key. Please try again.');
            this.setupToken=result.token;
          }
          if(sequence===this.setupSequence)this.setupStep=2;
        }catch(e){if(sequence===this.setupSequence)this.setupError=e.message;}
        finally{if(sequence===this.setupSequence)this.setupLoading=false;}
      },
      showSetupExamples(){this.setupStep=3;this.setupShowToken=false;this.setupExampleCopied='';this.setupError='';},
      openSetupCatalog(service){this.closeSetup();location.assign(service?'/app/marketplace/'+encodeURIComponent(service):'/app#connections');},
      async copySetup(text,exampleKey=''){
        const sequence=this.setupSequence;this.setupError='';
        try{await navigator.clipboard.writeText(text);if(sequence!==this.setupSequence)return;this.setupCopied=!exampleKey;this.setupExampleCopied=exampleKey;setTimeout(()=>{if(sequence===this.setupSequence){this.setupCopied=false;this.setupExampleCopied='';}},1400);}
        catch{if(sequence===this.setupSequence)this.setupError='Could not copy. Select the setup text and copy it manually.';}
      },
      async openLogin(pending){this.track('arena_signup_opened');this.pendingSubmit=pending;this.authError='';this.emailStep='email';this.code='';this.devCode='';this.saveDraft(pending);await this.$nextTick();this.$refs.loginDialog.showModal();},
      closeLogin(){remove(SIGNUP_SETUP);this.pendingSubmit=false;this.saveDraft(false);this.$refs.loginDialog.close();},
      socialLogin(provider){this.saveDraft(this.pendingSubmit);write(SIGNUP_SETUP,{at:Date.now()});const target=(location.pathname||'/enrich-arena')+(location.search||'');location.assign('/auth/'+provider+'?return_to='+encodeURIComponent(target));},
      async finishSignup(){remove(SIGNUP_SETUP);this.pendingSubmit=false;this.saveDraft(false);await this.openSetup();},
      async resumeSignupSetup(){
        const pending=read(SIGNUP_SETUP);
        if(!pending)return false;
        if(!Number.isFinite(pending.at)||Date.now()-pending.at>600000||pending.at>Date.now()){remove(SIGNUP_SETUP);return false;}
        if(!this.user)return false;
        await this.finishSignup();return true;
      },
      async sendCode(){this.authBusy=true;this.authError='';try{const r=await this.api('/auth/email/start',{method:'POST',body:JSON.stringify({email:this.email})},'');this.emailStep='code';this.devCode=r.dev_code||'';}catch(e){this.authError=e.message;}finally{this.authBusy=false;}},
      async verifyEmail(){this.authBusy=true;this.authError='';try{await this.api('/auth/email/verify',{method:'POST',body:JSON.stringify({email:this.email,code:this.code})},'');this.$refs.loginDialog.close();await this.loadIdentity();await this.restoreLinkedRun();await this.finishSignup();}catch(e){this.authError=e.message;this.error=e.message;}finally{this.authBusy=false;}},
      async createTeam(){this.authBusy=true;this.authError='';try{await this.api('/orgs',{method:'POST',body:JSON.stringify({name:this.newTeamName})},'');this.$refs.teamDialog.close();await this.loadIdentity();await this.prepare();}catch(e){this.authError=e.message;}finally{this.authBusy=false;}},
      inputError(){
        if(this.inputRows.length>this.maxEntries)return 'Use at most '+this.maxEntries+' entries for this task.';
        const seen=new Set();
        for(const [i,row] of this.inputRows.entries()){
          const prefix=this.inputRows.length>1?'Entry '+(i+1)+': ':'';
          if(!this.inputKeys.length||this.inputKeys.some(k=>!(row[k]||'').trim()))return prefix+'Complete the query fields.';
          if(this.inputKeys.includes('country')&&!/^[A-Za-z]{2}$/.test(row.country.trim()))return prefix+'Use a two-letter country code, such as US or GB.';
          if(this.inputKeys.includes('full_name')){const parts=row.full_name.trim().split(/\s+/);if(parts.length<2||![parts[0],parts.at(-1)].every(p=>/\p{L}/u.test(p)))return prefix+'Enter both a first and last name for a name-based comparison, or use a LinkedIn URL.';}
          const fingerprint=JSON.stringify(this.inputKeys.map(k=>row[k].trim().toLowerCase()));
          if(seen.has(fingerprint))return prefix+'Duplicate entry. Remove it before running.';
          seen.add(fingerprint);
        }
        if(this.customServices&&!this.services.length)return 'Enable at least one vendor by clicking its avatar.';
        return '';
      },
      scheduleQuote(){
        clearTimeout(this.quoteTimer);this.quoteSequence++;this.quote=null;this.pricedKey='';this.pricing=false;
        if(this.leaderboard||this.benchmark||this.run&&!this.showProviderPreview||!this.booted||!this.user||!this.team||this.running||this.inputError())return;
        this.quoteTimer=setTimeout(()=>this.prepare(true),800);
      },
      async submit(){
        if(this.busy||this.running||this.pricing)return;
        this.error=this.inputError();if(this.error)return;
        this.saveDraft(false);
        if(!this.user){await this.openLogin(true);return;}
        if(!this.team){this.$refs.teamDialog.showModal();return;}
        if(!this.readyQuote){await this.prepare();return;}
        if(!this.readyQuote.affordable){this.topUp();return;}
        if(Date.parse(this.readyQuote.expires_at)<=Date.now()){await this.prepare();return;}
        await this.startRun();
      },
      async prepare(quiet=false){
        if(this.leaderboard||this.benchmark)return;
        clearTimeout(this.quoteTimer);
        if(!this.user||!this.team||this.running)return;
        const problem=this.inputError();if(problem){if(!quiet)this.error=problem;return;}
        const key=this.quoteKey,sequence=++this.quoteSequence,team=this.team;
        this.pricing=true;this.quote=null;this.error='';this.saveDraft(false);
        try{
          const q=await this.api('/arena/plans',{method:'POST',body:JSON.stringify({capability:this.taskId,...(this.extraInputs.length?{identities:this.identities()}:{identity:this.identity()}),mode:this.mode,auto_verify:this.autoVerify&&!!this.verificationTask,providers:this.customServices?this.services:null,max_cost_micro:10_000_000})},team);
          if(sequence!==this.quoteSequence||key!==this.quoteKey)return;
          this.quote=q;this.pricedKey=key;this.balance=q.balance_micro;
        }catch(e){if(sequence!==this.quoteSequence||key!==this.quoteKey)return;if(e.status===401){this.user=null;this.quote=null;if(!quiet)await this.openLogin(true);}else this.error=e.message;}
        finally{if(sequence===this.quoteSequence)this.pricing=false;}
      },
      topUp(){this.saveDraft(false);try{localStorage.setItem('treg-active',this.team);}catch{}this.track('arena_topup_clicked');location.assign('/app?from=enrich-arena#billing');},
      async startRun(){
        if(this.busy||this.running||!this.readyQuote)return;
        const id=this.readyQuote.id;this.busy=true;this.error='';this.selectedEntry=null;this.resultFilter='all';this.expandedResults=[];this.runTeam=this.team;clearTimeout(this.quoteTimer);
        try{await this.api('/arena/runs/'+id+'/start',{method:'POST'},this.runTeam);this.scrollOnComplete=id;this.quote=null;await this.pollRun(id);this.syncUrl(id);if(this.running)await this.refreshHistory();}
        catch(e){if(e.status===401){this.user=null;this.quote=null;await this.openLogin(true);}else if(e.status===402){this.topUp();}else{this.error=e.message;if(e.status===409){this.quote=null;await this.prepare(true);}}}
        finally{this.busy=false;}
      },
      async pollRun(id){
        clearTimeout(this.pollTimer);
        try{this.run=await this.api('/arena/runs/'+id,{},this.runTeam);this.pollFailures=0;
          if(this.run.state==='running')this.pollTimer=setTimeout(()=>this.pollRun(id),1500);
          else{await this.scrollToCompletedResults(id);await this.loadBalance();await this.refreshHistory();}
        }catch(e){this.error=e.message;this.pollFailures++;if(this.pollFailures<5&&e.status!==401&&e.status!==403&&e.status!==404)this.pollTimer=setTimeout(()=>this.pollRun(id),3000);}
      },
      async scrollToCompletedResults(id){
        if(this.scrollOnComplete!==id)return;
        this.scrollOnComplete='';
        await this.$nextTick();
        if(this.run?.id!==id||this.running||this.leaderboard||this.benchmark)return;
        const target='.results-section .run-cost-summary';
        document.querySelector(target)?.scrollIntoView({block:'start',behavior:window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches?'instant':'smooth'});
      },
      verificationEstimate(task){const prices=(this.tasks.find(t=>t.id===task)?.provider_previews?.[0]||[]).filter(p=>Number.isFinite(p.estimate_micro));return prices.length?Math.min(...prices.map(p=>p.estimate_micro)):null;},
      verificationLabel(r){const task=({'people.email.find':'people.email.verify','people.phone.find':'people.phone.verify'})[this.run?.capability],q=this.verificationQuotes[r.id],price=q?.estimate_micro??this.verificationEstimate(task);return (this.verificationPending[r.id]?'Verifying…':q?.affordable===false?'Top up':task==='people.phone.verify'?'Verify phone':'Verify email')+(price==null?'':' · '+this.usd(price));},
      verificationVerdict(r){const v=r.verification;if(!v)return '';if(v.not_started)return 'Verification not run';if(['queued','running'].includes(v.state))return 'Verifying…';if(v.state!=='hit')return 'Verification unavailable';const o=v.output||{};if(v.capability==='people.phone.verify')return o.valid===true?'Valid phone format':o.valid===false?'Invalid phone number':'Unknown';return this.emailVerdict(o).label;},
      async verifyResult(r){
        if(!r.can_verify||this.verificationPending[r.id])return;
        const id=this.run.id,team=this.runTeam,task=({'people.email.find':'people.email.verify','people.phone.find':'people.phone.verify'})[this.run.capability];
        this.verificationPending[r.id]=true;this.error='';
        try{
          let q=this.verificationQuotes[r.id];const displayed=q?.estimate_micro??this.verificationEstimate(task);
          if(!q||Date.parse(q.expires_at)<=Date.now()){
            q=await this.api('/arena/runs/'+id+'/attempts/'+r.id+'/verification/plan',{method:'POST'},team);
            if(this.run?.id!==id||this.runTeam!==team)return;
            this.verificationQuotes[r.id]=q;this.balance=q.balance_micro;
            if(displayed==null||q.estimate_micro>displayed){this.error='Review the verification price and click Verify again.';return;}
          }
          if(!q.affordable){this.topUp();return;}
          await this.api('/arena/runs/'+id+'/attempts/'+r.id+'/verification/start',{method:'POST',body:JSON.stringify({quote_id:q.id})},team);
          if(this.run?.id!==id||this.runTeam!==team)return;
          r.can_verify=false;this.run.state='running';delete this.verificationQuotes[r.id];await this.pollRun(id);
        }catch(e){if(e.status===402)this.topUp();else this.error=e.message;}finally{delete this.verificationPending[r.id];}
      },
      manualPrice(r){const q=this.manualQuotes[r.id];return q?.required_micro??((q?.estimate_micro??r.estimate_micro)+(this.run?.auto_verify?.estimate_micro||0));},
      manualUnaffordable(r){const q=this.manualQuotes[r.id],price=this.manualPrice(r);return price>0&&((Number.isFinite(this.balance)&&this.balance<price)||(q?.affordable===false));},
      manualLabel(r){const q=this.manualQuotes[r.id];return (this.manualPending[r.id]?'Starting…':this.manualUnaffordable(r)?'Top up':'Try')+' · '+this.usd(this.manualPrice(r));},
      ratingValue(r){return r.rating?.value || (r.report?'down':'');},
      openReport(r){if(this.isBatch&&!this.selectedVendor)this.selectedEntry=r.entry_index;this.reporting=r.id;this.reportDrafts[r.id] ||= {reason:r.report?.reason||'',comment:r.report?.comment||''};},
      async rateResult(r,value){
        if(this.busy||this.running)return;
        if(value==='down')this.openReport(r);else this.reporting='';
        if(r.rating?.value===value)return;
        const previous=r.rating;this.busy=true;this.error='';r.rating={value};
        try{const saved=await this.api('/arena/runs/'+this.run.id+'/attempts/'+r.id+'/rating',{method:'POST',body:JSON.stringify({value})},this.runTeam);r.rating=saved.rating;}
        catch(e){r.rating=previous;this.reporting='';this.error='Could not save your rating. '+e.message;}
        finally{this.busy=false;}
      },
      async sendReport(r){
        if(this.busy||this.running||r.report||this.ratingValue(r)!=='down')return;
        this.busy=true;this.error='';
        try{const saved=await this.api('/arena/runs/'+this.run.id+'/attempts/'+r.id+'/report',{method:'POST',body:JSON.stringify(this.reportDrafts[r.id])},this.runTeam);r.report=saved.report;this.reporting='';}
        catch(e){this.error=e.message;}finally{this.busy=false;}
      },
      async tryVendor(r){
        if(this.busy||this.manualPending[r.id]||!r.can_try)return;
        if(this.manualUnaffordable(r)){this.topUp();return;}
        const runId=this.run.id,team=this.runTeam;this.manualPending[r.id]=true;this.error='';
        try{
          let q=this.manualQuotes[r.id];const displayed=this.manualPrice(r);
          if(!q||Date.parse(q.expires_at)<=Date.now()){
            q=await this.api('/arena/runs/'+runId+'/attempts/'+r.id+'/plan',{method:'POST'},team);
            if(this.run?.id!==runId||this.runTeam!==team)return;
            this.manualQuotes[r.id]=q;this.balance=q.balance_micro;
            if(this.manualPrice(r)>displayed){this.error='This service’s price changed. Review the updated price and click Try again.';return;}
          }
          if(!q.affordable){this.topUp();return;}
          await this.api('/arena/runs/'+runId+'/attempts/'+r.id+'/start',{method:'POST',body:JSON.stringify({quote_id:q.id})},team);
          if(this.run?.id!==runId||this.runTeam!==team)return;
          r.can_try=false;r.state='queued';r.manual=true;this.run.state='running';
          delete this.manualQuotes[r.id];this.quote=null;this.reporting='';
          await this.pollRun(runId);await this.refreshHistory();
        }catch(e){delete this.manualQuotes[r.id];if(e.status===402)this.topUp();else this.error=e.message;}
        finally{delete this.manualPending[r.id];}
      },
      async cancelRun(){this.busy=true;try{await this.api('/arena/runs/'+this.run.id+'/cancel',{method:'POST'},this.runTeam);}catch(e){this.error=e.message;}finally{this.busy=false;}},
      historyDate(value){return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(value));},
      async refreshHistory(){
        const team=this.team,sequence=++this.historySequence;
        if(!this.user||!team){this.history=[];this.historyHasMore=false;this.historyLoading=false;return;}
        this.historyLoading=true;
        try{const rows=await this.api('/arena/runs?limit=31',{},team);if(sequence===this.historySequence&&team===this.team&&this.user){this.history=rows.slice(0,30);this.historyHasMore=rows.length>30;}}
        catch(e){if(sequence===this.historySequence&&team===this.team)this.error=e.message;}
        finally{if(sequence===this.historySequence)this.historyLoading=false;}
      },
      async loadMoreHistory(){
        if(this.historyLoading||!this.historyHasMore||!this.history.length||!this.user||!this.team||this.busy||this.running)return;
        const team=this.team,sequence=++this.historySequence,before=this.history.at(-1).id;
        this.historyLoading=true;
        try{
          const rows=await this.api('/arena/runs?limit=31&before='+encodeURIComponent(before),{},team);
          if(sequence!==this.historySequence||team!==this.team||!this.user)return;
          const seen=new Set(this.history.map(row=>row.id));
          this.history.push(...rows.slice(0,30).filter(row=>!seen.has(row.id)));
          this.historyHasMore=rows.length>30;
        }catch(e){if(sequence===this.historySequence&&team===this.team)this.error=e.message;}
        finally{if(sequence===this.historySequence)this.historyLoading=false;}
      },
      async loadHistory(id,{replace=false}={}){
        if(this.busy||this.running)return;
        this.busy=true;this.error='';clearTimeout(this.pollTimer);
        try{
          const result=await this.api('/arena/runs/'+encodeURIComponent(id),{},this.team);
          this.runTeam=this.team;this.run=result;this.expandedResults=[];this.manualQuotes={};this.reporting='';this.pollFailures=0;
          this.taskId=result.capability;this.autoVerify=!!result.auto_verify;this.mode=result.mode;this.inputs={...result.identity};this.extraInputs=(result.identities||[]).slice(1).map(r=>({...r}));this.selectedEntry=null;this.resultFilter='all';this.showAllEntries=false;
          this.variant=Math.max(0,this.currentTask.variants.findIndex(v=>v.every(k=>k in this.inputs)));
          this.customServices=true;this.services=[...new Set(result.results.map(r=>r.provider))];this.quote=null;this.saveDraft(false);
          this.syncUrl(id,replace);
          if(this.running){this.scrollOnComplete=id;this.pollTimer=setTimeout(()=>this.pollRun(id),1500);}
        }catch(e){this.error=[403,404,410].includes(e.status)?'This saved run is unavailable, expired, or belongs to another account.':e.message;}finally{this.busy=false;}
      },
      async newSession(){
        if(this.busy||this.running)return;
        this.selectedEntry=null;this.variant=0;this.fillExampleInputs();this.customServices=false;this.services=[];this.error='';
        await this.newQuery();this.saveDraft(false);document.querySelector('#composer input')?.focus();
      },
      async newQuery(){this.manualQuotes={};this.reporting='';this.expandedResults=[];this.run=null;this.quote=null;this.linkedRunPending=false;clearTimeout(this.pollTimer);remove(ACTIVE);this.syncUrl();this.scheduleQuote();await this.$nextTick();document.querySelector('#composer')?.scrollIntoView({behavior:'smooth'});},
      tryWaterfall(){const previous=this.run;this.taskId=previous.capability;this.inputs={...previous.identity};this.extraInputs=(previous.identities||[]).slice(1).map(r=>({...r}));this.variant=Math.max(0,this.currentTask.variants.findIndex(v=>v.every(k=>k in previous.identity)));this.mode='waterfall';this.customServices=false;this.services=[];this.newQuery();},
      exportResult(){const b=new Blob([JSON.stringify(this.run,null,2)],{type:'application/json'});const url=URL.createObjectURL(b);const a=document.createElement('a');a.href=url;a.download='enrich-arena-'+this.run.id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);},
      async logout(){try{await this.api('/auth/logout',{method:'POST'});this.shutdownIntercom();this.user=null;this.teams=[];this.team='';this.balance=null;this.run=null;this.quote=null;this.history=[];this.historySequence++;remove(ACTIVE);remove(DRAFT);}catch(e){this.error=e.message;}}
    },
    async mounted(){
      this.track('arena_page_viewed');
      this.booted=false;this.urlPopHandler=()=>location.reload();window.addEventListener?.('popstate',this.urlPopHandler);
      if(this.benchmark){
        document.title='People Search Bench — treg';
        await this.loadBenchmark();
        try{this.meta=await this.api('/meta',{},'');await this.loadIdentity();await this.resumeSignupSetup();}catch(e){this.error=e.message;}
        this.booted=true;return;
      }
      this.loadInsights();
      this.insightsTimer=setInterval(()=>{if(!document.hidden)this.loadInsights();},120000);
      try{
        const [tasks,meta]=await Promise.all([this.api('/arena/tasks',{},''),this.api('/meta',{},'')]);this.tasks=this.leaderboard?tasks.filter(t=>!t.discovery):tasks;this.meta=meta;
        const params=new URLSearchParams(location.search),explicitTask=params.has('capability'),linkedRun=params.has('run'),pending=this.leaderboard||linkedRun?false:this.restoreDraft();
        const preset=params.get('capability');if(this.tasks.some(t=>t.id===preset)&&this.taskId!==preset)this.chooseTask(preset);
        if(explicitTask&&!params.has('variant')&&this.variant!==0)this.chooseVariant(0);
        const variant=Number(params.get('variant'));if(params.has('variant')&&Number.isInteger(variant)&&variant>=0&&variant<this.currentTask.variants.length&&this.variant!==variant)this.chooseVariant(variant);
        if(!this.leaderboard&&!linkedRun&&!this.draftRestored)this.fillExampleInputs();
        const presetMode=params.get('mode');if(['compare','waterfall'].includes(presetMode))this.mode=presetMode;
        await this.loadIdentity();this.booted=true;
        if(this.leaderboard){document.title='Enrichment Leaderboard — treg';await this.resumeSignupSetup();return;}
        const restored=await this.restoreLinkedRun();
        if(await this.resumeSignupSetup()||restored)return;
        if(pending&&this.user&&!explicitTask){this.saveDraft(false);if(this.team)await this.prepare();else this.$refs.teamDialog.showModal();}
        if(!pending||explicitTask)this.scheduleQuote();
      }catch(e){this.error=e.message;}
    },
    beforeUnmount(){window.removeEventListener?.('popstate',this.urlPopHandler);clearInterval(this.insightsTimer);clearTimeout(this.pollTimer);clearTimeout(this.quoteTimer);this.quoteSequence++;this.historySequence++;}
  }).mount('#arena');
})();
