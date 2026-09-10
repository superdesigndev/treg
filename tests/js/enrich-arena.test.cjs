const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../src/treg/web/enrich-arena/arena.js'),'utf8');
function setup(location={}){
  let options;const stored=new Map(),destinations=[];
  const storage={getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)};
  const Vue={createApp:o=>{options=o;return {mount(){}};}};
  const runtime={Vue,window:{Vue},sessionStorage:storage,localStorage:storage,location:{assign:x=>destinations.push(x),...location},setTimeout:()=>1,clearTimeout(){},document:{querySelector:()=>null},URLSearchParams,Intl,Date};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../src/treg/web/agent-setup.js'),'utf8'),runtime);
  vm.runInNewContext(source,runtime);
  const app=options.data();for(const [k,f]of Object.entries(options.methods))app[k]=f.bind(app);
  for(const [k,f]of Object.entries(options.computed))Object.defineProperty(app,k,{get:f.bind(app)});
  app.tasks=[{id:'people.email.find',variants:[['full_name','domain']],fields:[]}];
  app.inputs={full_name:'Test Person',domain:'example.com'};app.user={id:1};app.team='test-team';app.booted=true;app.$nextTick=async()=>{};
  const quote=(extra={})=>({id:'q1',required_micro:25000,balance_micro:1000000,affordable:true,expires_at:new Date(Date.now()+60000).toISOString(),...extra});
  const price=(extra={})=>{app.quote=quote(extra);app.pricedKey=app.quoteKey;};
  return {app,quote,price,stored,destinations,components:options.components,directives:options.directives,runtime,mounted:options.mounted};
}
test('Intercom loads only for authenticated users on opted-in deployments and reuses its loader',()=>{
 const {app,runtime}=setup(),scripts=[];
 runtime.document.createElement=()=>({});runtime.document.head={appendChild:s=>scripts.push(s)};
 app.meta={intercom_app_id:'test-app'};app.user=null;app.syncIntercom();assert.equal(scripts.length,0);
 app.user={email:'test@example.com',intercom_user_hash:'signed-test-hash'};app.meta={};app.syncIntercom();assert.equal(scripts.length,0);
 app.meta={intercom_app_id:'test-app'};app.syncIntercom();app.syncIntercom();
 assert.equal(scripts.length,1);assert.equal(scripts[0].src,'https://widget.intercom.io/widget/test-app');
 const calls=runtime.window.Intercom.q;
 assert.equal(calls[0][0],'boot');assert.equal(calls[1][0],'update');assert.equal(calls[0][1].user_hash,'signed-test-hash');
 assert.equal(calls[0][1].email,'test@example.com');assert.equal(calls[0][1].company.id,'test-team');
});
test('Intercom does not send unhashed identity and clears conversations between accounts',async()=>{
 const {app,runtime}=setup(),calls=[];runtime.window.Intercom=(...args)=>calls.push(args);
 app.meta={intercom_app_id:'test-app'};app.user={email:'first@example.com'};app.syncIntercom();
 assert.deepEqual(Object.keys(calls[0][1]),['app_id']);
 app.user={email:'second@example.com',intercom_user_hash:'second-hash'};app.syncIntercom();
 assert.deepEqual(calls.map(c=>c[0]),['boot','shutdown','boot']);
 app.api=async()=>({});await app.logout();assert.equal(calls.at(-1)[0],'shutdown');assert.equal(runtime.window.intercomSettings,undefined);
 app.syncIntercom();assert.equal(calls.length,4);
 app.user={email:'first@example.com',intercom_user_hash:'first-hash'};app.syncIntercom();assert.equal(calls.at(-1)[0],'boot');
 app.api=async()=>{throw {status:401};};await app.loadIdentity();assert.equal(calls.at(-1)[0],'shutdown');assert.equal(app.intercomStarted,false);
});
test('Identity loading boots Intercom with the selected team, and team changes update it',async()=>{
 const {app,runtime,stored}=setup(),calls=[];runtime.window.Intercom=(...args)=>calls.push(args);
 app.meta={intercom_app_id:'test-app'};stored.set('treg.arena.team','second-team');
 app.api=async path=>{if(path==='/auth/me')return {email:'test@example.com',intercom_user_hash:'signed-test-hash'};if(path==='/orgs')return [{slug:'first-team'},{slug:'second-team'}];throw Error(path);};
 app.loadBalance=async()=>{};app.refreshHistory=async()=>{};
 await app.loadIdentity();assert.equal(calls.length,1);assert.equal(calls[0][0],'boot');assert.equal(calls[0][1].company.id,'second-team');
 app.team='first-team';await app.changeTeam();assert.equal(calls.at(-1)[0],'update');assert.equal(calls.at(-1)[1].company.id,'first-team');
 runtime.window.Intercom=()=>{throw Error('Widget blocked');};await app.changeTeam();assert.equal(app.error,'');
});
test('Page-scrolling headers stop at table bounds, offset nested headers, and clean up listeners',()=>{
 const {directives,runtime}=setup(),listeners=new Map();let pending,offset,disconnected=false;
 runtime.requestAnimationFrame=fn=>{pending=fn;return 1;};runtime.cancelAnimationFrame=()=>{pending=null;};
 runtime.ResizeObserver=class{observe(){}disconnect(){disconnected=true;}};
 runtime.window.addEventListener=(name,fn)=>listeners.set(name,fn);
 runtime.window.removeEventListener=name=>listeners.delete(name);
 const bounds={top:100,height:1000},head={getBoundingClientRect:()=>({height:40}),style:{setProperty:(key,value)=>{offset=value;}}};
 const table={tHead:head,getBoundingClientRect:()=>bounds,closest:()=>null,querySelector:()=>null};
 directives.stickyHeader.mounted(table);pending();assert.equal(offset,'0px');
 bounds.top=-250;listeners.get('scroll')();pending();assert.equal(offset,'250px');
 let entryOffset=0,end=600;
 const row={getBoundingClientRect:()=>({top:-200+entryOffset,height:80}),nextElementSibling:{getBoundingClientRect:()=>({bottom:end})},style:{setProperty:(key,value)=>{entryOffset=parseFloat(value);},removeProperty:()=>{entryOffset=0;}}};
 table.querySelector=()=>row;directives.stickyHeader.updated(table);pending();assert.equal(entryOffset,240);
 listeners.get('scroll')();pending();assert.equal(entryOffset,240,'Repeated scroll updates must not drift');
 end=20;listeners.get('scroll')();pending();assert.equal(entryOffset,140,'Entry stops at the end of its details');
 runtime.window.matchMedia=()=>({matches:true});listeners.get('resize')();pending();
 assert.equal(offset,'0px');assert.equal(entryOffset,0,'Mobile cards must not be covered by a pinned overview');
 runtime.window.matchMedia=()=>({matches:false});listeners.get('resize')();pending();assert.equal(entryOffset,140);
 table.querySelector=()=>null;directives.stickyHeader.updated(table);pending();assert.equal(entryOffset,0);
 table.closest=()=>({closest:()=>({tHead:head,querySelector:()=>row})});listeners.get('scroll')();pending();assert.equal(offset,'370px');
 bounds.top=-2000;listeners.get('scroll')();pending();assert.equal(offset,'960px');
 directives.stickyHeader.unmounted(table);assert.equal(listeners.size,0);assert.equal(disconnected,true);
});
for(const mode of ['compare','waterfall'])for(const batch of [false,true])test(`${mode} completion reveals ${batch?'entries':'single-entry results'} once, including fast runs`,async()=>{
 const {app,price,runtime}=setup(),scrolls=[];let rendered=false;
 app.mode=mode;if(batch)app.extraInputs=[{full_name:'Second Person',domain:'second.example'}];price();
 runtime.document.querySelector=selector=>({scrollIntoView:options=>{assert.equal(rendered,true);scrolls.push({selector,...options});}});
 app.$nextTick=async()=>{rendered=true;};app.loadBalance=async()=>{};app.refreshHistory=async()=>{};
 const result={id:'q1',state:'completed',capability:app.taskId,mode,identity:app.inputs,identities:app.inputRows,results:[]};
 app.api=async(path,options)=>options?.method==='POST'?{}:{...result};
 await app.startRun();assert.equal(scrolls.length,1);assert.equal(scrolls[0].selector,'.results-section .run-cost-summary');assert.equal(scrolls[0].behavior,'smooth');
 await app.pollRun('q1');assert.equal(scrolls.length,1,'A repeated completion poll must not scroll again');
 app.run.state='running';await app.pollRun('q1');assert.equal(scrolls.length,1,'Later Try/Verify calls must not scroll again');
});
test('Result scroll honors reduced motion and ignores a run replaced before rendering',async()=>{
 const {app,runtime}=setup(),scrolls=[];runtime.window.matchMedia=()=>({matches:true});
 runtime.document.querySelector=()=>({scrollIntoView:o=>scrolls.push(o)});
 app.run={id:'run',state:'completed'};app.scrollOnComplete='run';await app.scrollToCompletedResults('run');assert.equal(scrolls[0].behavior,'instant');
 app.scrollOnComplete='run';app.$nextTick=async()=>{app.run=null;};await app.scrollToCompletedResults('run');assert.equal(scrolls.length,1);
});
test('Opening completed history does not scroll, but a resumed running history scrolls on completion',async()=>{
 const {app,runtime}=setup(),scrolls=[];runtime.document.querySelector=()=>({scrollIntoView:o=>scrolls.push(o)});
 app.loadBalance=async()=>{};app.refreshHistory=async()=>{};
 let state='completed';app.api=async()=>({id:'saved',state,capability:app.taskId,mode:'waterfall',identity:app.inputs,results:[]});
 await app.loadHistory('saved');assert.equal(scrolls.length,0);assert.equal(app.scrollOnComplete,'');
 state='running';await app.loadHistory('saved');assert.equal(scrolls.length,0);assert.equal(app.scrollOnComplete,'saved');
 state='completed';await app.pollRun('saved');assert.equal(scrolls.length,1);
});
test('Waterfall is the default and the priced button includes its estimate',()=>{
 const {app,price}=setup();assert.equal(app.mode,'waterfall');price();assert.equal(app.runButtonLabel,'Run from $0.025');
});
test('Vendor drilldown includes only attempts and keeps feedback in the selected view',()=>{
 const {app}=setup();app.run={identities:[{domain:'one.example'},{domain:'two.example'}],results:['hit','miss','error','timeout','running','queued','skipped','not_attempted'].map((state,i)=>({id:'r'+i,provider:'hunter',state,entry_index:i%2}))};
 app.selectedEntry=0;app.selectVendor({provider:'hunter',attempted:5});
 assert.equal(app.selectedEntry,null);assert.equal(app.vendorResults.length,5);
 assert.deepEqual(Array.from(app.vendorResults,r=>r.state),['hit','miss','error','timeout','running']);
 app.openReport(app.run.results[0]);assert.equal(app.selectedEntry,null);assert.equal(app.reporting,'r0');
 app.selectVendor({provider:'tomba',attempted:0});assert.equal(app.selectedVendor,'hunter');
 app.selectVendor({provider:'hunter',attempted:5});assert.equal(app.selectedVendor,'');assert.equal(app.reporting,'');
 app.selectVendor({provider:'hunter',attempted:5});app.selectEntry(1);assert.equal(app.selectedVendor,'');assert.equal(app.selectedEntry,1);
});
test('Adding and removing entries invalidates pricing and preserves neighboring inputs',()=>{
 const {app,price}=setup();price();app.addEntry();assert.equal(app.inputRows.length,2);assert.equal(app.readyQuote,null);
 app.extraInputs[0]={full_name:'Second Person',domain:'second.example'};price();assert.match(app.runButtonLabel,/Run 2 entries from/);
 app.removeEntry(0);assert.equal(app.inputs.full_name,'Second Person');assert.equal(app.inputRows.length,1);assert.equal(app.readyQuote,null);
 app.removeEntry(0);assert.equal(app.inputRows.length,1);
});
function paste(app,text,index=0){let prevented=false;app.pasteEntries({clipboardData:{getData:()=>text},preventDefault(){prevented=true;}},index);return prevented;}
test('Spreadsheet paste maps headers and quoted CSV, preserving other rows',()=>{
 const {app}=setup();app.addEntry();app.extraInputs[0]={full_name:'Keep Person',domain:'keep.example'};
 assert.equal(paste(app,'Domain\tFull name\nfirst.example\tFirst Person\nsecond.example\tSecond Person'),true);
 assert.equal(app.inputRows.length,3);assert.equal(app.inputs.full_name,'First Person');assert.equal(app.extraInputs[1].full_name,'Keep Person');
 assert.equal(paste(app,'"Comma, Person",third.example\nOther Person,fourth.example',1),true);
 assert.equal(app.extraInputs[0].full_name,'Comma, Person');assert.equal(app.inputRows.length,4);
});
test('Paste rejects overflow and malformed columns without altering the list; duplicates block spending',async()=>{
 const {app}=setup();const original=JSON.stringify(app.inputRows);
 paste(app,'One\tTwo\tThree\nFour\tFive\tSix');assert.equal(JSON.stringify(app.inputRows),original);assert.match(app.error,/Paste 2 columns/);
 paste(app,Array.from({length:51},(_,i)=>`Person ${i}\tcompany${i}.com`).join('\n'));assert.equal(JSON.stringify(app.inputRows),original);assert.match(app.error,/50 entries/);
 paste(app,'Same Person\tone.example\nSame Person\tone.example');assert.match(app.inputError(),/Duplicate/);
 app.api=()=>assert.fail('Invalid input must not price or spend');await app.submit();
});
test('Batch pricing sends all identities once and a later-row edit discards an in-flight price',async()=>{
 const {app,quote}=setup();app.addEntry();app.extraInputs[0]={full_name:'Second Person',domain:'second.example'};
 let body,release;app.api=async(path,options)=>{body=JSON.parse(options.body);return new Promise(r=>release=r);};
 const pending=app.prepare();assert.equal(body.identities.length,2);assert.equal(body.identity,undefined);
 app.extraInputs[0].domain='changed.example';release(quote());await pending;assert.equal(app.readyQuote,null);
});
test('Batch entries survive login draft and history, with one vendor selection per provider',async()=>{
 const {app}=setup();app.addEntry();app.extraInputs[0]={full_name:'Second Person',domain:'second.example'};app.saveDraft(true);
 app.extraInputs=[];assert.equal(app.restoreDraft(),true);assert.equal(app.extraInputs.length,1);
 app.api=async()=>({id:'batch',capability:app.taskId,mode:'compare',state:'completed',identity:app.inputs,identities:app.identities(),results:[{provider:'hunter',entry_index:0},{provider:'hunter',entry_index:1}]});
 await app.loadHistory('batch');assert.equal(app.extraInputs.length,1);assert.equal(app.services.length,1);assert.equal(app.showProviderPreview,false);
 app.extraInputs[0].domain='changed.example';assert.equal(app.showProviderPreview,true);
});
function batchResults(app){
 app.run={id:'batch',capability:app.taskId,identity:app.identity(),identities:[app.identity(),{full_name:'Second Person',domain:'second.example'}],mode:'compare',state:'completed',fields:['email'],results:[
  {id:'a0',provider:'tomba',entry_index:0,state:'hit',output:{email:'a@first.example'},charged_micro:10,duration_ms:10},
  {id:'a1',provider:'tomba',entry_index:1,state:'hit',output:{email:'a@second.example'},charged_micro:10,duration_ms:30},
  {id:'b0',provider:'hunter',entry_index:0,state:'hit',output:{email:'b@first.example'},charged_micro:5,duration_ms:5},
  {id:'b1',provider:'hunter',entry_index:1,state:'miss',output:{},charged_micro:5,duration_ms:5}]};
}
test('Batch awards compare coverage, cost including misses, and median; a single thumb approves only its cell',()=>{
 const {app}=setup();batchResults(app);
 assert.equal(app.winnerIds.join(),'tomba');assert.equal(app.batchVendors[0].median,20);
 assert.equal(app.batchVendors[1].costPerKept,10);assert.equal(app.batchAwards.hunter.includes('Cheapest'),true);
 app.run.results[2].rating={value:'up'};assert.equal(app.winnerIds.join(),'tomba');
 app.run.results[0].rating={value:'down'};assert.equal(app.batchVendors[0].kept,1);assert.equal(app.batchVendors[0].found,2);
 assert.equal(app.winnerIds.length,2);assert.equal(app.batchAwards.tomba.includes('Cheapest'),false);
 app.run.results[1].rating={value:'down'};assert.equal(app.winnerIds.join(),'hunter');
 app.run.mode='waterfall';assert.equal(Object.keys(app.batchAwards).length,0);assert.equal(app.winnerIds.length,0);
});
test('Unknown charges, partial runs and running batches do not receive misleading awards',()=>{
 const {app}=setup();batchResults(app);app.run.results[1].charged_micro=null;
 assert.equal(Object.values(app.batchAwards).flat().includes('Cheapest'),false);
 app.run.results[1].state='not_attempted';assert.equal(Object.keys(app.batchAwards).length,0);
 app.run.state='running';assert.equal(app.winnerIds.length,0);
});
test('Batch matrix filters and feedback keep the correct entry selected',()=>{
 const {app}=setup();batchResults(app);app.resultFilter='disagreement';assert.equal(app.matrixEntries.length,1);
 app.run.results[1].rating={value:'down'};app.resultFilter='unresolved';assert.equal(app.matrixEntries[0].index,1);
 app.openReport(app.run.results[1]);assert.equal(app.selectedEntry,1);assert.equal(app.visibleResults.length,2);assert.equal(app.reporting,'a1');
 assert.match(app.domainMismatch(app.run.results[0]),/example.com/);assert.equal(app.domainMismatch(app.run.results[1]),'');
});
test('Entry details toggle in place and row actions do not toggle their parent',()=>{
 const {app}=setup();batchResults(app);
 app.selectEntry(0);assert.equal(app.selectedEntry,0);
 app.selectEntry(0);assert.equal(app.selectedEntry,null);
 app.selectEntry(1,app.run.results[1]);assert.equal(app.selectedEntry,1);assert.equal(app.expandedResults.join(),'a1');
 app.toggleEntryRow({target:{closest:()=>true}},1);assert.equal(app.selectedEntry,1);
 app.toggleEntryRow({target:{closest:()=>null}},1);assert.equal(app.selectedEntry,null);
 for(const state of ['queued','skipped','not_attempted'])assert.equal(app.hasAttempt({state}),false);
 for(const state of ['running','hit','miss','error','timeout','cancelled'])assert.equal(app.hasAttempt({state}),true);
});
test('Submitting a priced query starts directly, once, and consumes the quote',async()=>{
 const {app,price}=setup();price();let calls=0,release;app.api=()=>{calls++;return new Promise(r=>release=r);};app.pollRun=async()=>{app.run={state:'completed'};};
 const run=app.submit();await app.submit();assert.equal(calls,1);release({});await run;assert.equal(app.quote,null);
});
test('Pricing alone does not start or open a confirmation modal',async()=>{
 const {app,quote}=setup();const calls=[];app.api=async p=>{calls.push(p);return quote();};await app.submit();assert.deepEqual(calls,['/arena/plans']);assert.equal(app.readyQuote.id,'q1');
});
test('An edit invalidates a previous price and discards an in-flight response',async()=>{
 const {app,quote,price}=setup();price();let release;app.api=()=>new Promise(r=>release=r);const pricing=app.prepare();app.inputs.full_name='Different Person';app.scheduleQuote();release(quote());await pricing;assert.equal(app.readyQuote,null);assert.equal(app.pricing,false);
});
test('Changing input makes a quote unusable even before the watcher runs',()=>{
 const {app,price}=setup();price();app.mode='compare';assert.equal(app.readyQuote,null);
});
test('Insufficient credits go to the selected team billing page without dispatch',async()=>{
 const {app,price,destinations,stored}=setup();price({affordable:false});app.api=()=>assert.fail('Must not dispatch');await app.submit();assert.deepEqual(destinations,['/app?from=enrich-arena#billing']);assert.equal(stored.get('treg-active'),'test-team');assert.equal(JSON.parse(stored.get('treg.arena.draft.v1')).pending,false);
});
test('Anonymous submission triggers login without sending a plan or paid request',async()=>{
 const {app}=setup();app.user=null;let login=false;app.openLogin=async pending=>{login=pending;};app.api=()=>assert.fail('Must not call before login');await app.submit();assert.equal(login,true);
});
test('Expired quotes refresh on the button without buying at an unseen price',async()=>{
 const {app,price,quote}=setup();price({expires_at:new Date(Date.now()-1000).toISOString()});const calls=[];app.api=async p=>{calls.push(p);return quote({required_micro:50000});};await app.submit();assert.deepEqual(calls,['/arena/plans']);assert.match(app.runButtonLabel,/0.05/);
});
test('Incomplete names never request pricing',async()=>{
 const {app}=setup();app.inputs.full_name='Test';app.api=()=>assert.fail('Must validate first');await app.submit();assert.match(app.error,/first and last name/);
});
test('A balance consumed elsewhere redirects a rejected start to top-up',async()=>{
 const {app,price,destinations}=setup();price();app.api=async()=>{throw Object.assign(new Error('Not enough credits'),{status:402});};await app.submit();assert.deepEqual(destinations,['/app?from=enrich-arena#billing']);assert.equal(app.busy,false);
});

test('Vendor estimates appear before login or a complete input and follow input type',()=>{
 const {app}=setup();app.user=null;app.inputs={};app.tasks[0].variants.push(['linkedin_url']);
 app.tasks[0].provider_previews=[[{provider:'hunter',estimate_micro:24500}],[{provider:'fiber-ai',estimate_micro:40000}]];
 assert.equal(app.showProviderPreview,true);assert.equal(app.previewProviders[0].provider,'hunter');
 app.chooseVariant(1);assert.equal(app.previewProviders[0].provider,'fiber-ai');
 app.customServices=true;app.services=[];assert.equal(app.previewProviders.length,0);
});
test('Current team quotes replace catalog pricing, including own keys',()=>{
 const {app,price}=setup();app.tasks[0].provider_previews=[[{provider:'hunter',estimate_micro:24500}]];
 price({providers:[{provider:'hunter',estimate_micro:0,tier:'credential'}]});
 assert.equal(app.previewProviders[0].estimate_micro,0);assert.match(app.billingLabel(app.previewProviders[0]),/Your key/);
 app.inputs.domain='changed.com';assert.equal(app.previewProviders[0].estimate_micro,24500);
});
test('Results replace the matching preview; changing a query restores pricing without losing results',()=>{
 const {app}=setup();app.run={state:'completed',capability:app.taskId,mode:app.mode,identity:app.identity(),results:[]};
 assert.equal(app.showProviderPreview,false);app.inputs.domain='changed.com';assert.equal(app.showProviderPreview,true);assert.equal(app.run.state,'completed');
 app.run.state='running';assert.equal(app.showProviderPreview,false);
});

test('Fighters follow real attempt states and never label uncalled or failed services defeated',()=>{
 const {components}=setup();const component=components.ArenaFighters;const fighter={active:true,selected:[]};
 for(const [name,fn] of Object.entries(component.methods))fighter[name]=fn.bind(fighter);
 assert.equal(fighter.state({state:'running'}),'fighting');assert.equal(fighter.state({state:'queued'}),'waiting');
 assert.equal(fighter.state({state:'hit'}),'won');assert.equal(fighter.state({state:'miss'}),'defeated');
 for(const state of ['error','timeout'])assert.equal(fighter.state({state}),'error');
 for(const state of ['skipped','not_attempted'])assert.equal(fighter.state({state}),'benched');
 for(const state of ['cancelled','interrupted'])assert.equal(fighter.state({state}),'paused');
 fighter.active=false;assert.equal(fighter.state({state:'running'}),'paused');
 fighter.selected=['winner'];assert.equal(fighter.state({id:'winner',state:'hit'}),'champion');
 assert.equal(fighter.label({id:'winner',state:'hit'}),'Winner');
});

test('Battle mode selection invalidates pricing without dispatch and is locked during a run',()=>{
 const {app,price,components}=setup();price();const emitted=[];
 const fighter={selectable:true,disabled:false,active:false,mode:app.mode,$emit:(event,mode)=>{emitted.push(event);app.setMode(mode);}};
 const choose=components.ArenaFighters.methods.chooseMode.bind(fighter);
 app.api=()=>assert.fail('Selecting a mode must not dispatch');choose('compare');
 assert.equal(app.mode,'compare');assert.equal(app.readyQuote,null);assert.deepEqual(emitted,['change-mode']);
 fighter.active=true;choose('waterfall');assert.equal(emitted.length,1);
 fighter.active=false;fighter.disabled=true;choose('waterfall');assert.equal(emitted.length,1);
 app.run={state:'running'};app.setMode('waterfall');assert.equal(app.mode,'compare');
});

test('Session history is loaded without spending and stale team responses are discarded',async()=>{
 const {app}=setup();let release;app.api=()=>new Promise(r=>release=r);const pending=app.refreshHistory();
 app.team='different-team';release([{id:'old-team-session'}]);await pending;assert.equal(app.history.length,0);
 app.api=async path=>{assert.equal(path,'/arena/runs?limit=31');return [];};await app.refreshHistory();assert.equal(app.history.length,0);
 app.user=null;app.api=()=>assert.fail('Anonymous visitors must not fetch private history');await app.refreshHistory();
});
test('Selecting a session restores its inputs and mode using only a read',async()=>{
 const {app,stored}=setup();const calls=[];app.quote={id:'old-quote'};
 app.api=async(path,options)=>{calls.push(path);assert.equal(options.method,undefined);return {id:'saved',state:'completed',capability:app.taskId,mode:'compare',identity:{full_name:'Saved Person',domain:'saved.com'},results:[]};};
 await app.loadHistory('saved');assert.deepEqual(calls,['/arena/runs/saved']);assert.equal(app.inputs.full_name,'Saved Person');assert.equal(app.mode,'compare');assert.equal(app.quote,null);assert.equal(app.run.id,'saved');assert.equal(stored.has('treg.arena.active.v1'),false);
});
test('Failed session selection keeps the existing session and does not persist a failed id',async()=>{
 const {app,stored}=setup();app.run={id:'existing',state:'completed'};app.api=async()=>{throw new Error('Unavailable');};
 await app.loadHistory('missing');assert.equal(app.run.id,'existing');assert.equal(stored.has('treg.arena.active.v1'),false);assert.equal(app.busy,false);
});
test('New query clears current results and inputs while retaining the session list',async()=>{
 const {app,price,stored}=setup();price();app.run={id:'saved',state:'completed'};app.history=[{id:'saved'}];app.customServices=true;app.services=['hunter'];
 app.api=()=>assert.fail('Starting a blank query must not call anything');await app.newSession();
 assert.equal(app.run,null);assert.equal(Object.keys(app.inputs).length,0);assert.equal(app.quote,null);assert.equal(app.customServices,false);assert.equal(app.history.length,1);assert.equal(stored.has('treg.arena.active.v1'),false);assert.equal(JSON.parse(stored.get('treg.arena.draft.v1')).pending,false);
});
test('Trying an uncalled vendor prices then starts it once in the same session',async()=>{
 const {app}=setup();app.run={id:'session',state:'completed'};app.runTeam=app.team;const calls=[];
 app.api=async(path,options)=>{calls.push(path);return path.endsWith('/plan')?{id:'price',estimate_micro:100,affordable:true,expires_at:new Date(Date.now()+60000).toISOString()}:{};};
 app.pollRun=async id=>assert.equal(id,'session');app.refreshHistory=async()=>{};
 const row={id:'next',can_try:true,estimate_micro:100};const first=app.tryVendor(row);await app.tryVendor(row);await first;
 assert.deepEqual(calls,['/arena/runs/session/attempts/next/plan','/arena/runs/session/attempts/next/start']);
 row.can_try=false;await app.tryVendor(row);assert.equal(calls.length,2);
});
test('A higher manual price is displayed for another click before spending',async()=>{
 const {app}=setup();app.run={id:'session',state:'completed'};app.runTeam=app.team;const calls=[];
 app.api=async path=>{calls.push(path);return {id:'price',estimate_micro:200,affordable:true,expires_at:new Date(Date.now()+60000).toISOString()};};
 await app.tryVendor({id:'next',can_try:true,estimate_micro:100});assert.equal(calls.length,1);assert.match(app.manualLabel({id:'next'}),/0.0002/);assert.match(app.error,/price changed/);
});
test('Manual attempts with insufficient credits go to billing without starting',async()=>{
 const {app,destinations}=setup();app.run={id:'session',state:'completed'};app.runTeam=app.team;const calls=[];
 app.api=async path=>{calls.push(path);return {id:'price',estimate_micro:100,affordable:false,expires_at:new Date(Date.now()+60000).toISOString()};};
 await app.tryVendor({id:'next',can_try:true,estimate_micro:100});assert.equal(calls.length,1);assert.deepEqual(destinations,['/app?from=enrich-arena#billing']);
});
test('Reporting incorrect data sends feedback without dispatching vendor calls',async()=>{
 const {app}=setup();app.run={id:'session',state:'completed'};app.runTeam=app.team;const calls=[];const row={id:'answer',rating:{value:'down'}};
 app.openReport(row);app.reportDrafts.answer.comment='Wrong company';
 app.api=async(path,options)=>{calls.push(path);assert.equal(JSON.parse(options.body).comment,'Wrong company');return {};};app.pollRun=async()=>{};
 await app.sendReport(row);assert.deepEqual(calls,['/arena/runs/session/attempts/answer/report']);assert.equal(app.reporting,'');
});
test('Clicking a result row toggles details while its action buttons remain independent',()=>{
 const {app}=setup();app.toggleResultRow({target:{closest:()=>null}},'answer');assert.equal(app.expandedResults.includes('answer'),true);
 app.toggleResultRow({target:{closest:()=>({tagName:'BUTTON'})}},'answer');assert.equal(app.expandedResults.includes('answer'),true);
 app.toggleResultRow({target:{closest:()=>null}},'answer');assert.equal(app.expandedResults.length,0);
});
test('Thumbs down saves immediately even when optional details are dismissed',async()=>{
 const {app}=setup();app.run={id:'session',state:'completed'};app.runTeam=app.team;
 const row={id:'answer'},calls=[];let release;
 app.api=(path,options)=>{calls.push(path);assert.equal(JSON.parse(options.body).value,'down');return new Promise(resolve=>release=resolve);};
 const save=app.rateResult(row,'down');assert.equal(app.ratingValue(row),'down');assert.equal(app.reporting,'answer');
 assert.equal(app.reportDrafts.answer.reason,'');app.reporting='';
 await app.rateResult(row,'up');assert.equal(calls.length,1);
 release({rating:{value:'down',created_at:'saved'}});await save;
 assert.equal(row.rating.created_at,'saved');assert.equal(app.reporting,'');assert.equal(row.report,undefined);
 assert.deepEqual(calls,['/arena/runs/session/attempts/answer/rating']);
 await app.rateResult(row,'down');assert.equal(calls.length,1);assert.equal(app.reporting,'answer');
});
test('Thumbs up saves without a form and can replace a downvote',async()=>{
 const {app}=setup();app.run={id:'session',state:'completed'};const row={id:'answer',rating:{value:'down'}};app.reporting='answer';
 app.api=async(path,options)=>{assert.ok(path.endsWith('/rating'));assert.equal(JSON.parse(options.body).value,'up');return {rating:{value:'up'}};};
 await app.rateResult(row,'up');assert.equal(row.rating.value,'up');assert.equal(app.reporting,'');assert.equal(app.reportDrafts.answer,undefined);
});
test('Failed rating saves restore the previous selection and show an error',async()=>{
 const {app}=setup();app.run={id:'session',state:'completed'};const previous={value:'up'},row={id:'answer',rating:previous};
 app.api=async()=>{throw new Error('Offline');};await app.rateResult(row,'down');
 assert.equal(row.rating,previous);assert.equal(app.reporting,'');assert.equal(app.busy,false);assert.match(app.error,/Could not save.*Offline/);
});
test('Avatar selection retains excluded vendors and prices only enabled services',async()=>{
 const {app,quote,stored}=setup();app.tasks[0].provider_previews=[[{provider:'hunter',estimate_micro:24500},{provider:'tomba',estimate_micro:8900}]];
 app.api=async(path,options)=>{assert.equal(path,'/arena/plans');const body=JSON.parse(options.body);assert.deepEqual(body.providers,['tomba']);assert.equal(body.max_cost_micro,10000000);return quote({providers:[{provider:'tomba',estimate_micro:0,tier:'credential'}]});};
 app.toggleProvider('hunter');assert.equal(app.customServices,true);assert.deepEqual(Array.from(app.enabledProviders),['tomba']);await app.prepare();
 assert.equal(app.availableProviders.length,2);assert.equal(app.previewProviders.length,1);assert.equal(app.previewProviders[0].estimate_micro,0);
 app.toggleProvider('hunter');assert.equal(app.readyQuote,null);assert.equal(app.availableProviders.length,2);assert.equal(app.enabledProviders.length,2);
 assert.equal(JSON.parse(stored.get('treg.arena.draft.v1')).budget,undefined);
});
test('Disabling every avatar blocks a run and changing input type resets the selection',async()=>{
 const {app}=setup();app.tasks[0].provider_previews=[[{provider:'hunter'}],[{provider:'tomba'}]];app.tasks[0].variants.push(['linkedin_url']);
 app.toggleProvider('hunter');assert.match(app.inputError(),/Enable at least one vendor/);app.api=()=>assert.fail('Empty selection cannot dispatch');await app.submit();
 app.chooseVariant(1);assert.equal(app.customServices,false);assert.deepEqual(Array.from(app.enabledProviders),['tomba']);
});
test('Avatar buttons are locked during calls and excluded is distinct from a failed result',()=>{
 const {app,components}=setup();app.tasks[0].provider_previews=[[{provider:'hunter'}]];app.run={state:'running'};
 app.toggleProvider('hunter');assert.equal(app.customServices,false);
 const events=[],fighter={choosable:true,active:false,disabled:false,enabledProviders:[],selected:[],$emit:(...args)=>events.push(args)};
 for(const [name,fn] of Object.entries(components.ArenaFighters.methods))fighter[name]=fn.bind(fighter);
 assert.equal(fighter.label({provider:'hunter'}),'Excluded');fighter.chooseProvider({provider:'hunter'});assert.deepEqual(events,[['toggle-provider','hunter']]);
 fighter.active=true;fighter.chooseProvider({provider:'hunter'});assert.equal(events.length,1);assert.equal(fighter.state({provider:'hunter',state:'running'}),'fighting');
});
test('Thumbs down defeats a successful fighter immediately, even a previous winner',async()=>{
 const {app,components}=setup();app.run={id:'session',state:'completed'};const row={id:'answer',state:'hit'};
 const fighter={active:false,selected:['answer']};for(const [name,fn] of Object.entries(components.ArenaFighters.methods))fighter[name]=fn.bind(fighter);
 let release;app.api=()=>new Promise(resolve=>release=resolve);
 const save=app.rateResult(row,'down');assert.equal(fighter.state(row),'defeated');assert.equal(fighter.label(row),'Thumbs down');
 release({rating:{value:'down'}});await save;assert.equal(fighter.state(row),'defeated');
 row.rating={value:'up'};assert.equal(fighter.state(row),'champion');
 row.report={reason:'wrong_person'};row.rating=null;assert.equal(fighter.state(row),'defeated');
});
test('Battle awards use actual successful results, share ties, and omit rejected or unknown results',()=>{
 const {app}=setup();app.run={mode:'compare',state:'completed',results:[
 {id:'a',state:'hit',duration_ms:100,charged_micro:20,estimate_micro:1},
 {id:'b',state:'hit',duration_ms:200,charged_micro:10,estimate_micro:999},
 {id:'c',state:'hit',duration_ms:100,charged_micro:10},
 {id:'d',state:'miss',duration_ms:0,charged_micro:0},
 {id:'e',state:'hit',duration_ms:1,charged_micro:0,rating:{value:'down'}}]};
 assert.deepEqual(JSON.parse(JSON.stringify(app.battleAwards)),{a:['Fastest'],b:['Cheapest'],c:['Fastest','Cheapest']});
 app.run.results[2].charged_micro=null;assert.equal(Object.values(app.battleAwards).flat().includes('Cheapest'),false);
 app.run.results[2].charged_micro=0;assert.deepEqual(Array.from(app.battleAwards.c),['Fastest','Cheapest']);
 app.run.state='running';assert.equal(Object.keys(app.battleAwards).length,0);
 app.run.state='completed';app.run.mode='waterfall';assert.equal(Object.keys(app.battleAwards).length,0);
});
test('One thumbs-up wins over faster and cheaper unrated results in either mode',()=>{
 const {app}=setup();app.run={mode:'compare',state:'completed',vote:{selected:['fast']},results:[
 {id:'fast',state:'hit',duration_ms:1,charged_micro:0},
 {id:'chosen',state:'hit',duration_ms:200,charged_micro:50,rating:{value:'up'}}]};
 assert.deepEqual(JSON.parse(JSON.stringify(app.battleAwards)),{fast:['Fastest','Cheapest']});assert.deepEqual(Array.from(app.winnerIds),['chosen']);
 assert.match(app.awardTitle('Fastest'),/successful, non-downvoted/);
 app.run.results[0].duration_ms=300;app.run.results[0].charged_micro=100;assert.deepEqual(Array.from(app.battleAwards.chosen),['Fastest','Cheapest']);assert.deepEqual(Array.from(app.winnerIds),['chosen']);
 app.run.mode='waterfall';assert.deepEqual(Array.from(app.winnerIds),['chosen']);
 app.run.results[1].rating.value='down';assert.equal(app.winnerIds.length,0);
});
test('Multiple thumbs-up compare metrics only inside that set, updating when a vote changes',()=>{
 const {app}=setup();app.run={mode:'compare',state:'completed',results:[
 {id:'unrated',state:'hit',duration_ms:0,charged_micro:0},
 {id:'quick',state:'hit',duration_ms:100,charged_micro:50,rating:{value:'up'}},
 {id:'cheap',state:'hit',duration_ms:200,charged_micro:10,rating:{value:'up'}},
 {id:'other',state:'hit',duration_ms:300,charged_micro:100,rating:{value:'up'}}]};
 assert.deepEqual(JSON.parse(JSON.stringify(app.battleAwards)),{quick:['Fastest'],cheap:['Cheapest']});
 assert.deepEqual(Array.from(app.winnerIds),['quick','cheap']);
 app.run.results[2].rating.value='down';assert.deepEqual(JSON.parse(JSON.stringify(app.battleAwards)),{quick:['Fastest','Cheapest']});
 app.run.results[3].rating.value='down';assert.deepEqual(JSON.parse(JSON.stringify(app.battleAwards)),{unrated:['Fastest','Cheapest']});
 app.run.state='running';assert.equal(app.winnerIds.length,0);
});

test('Identity conflicts are visible without electing a correct vendor or mixing batch entries',()=>{
 const {app}=setup();
 const a={id:'a',state:'hit',entry_index:0,output:{full_name:'Test Person',linkedin_url:'https://www.linkedin.com/in/test-person'}};
 const b={id:'b',state:'hit',entry_index:0,output:{full_name:'Different Person',linkedin_url:'https://www.linkedin.com/in/different-person'}};
 app.run={capability:'people.enrich',identities:[{email:'person@example.com'},{email:'second@example.com'}],results:[a,b]};
 assert.match(app.identityWarning(a),/different identities/);assert.match(app.identityWarning(b),/different identities/);
 b.entry_index=1;assert.equal(app.identityWarning(a),'');
 b.entry_index=0;b.state='miss';assert.equal(app.identityWarning(a),'');
 app.run.identities[0]={full_name:'Different Person',domain:'example.com'};
 assert.match(app.identityWarning(a),/name differs/);
 app.run.identities[0]={linkedin_url:'https://www.linkedin.com/in/someone-else'};
 assert.match(app.identityWarning(a),/profile differs/);
 app.run.identities[0]={full_name:'Tést Person'};assert.equal(app.identityWarning(a),'');
 assert.equal(a.output.full_name,'Test Person','Warnings preserve the provider answer');
});
test('Vendor quota errors recommend another vendor, not a team top-up',()=>{
 const {app}=setup();
 assert.match(app.emptyLabel({state:'error',upstream_status:402}),/vendor.*exhausted.*another vendor/);
 assert.doesNotMatch(app.emptyLabel({state:'error',upstream_status:null}),/exhausted/);
});

test('History loads older pages once, preserves existing rows and stops at the end',async()=>{
 const {app}=setup();const rows=Array.from({length:31},(_,i)=>({id:'run-'+i}));
 app.api=async(path,options)=>{assert.equal(path,'/arena/runs?limit=31');assert.equal(options.method,undefined);return rows;};
 await app.refreshHistory();assert.equal(app.history.length,30);assert.equal(app.historyHasMore,true);
 let release,calls=0;app.api=(path,options)=>{calls++;assert.equal(path,'/arena/runs?limit=31&before=run-29');assert.equal(options.method,undefined);return new Promise(resolve=>release=resolve);};
 const pending=app.loadMoreHistory();await app.loadMoreHistory();assert.equal(calls,1);assert.equal(app.historyLoading,true);
 release([rows[30],{id:'run-31'}]);await pending;
 assert.equal(app.history.length,32);assert.equal(app.history[0].id,'run-0');assert.equal(app.history.at(-1).id,'run-31');
 assert.equal(app.historyHasMore,false);assert.equal(app.historyLoading,false);await app.loadMoreHistory();assert.equal(calls,1);
});
test('Failed or stale older-history requests preserve the list and allow retry',async()=>{
 const {app}=setup();app.history=[{id:'current'}];app.historyHasMore=true;
 app.api=async()=>{throw new Error('Network error');};await app.loadMoreHistory();
 assert.equal(app.history.length,1);assert.equal(app.historyHasMore,true);assert.equal(app.historyLoading,false);assert.equal(app.error,'Network error');
 let release;app.api=()=>new Promise(resolve=>release=resolve);const pending=app.loadMoreHistory();
 app.team='another-team';app.history=[];release([{id:'private-old-team'}]);await pending;assert.equal(app.history.length,0);
});
test('Refreshing history invalidates a pending older page and exact page sizes need no extra click',async()=>{
 const {app}=setup();app.history=[{id:'old'}];app.historyHasMore=true;
 let release;app.api=()=>new Promise(resolve=>release=resolve);const pending=app.loadMoreHistory();
 app.api=async()=>Array.from({length:30},(_,i)=>({id:'fresh-'+i}));await app.refreshHistory();
 release([{id:'stale'}]);await pending;
 assert.equal(app.history.length,30);assert.equal(app.history[0].id,'fresh-0');assert.equal(app.historyHasMore,false);assert.equal(app.historyLoading,false);
});

test('Historical insights match task, input and endpoint without following paid vendor selection',()=>{
 const {app}=setup();
 const rows=[
  {task:app.taskId,input:'name_domain',endpoint:'hunter.people.email.find',provider:'hunter',hits:30,misses:70,unique_requests:80,unique_rate:37.5},
  {task:app.taskId,input:'linkedin_url',endpoint:'hunter.people.email.find',provider:'hunter',hits:99,misses:1},
  {task:'people.phone.find',input:'name_domain',endpoint:'hunter.people.email.find',provider:'hunter',hits:100,misses:0},
  {task:app.taskId,input:'name_domain',endpoint:'removed.endpoint',provider:'removed',hits:200,misses:0}
 ];
 app.insights={rows};app.tasks[0].provider_previews=[[{provider:'hunter',endpoint_id:'hunter.people.email.find'},{provider:'new-vendor',endpoint_id:'new.endpoint'}],[]];
 app.customServices=true;app.services=[];
 assert.equal(app.insightRows.length,2);assert.equal(app.insightRows[0].sample.hits,30);
 assert.equal(app.insightRate(app.insightRows[0]),'37.5%');
 assert.equal(app.insightRate(app.insightRows[1]),'—');
 app.tasks[0].variants.push(['linkedin_url']);app.chooseVariant(1);assert.equal(app.insightRows.length,0);
 app.tasks.push({id:'people.phone.find',variants:[['full_name','domain']],provider_previews:[[{provider:'hunter',endpoint_id:'hunter.people.email.find'}]]});
 app.chooseTask('people.phone.find');assert.equal(app.insightRows[0].sample.hits,100);
});
test('Small samples, zero coverage and verification verdicts have distinct presentations',()=>{
 const {app}=setup();const row={sample:{hits:0,misses:19,unique_requests:19,unique_rate:0}};
 assert.equal(app.insightRate(row),'—');row.sample.misses=20;
 assert.equal(app.insightRate(row),'—');row.sample.unique_requests=20;assert.equal(app.insightRate(row),'0.0%');
 app.taskId='people.email.verify';row.sample={hits:45,misses:0,unique_requests:40,unique_rate:100};
 assert.equal(app.insightRate(row),'100.0%');assert.equal(app.insightVerification,true);
});
test('Unavailable historical stats can be retried without blocking a query or spending',async()=>{
 const {app}=setup();app.api=async(path,options,team)=>{assert.equal(path,'/arena/insights');assert.equal(options.method,undefined);assert.equal(team,'');throw new Error('Offline');};
 await app.loadInsights();assert.equal(app.insightsState,'error');assert.equal(app.error,'');assert.equal(app.busy,false);
 const snapshot={version:2,status:'ready',updated_at:'2026-02-01T00:01:00Z',since:'2026-01-01T00:00:00Z',until:'2026-02-01T00:00:00Z',rows:[]};
 app.api=async()=>snapshot;await app.loadInsights();assert.equal(app.insightsState,'ready');assert.equal(app.insights,snapshot);
});
test('Historical stats load from the database API and retain last values on refresh failure',async()=>{
 const {app}=setup();let called;
 const data={version:2,status:'ready',since:'2026-01-01T00:00:00Z',until:'2026-02-01T00:00:00Z',updated_at:'2026-02-01T00:01:00Z',rows:[]};
 app.api=async path=>{called=path;return data;};await app.loadInsights();
 assert.equal(called,'/arena/insights');assert.equal(app.insightsState,'ready');assert.equal(app.insights,data);
 app.api=async()=>{throw Error('unavailable');};await app.loadInsights();
 assert.equal(app.insightsState,'error');assert.equal(app.insights,data);
 assert.equal(fs.existsSync(path.join(__dirname,'../../src/treg/web/enrich-arena/insights.json')),false);
});

test('Arena setup uses the shared dashboard agents, restores the choice and makes no vendor calls',async()=>{
 const {app,stored,runtime}=setup();let opened=0;
 app.$refs={setupDialog:{showModal(){opened++;},close(){}}};stored.set('treg-agent','codex');
 app.api=()=>assert.fail('Opening the picker must not request tokens or dispatch calls');
 await app.openSetup();assert.equal(opened,1);assert.equal(app.setupAgentId,'codex');assert.equal(app.setupOtherCount,7);
 assert.deepEqual(Array.from(app.setupFeatured,a=>a.id),['claude-code','codex','openclaw','hermes']);
 app.api=async(path,options,team)=>{assert.equal(path,'/auth/cli-token');assert.equal(options.method,undefined);assert.equal(team,'test-team');return {token:'test-token-for-selected-team'};};
 await app.prepareSetup();assert.equal(app.setupStep,2);assert.equal(app.setupToken,'test-token-for-selected-team');
 assert.equal(app.setupShowToken,false);assert.equal(stored.get('treg-agent'),'codex');
 const shared=runtime.TregAgentSetup;assert.equal(shared.command('https://treg.example/'),'set up treg — https://treg.example/llms.txt');
 const text=shared.setupText('command','test-team',app.setupToken,true);assert.ok(!text.includes(app.setupToken));assert.match(text,/••/);
 app.closeSetup();assert.equal(app.setupToken,null);assert.equal(app.setupShowToken,false);
});
test('Anonymous setup is usable without placeholders or requesting a private token',async()=>{
 const {app,runtime}=setup();app.user=null;app.team='';runtime.location.origin='https://arena.example';
 app.api=()=>assert.fail('Anonymous setup must not request a token');await app.prepareSetup();
 assert.equal(app.setupStep,2);assert.equal(app.setupToken,null);
 assert.equal(runtime.TregAgentSetup.setupText(app.setupCommand,'',null),'set up treg — https://arena.example/llms.txt');
});
test('Closing setup or changing teams discards a late token; failed loads can retry',async()=>{
 const {app}=setup();app.$refs={setupDialog:{close(){}}};let release;
 app.api=()=>new Promise(resolve=>release=resolve);const pending=app.prepareSetup();app.closeSetup();release({token:'late-token'});await pending;
 assert.equal(app.setupToken,null);assert.equal(app.setupStep,1);
 const another=app.prepareSetup();app.team='other-team';release({token:'old-team-token'});await another;assert.equal(app.setupToken,null);
 app.api=async()=>{throw new Error('Try again');};await app.prepareSetup();assert.equal(app.setupLoading,false);assert.equal(app.setupError,'Try again');
 app.api=async()=>({token:'fresh-token'});await app.prepareSetup();assert.equal(app.setupToken,'fresh-token');assert.equal(app.setupStep,2);
});
test('Setup copy writes the complete chosen text only on click and reports clipboard failures',async()=>{
 const {app,runtime}=setup();let copied;
 runtime.navigator={clipboard:{async writeText(text){copied=text;}}};await app.copySetup('setup text with test token');
 assert.equal(copied,'setup text with test token');assert.equal(app.setupCopied,true);
 runtime.navigator.clipboard.writeText=async()=>{throw new Error('denied');};await app.copySetup('test');assert.match(app.setupError,/Could not copy/);
});

test('The final setup step shares dashboard examples and copies prompts without running tools',async()=>{
 const {app,runtime,components}=setup();app.setupStep=2;app.setupShowToken=true;app.setupToken='test-token';
 const inputs=JSON.stringify(app.inputs);app.api=()=>assert.fail('The Try it out step must not call tools or start OAuth');
 app.showSetupExamples();assert.equal(app.setupStep,3);assert.equal(app.setupShowToken,false);
 assert.equal(components.TregTryItOut,runtime.TregAgentSetup.TryItOut);
 const examples=runtime.TregAgentSetup.examples;assert.equal(examples.length,4);
 let copied;runtime.navigator={clipboard:{async writeText(text){copied=text;}}};
 await app.copySetup(examples[0].prompt,examples[0].k);
 assert.equal(copied,examples[0].prompt);assert.equal(app.setupExampleCopied,examples[0].k);assert.equal(app.setupCopied,false);
 assert.equal(JSON.stringify(app.inputs),inputs);assert.ok(!copied.includes(app.setupToken));
});
test('Final setup links open the catalog or provider page and clear the setup key',()=>{
 const {app,destinations}=setup();let closed=0;app.$refs={setupDialog:{close(){closed++;}}};
 app.api=()=>assert.fail('Navigation must not authorize or connect an account');app.setupToken='test-token';
 app.openSetupCatalog();assert.equal(destinations[0],'/app#connections');assert.equal(app.setupToken,null);
 app.openSetupCatalog('google-ads');assert.equal(destinations[1],'/app/marketplace/google-ads');assert.equal(closed,2);
});

test('Vendor table attaches unique hit rate and successful response time without changing quoted prices or order',()=>{
 const {app,price}=setup();
 app.tasks[0].provider_previews=[[{provider:'hunter',endpoint_id:'hunter.people.email.find',estimate_micro:100}]];
 app.insightsState='ready';app.insights={rows:[{task:app.taskId,input:'name_domain',endpoint:'hunter.people.email.find',hits:30,misses:70,unique_requests:80,unique_rate:37.5,timed_hits:30,median_hit_ms:837.5}]};
 price({providers:[{provider:'hunter',endpoint_id:'hunter.people.email.find',estimate_micro:0,tier:'credential'}]});
 const row=app.previewProviders[0];assert.equal(row.estimate_micro,0);assert.equal(app.insightRate(row),'37.5%');
 assert.equal(app.insightResponseTime(row),'838 ms');assert.equal(app.insightSampleLabel(row),'');
 row.sample={...row.sample,median_hit_ms:null};assert.equal(app.insightResponseTime(row),'—');
 row.sample={...row.sample,median_hit_ms:50,hits:2,timed_hits:2};assert.equal(app.insightResponseTime(row),'—');
 app.customServices=true;app.services=[];assert.equal(app.previewProviders.length,0);
 app.customServices=false;price({providers:[{provider:'hunter',endpoint_id:'hunter.different.endpoint',estimate_micro:50}]});
 assert.equal(app.insightRate(app.previewProviders[0]),'—','Do not attach data from a different endpoint of the same vendor');
});

test('Charts preserve zero results, omit insufficient evidence and sort without changing dispatch order',()=>{
 const {app}=setup();
 app.tasks[0].provider_previews=[[{provider:'hunter',endpoint_id:'hunter.people.email.find',estimate_micro:10000},{provider:'tomba',endpoint_id:'tomba.people.email.find',estimate_micro:0},{provider:'findymail',endpoint_id:'findymail.people.email.find',estimate_micro:null}]];
 app.insights={rows:[{task:app.taskId,input:'name_domain',endpoint:'hunter.people.email.find',unique_requests:100,unique_rate:0,timed_hits:20,median_hit_ms:500},{task:app.taskId,input:'name_domain',endpoint:'tomba.people.email.find',unique_requests:100,unique_rate:60,timed_hits:19,median_hit_ms:50},{task:app.taskId,input:'name_domain',endpoint:'findymail.people.email.find',unique_requests:19,unique_rate:100,timed_hits:20,median_hit_ms:200}]};
 assert.deepEqual(Array.from(app.chartBars,p=>p.provider),['tomba','hunter']);
 assert.equal(app.chartBars[1].value,0);assert.equal(app.chartRows.length,3,'Table retains all vendors');
 assert.deepEqual(Array.from(app.previewProviders,p=>p.provider),['hunter','tomba','findymail']);
 assert.equal(app.chartPoints.length,2);assert.equal(app.chartPoints[1].estimate_micro,0);
 assert.ok(app.chartPriceMax>=10000);
 app.statsView='speed';assert.deepEqual(Array.from(app.chartBars,p=>p.provider),['findymail','hunter']);
 assert.equal(app.chartBars.length,2);assert.ok(app.chartMax>=500);
 app.customServices=true;app.services=['tomba'];assert.equal(app.chartPoints.length,1);assert.ok(app.chartPriceMax>0);
 assert.equal(app.chartMax,1,'No timed evidence retains a safe nonzero axis');
});

test('Charts follow task and input selection and use current batch prices without fabricating evidence',()=>{
 const {app}=setup();
 app.tasks[0].variants.push(['linkedin_url']);
 app.tasks[0].provider_previews=[[{provider:'hunter',endpoint_id:'hunter.people.email.find',estimate_micro:10000}],[{provider:'tomba',endpoint_id:'tomba.linkedin.email',estimate_micro:20000}]];
 app.insights={rows:[{task:app.taskId,input:'name_domain',endpoint:'hunter.people.email.find',unique_requests:100,unique_rate:50}]};
 app.chartFocus='hunter.people.email.find';assert.equal(app.chartDetail.provider,'hunter');
 app.addEntry();assert.equal(app.chartPoints[0].estimate_micro,20000);
 app.chooseVariant(1);assert.equal(app.chartPoints.length,0);assert.equal(app.chartDetail,null);assert.equal(app.chartBars.length,0);
 app.taskId='people.email.verify';assert.equal(app.chartViews[0].label,'Verdict rate');assert.equal(app.chartViews[3].label,'Price vs verdict rate');
 assert.equal(app.chartRows.length,0);
});

test('Discovery jobs have their own group and preserve labelled batch fields',()=>{
 const {app}=setup();
 app.tasks.push({id:'people.search',label:'Find people',discovery:true,max_entries:10,result_limit:10,variants:[['q'],['title','country']],fields:['people','count']},{id:'people.company.search',label:'People at a company',discovery:true,max_entries:10,variants:[['company_domain'],['title','company_domain']],fields:['people','count']},{id:'companies.similar',label:'Find similar companies',discovery:true,max_entries:10,variants:[['domain']],fields:['companies','count']},{id:'people.email.verify',variants:[['email']]});
 assert.deepEqual(Array.from(app.jobGroups,g=>g.label),['Discover','Enrich','Verify']);
 app.chooseTask(app.tasks[1].id);assert.equal(app.taskId,'people.search');assert.equal(app.discovery,true);assert.equal(app.maxEntries,10);
 app.variant=1;app.inputs={};
 assert.equal(paste(app,'Country code\tJob title\nUS\tEngineer\nGB\tRecruiter'),true);
 assert.equal(app.inputs.title,'Engineer');assert.equal(app.extraInputs[0].country,'GB');assert.equal(app.inputError(),'');
 app.extraInputs[0].country='United Kingdom';assert.match(app.inputError(),/Entry 2: Use a two-letter country code/);
});

test('Discovery batch limits reject overflow without losing existing queries',()=>{
 const {app}=setup();app.tasks=[{id:'people.search',discovery:true,max_entries:10,variants:[['q']]}];app.taskId='people.search';app.inputs={q:'Keep this query'};
 const before=JSON.stringify(app.inputRows);
 paste(app,Array.from({length:11},(_,i)=>'Engineer role '+i).join('\n'));
 assert.match(app.error,/at most 10 entries/);assert.equal(JSON.stringify(app.inputRows),before);
 for(let i=0;i<12;i++)app.addEntry();assert.equal(app.inputRows.length,10);
 app.extraInputs.push({q:'Restored overflow'});assert.match(app.inputError(),/at most 10 entries/);
});

test('Discovery compares actual match lists and keeps batch metrics at query level',()=>{
 const {app}=setup();app.run={capability:'people.search',mode:'compare',fields:['people','count'],state:'completed',identities:[{q:'First'},{q:'Second'}],results:[
  {id:'a',provider:'aviato',entry_index:0,state:'hit',output:{count:1,people:[{name:'First Person'}]},charged_micro:10,duration_ms:100},
  {id:'b',provider:'exa',entry_index:0,state:'hit',output:{count:1,people:[{name:'Different Person'}]},charged_micro:10,duration_ms:100},
  {id:'c',provider:'aviato',entry_index:1,state:'hit',output:{count:2,people:[{name:'Person Three'},{name:'Person Four'}]},charged_micro:10,duration_ms:100}
 ]};
 app.resultFilter='disagreement';assert.equal(app.matrixEntries.length,1);
 assert.equal(app.batchVendors.find(v=>v.provider==='aviato').batchLabel,'2/2 queries matched');
 assert.equal(app.displayValue(app.searchMatches(app.run.results[2])),'2 matches');
 app.run.results[1].output.people=[{name:'First Person'}];assert.equal(app.matrixEntries.length,0);
 app.run.results[1].output.people=[{name:'Person Four'},{name:'Person Three'}];app.run.results[0].output.people=[{name:'Person Three'},{name:'Person Four'}];assert.equal(app.matrixEntries.length,0,'Result ordering alone is not a different answer');
});


test('Use-case tabs keep action labels, skip reselection, and support keyboard switching',()=>{
 const {components}=setup(),definition=components.ArenaTaskTabs,events=[];
 const tabs={value:'people.search',disabled:false,$emit:(...args)=>events.push(args)};
 for(const [name,method] of Object.entries(definition.methods))tabs[name]=method.bind(tabs);
 assert.equal(tabs.label({id:'people.email.find'}),'Find work email');
 assert.equal(tabs.label({id:'people.company.search'}),'Find people at company');
 tabs.select('people.search');assert.equal(events.length,0);
 let focused=-1,prevented=0;
 const buttons=['people.search','companies.similar','people.email.find'].map((id,i)=>({dataset:{task:id},focus(){focused=i;}}));
 const press=(key,index)=>tabs.keydown({key,currentTarget:{querySelectorAll:()=>buttons},target:{closest:()=>buttons[index]},preventDefault(){prevented++;}});
 press('ArrowRight',0);assert.equal(focused,1);assert.deepEqual(events.pop(),['change','companies.similar']);
 press('End',0);assert.equal(focused,2);press('ArrowLeft',0);assert.equal(focused,2);
 tabs.disabled=true;press('Home',2);assert.equal(focused,2);assert.equal(prevented,3);
});

test('Horizontal use-case tabs reveal the selected option without moving visible selections',async()=>{
 const {components}=setup(),definition=components.ArenaTaskTabs;
 let bounds={left:480,right:650};
 const host={clientWidth:300,scrollWidth:1200,scrollLeft:0,getBoundingClientRect:()=>({left:0,right:300}),querySelector:()=>({getBoundingClientRect:()=>bounds})};
 const tabs={$refs:{scroller:host},$nextTick:async()=>{}};
 tabs.updateOverflow=definition.methods.updateOverflow.bind(tabs);
 const reveal=definition.methods.reveal.bind(tabs);
 await reveal();assert.equal(host.scrollLeft,386);
 bounds={left:50,right:200};await reveal();assert.equal(host.scrollLeft,386);
 bounds={left:-60,right:100};await reveal();assert.equal(host.scrollLeft,290);
});


test('Leaderboard uses all public vendors and per-entry prices, independent of Arena quotes and selections',()=>{
 const {app,price}=setup({pathname:'/enrich-arena/leaderboard'});assert.equal(app.leaderboard,true);
 app.tasks[0].provider_previews=[[{provider:'hunter',endpoint_id:'hunter.people.email.find',estimate_micro:10000},{provider:'tomba',endpoint_id:'tomba.people.email.find',estimate_micro:20000}]];
 app.insights={rows:[{task:app.taskId,input:'name_domain',endpoint:'hunter.people.email.find',unique_requests:100,unique_rate:50}]};
 app.extraInputs=[{full_name:'Second Person',domain:'second.example'}];app.customServices=true;app.services=['tomba'];
 price({providers:[{provider:'tomba',endpoint_id:'tomba.people.email.find',estimate_micro:0}]});
 assert.equal(app.previewProviders.length,2);assert.equal(app.chartPoints.length,1);assert.equal(app.chartPoints[0].estimate_micro,10000);
 assert.deepEqual(Array.from(app.chartViews,v=>v.id),['rate','verified','speed','price','value']);
});
test('Leaderboard preserves Arena draft and skips pricing, pending dispatch, and run restoration',async()=>{
 const {app,stored,runtime,mounted}=setup({pathname:'/enrich-arena/leaderboard',search:'?capability=people.email.find&variant=1'});
 app.leaderboard=false;app.saveDraft(true);const before=JSON.stringify([...stored]);app.leaderboard=true;
 app.saveDraft(false);assert.equal(JSON.stringify([...stored]),before);
 let planned=0;app.api=async path=>{if(path==='/arena/tasks')return [{...app.tasks[0],variants:[['full_name','domain'],['linkedin_url']]},{id:'people.search',discovery:true}];if(path==='/meta')return {};planned++;throw Error('Unexpected request: '+path);};
 app.loadInsights=()=>{};app.loadIdentity=async()=>{};app.restoreDraft=()=>{throw Error('Restored Arena draft');};app.loadHistory=()=>{throw Error('Restored active run');};runtime.setInterval=()=>1;
 await app.prepare();app.scheduleQuote();await mounted.call(app);
 assert.equal(app.error,'');assert.equal(planned,0);assert.equal(app.tasks.length,1);assert.equal(app.variant,1);
 assert.equal(runtime.document.title,'Enrichment Leaderboard — treg');assert.equal(JSON.stringify([...stored]),before);
 assert.equal(app.sectionUrl(false),'/enrich-arena?capability=people.email.find&variant=1');
 assert.equal(app.sectionUrl(true),'/enrich-arena/leaderboard?capability=people.email.find&variant=1');
});

test('Use-case overflow arrows follow scroll boundaries and disappear when all tabs fit',()=>{
 const {components}=setup(),definition=components.ArenaTaskTabs;
 let position=0;const host={clientWidth:300,scrollWidth:900,get scrollLeft(){return position;},set scrollLeft(v){position=Math.max(0,Math.min(v,this.scrollWidth-this.clientWidth));}};
 const tabs={$refs:{scroller:host}};for(const [key,fn]of Object.entries(definition.methods))tabs[key]=fn.bind(tabs);
 tabs.updateOverflow();assert.equal(tabs.overflowLeft,false);assert.equal(tabs.overflowRight,true);
 tabs.scrollTabs(1);assert.equal(position,195);assert.equal(tabs.overflowLeft,true);assert.equal(tabs.overflowRight,true);
 host.scrollLeft=600;tabs.updateOverflow();assert.equal(tabs.overflowRight,false);
 tabs.scrollTabs(-1);assert.equal(position,405);assert.equal(tabs.overflowRight,true);
 host.scrollWidth=300;host.scrollLeft=0;tabs.updateOverflow();assert.equal(tabs.overflowLeft,false);assert.equal(tabs.overflowRight,false);
});

test('Price charts include priced vendors without historical samples, keep free prices and omit invalid prices',()=>{
 const {app}=setup({pathname:'/enrich-arena/leaderboard'});
 app.tasks[0].provider_previews=[[{provider:'hunter',endpoint_id:'hunter.email',estimate_micro:10000},{provider:'tomba',endpoint_id:'tomba.email',estimate_micro:0},{provider:'findymail',endpoint_id:'findymail.email',estimate_micro:null},{provider:'aviato',endpoint_id:'aviato.email',estimate_micro:-1}]];
 app.statsView='price';assert.deepEqual(Array.from(app.chartBars,p=>p.provider),['tomba','hunter']);
 assert.equal(app.chartBars[0].value,0);assert.ok(app.chartMax>=10000);assert.equal(app.chartTick(10000),'$0.01');
 app.chartFocus='hunter.email';assert.equal(app.chartDetail.provider,'hunter');
 app.tasks[0].provider_previews[0]=[{provider:'tomba',endpoint_id:'tomba.email',estimate_micro:0}];assert.ok(app.chartMax>0);
 app.statsView='rate';assert.equal(app.chartBars.length,0);
});

test('Try actions are independent per provider during a running waterfall',async()=>{
 const {app}=setup();app.run={id:'session',state:'running'};app.runTeam=app.team;let release;
 const waiting=new Promise(resolve=>{release=resolve;}),calls=[];
 app.api=async path=>{calls.push(path);if(path.includes('/first/plan'))await waiting;return {id:'q',estimate_micro:100,affordable:true,expires_at:new Date(Date.now()+60000).toISOString()};};
 app.pollRun=async()=>{};app.refreshHistory=async()=>{};
 const a={id:'first',can_try:true,estimate_micro:100},b={id:'second',can_try:true,estimate_micro:100};
 const first=app.tryVendor(a);assert.equal(app.manualPending.first,true);await app.tryVendor(a);await app.tryVendor(b);
 assert.ok(calls.includes('/arena/runs/session/attempts/second/start'));assert.equal(app.manualPending.first,true);
 release();await first;assert.equal(calls.filter(p=>p.includes('/first/plan')).length,1);assert.equal(Object.keys(app.manualPending).length,0);
 assert.equal(a.can_try,false);assert.equal(b.can_try,false);
});
test('Known insufficient balances offer top up without dispatch, while zero-cost calls remain available',async()=>{
 const {app,destinations}=setup();app.run={id:'session',state:'running'};app.runTeam=app.team;app.balance=-1;
 const paid={id:'paid',can_try:true,estimate_micro:100};assert.match(app.manualLabel(paid),/^Top up/);
 app.api=()=>assert.fail('Known insufficient funds must not dispatch');await app.tryVendor(paid);assert.equal(destinations.length,1);
 assert.equal(app.manualUnaffordable({estimate_micro:0}),false);app.balance=100;assert.equal(app.manualUnaffordable(paid),false);
 app.balance=0;assert.equal(app.manualUnaffordable(paid),true);
});

test('Auto verification invalidates the lookup quote and survives a saved draft',()=>{
 const {app}=setup();app.autoVerify=false;const key=app.quoteKey;app.autoVerify=true;
 assert.notEqual(app.quoteKey,key);app.saveDraft(false);app.autoVerify=false;app.restoreDraft();assert.equal(app.autoVerify,true);
});
test('Verification verdicts preserve catch-all and distinguish invalid phone format',()=>{
 const {app}=setup();
 assert.equal(app.verificationVerdict({verification:{state:'hit',output:{valid:false,status:'catch_all'}}}),'Risky');
 assert.equal(app.verificationVerdict({verification:{capability:'people.phone.verify',state:'hit',output:{valid:false}}}),'Invalid phone number');
 assert.equal(app.verificationVerdict({verification:{state:'error'}}),'Verification unavailable');
 assert.equal(app.verificationVerdict({verification:{state:'error',not_started:true}}),'Verification not run');
});
test('Verification is priced, claims once per row, and can run alongside other requests',async()=>{
 const {app}=setup();app.tasks.push({id:'people.email.verify',provider_previews:[[{estimate_micro:6250}]]});
 app.run={id:'r',capability:'people.email.find',state:'running'};app.runTeam=app.team;
 const r={id:'a',can_verify:true},calls=[];let release;
 app.api=async(path)=>{calls.push(path);if(path.endsWith('/plan'))return await new Promise(resolve=>{release=()=>resolve({id:'q',estimate_micro:6250,affordable:true,balance_micro:100000,expires_at:new Date(Date.now()+60000).toISOString()});});return {};};
 app.pollRun=async()=>{};
 const pending=app.verifyResult(r);await app.verifyResult(r);assert.equal(calls.length,1);release();await pending;
 assert.equal(calls.length,2);assert.match(calls[1],/verification\/start$/);assert.equal(r.can_verify,false);
});
test('Verification does not dispatch when credit is insufficient or a price increases',async()=>{
 for(const expensive of [false,true]){
  const {app}=setup();app.tasks.push({id:'people.email.verify',provider_previews:[[{estimate_micro:6250}]]});app.run={id:'r',capability:'people.email.find'};app.runTeam=app.team;
  const calls=[];let topped=false;app.topUp=()=>{topped=true;};app.api=async(path)=>{calls.push(path);return {id:'q',estimate_micro:expensive?7000:6250,affordable:expensive,balance_micro:expensive?100000:0,expires_at:new Date(Date.now()+60000).toISOString()};};
  await app.verifyResult({id:'a',can_verify:true});assert.equal(calls.length,1);assert.equal(topped,!expensive);
 }
});
test('Additional lookup pricing includes its automatic verification cap',()=>{
 const {app}=setup();app.run={auto_verify:{estimate_micro:6250}};app.balance=10000;
 const r={id:'a',estimate_micro:8900};assert.equal(app.manualPrice(r),15150);assert.equal(app.manualUnaffordable(r),true);
 app.manualQuotes.a={estimate_micro:8900,required_micro:15150,affordable:true};assert.equal(app.manualPrice(r),15150);
});

test('MillionVerifier success uses a readable verification verdict',()=>{
 const {app}=setup();assert.equal(app.verificationVerdict({verification:{state:'hit',output:{valid:true,status:'ok'}}}),'Valid');
 assert.equal(app.providerName('millionverifier'),'MillionVerifier');assert.equal(app.providerName('contactout'),'ContactOut');assert.equal(app.providerName('trykitt'),'Kitt');
});

test('Email verdict badges distinguish uncertain statuses from invalid boolean projections',()=>{
 const {app}=setup();app.run={capability:'people.email.verify',fields:['valid','status']};
 for(const [status,valid,label,tone] of [['valid',true,'Valid','valid'],['ok',true,'Valid','valid'],['invalid',false,'Invalid','invalid'],['accept_all',false,'Risky','risky'],['catch_all',false,'Risky','risky'],['catchall',false,'Risky','risky'],['risky',false,'Risky','risky'],['unknown',false,'Unknown','unknown'],['unverified',false,'Unknown','unknown'],['new_status',false,'Unknown','unknown']]){
  const r={state:'hit',output:{status,valid}};
  assert.equal(app.outcomeLabel(r),'Verdict: '+label);assert.equal(app.outcomeClass(r),'verdict-'+tone);
  assert.equal(app.verificationVerdict({verification:r}),label);assert.equal(app.verificationClass({verification:r}),'verdict-'+tone);
  assert.deepEqual(Array.from(app.summaryFields(r)),['status']);
 }
 assert.equal(app.outcomeLabel({state:'error',output:{status:'invalid'}}),'Error');
 assert.equal(app.outcomeClass({state:'error',output:{status:'invalid'}}),'error');
 app.run.capability='people.email.find';assert.equal(app.outcomeLabel({state:'hit'}),'Found');assert.equal(app.outcomeClass({state:'hit'}),'hit');
});

test('New email and phone lookups enable verification by default and preserve draft opt-outs',()=>{
 const {app}=setup();app.tasks.push({id:'people.phone.find',variants:[['linkedin_url']]},{id:'people.search',variants:[['q']]});assert.equal(app.autoVerify,true);assert.equal(JSON.parse(app.quoteKey).auto_verify,true);
 app.autoVerify=false;app.saveDraft(false);app.autoVerify=true;app.restoreDraft();assert.equal(app.autoVerify,false);
 app.chooseTask('people.phone.find');assert.equal(app.autoVerify,true);assert.equal(JSON.parse(app.quoteKey).auto_verify,true);
 app.autoVerify=false;app.saveDraft(false);app.autoVerify=true;app.restoreDraft();assert.equal(app.autoVerify,false);
 app.chooseTask('people.email.find');assert.equal(app.autoVerify,true);
 app.chooseTask('people.search');assert.equal(JSON.parse(app.quoteKey).auto_verify,false);
});

test('Benchmark page loads only public benchmark and account metadata, without restoring or spending',async()=>{
 const {app,mounted,stored}=setup({pathname:'/enrich-arena/people-search-bench'});
 const calls=[];app.loadBenchmark=async()=>calls.push('benchmark');app.api=async p=>{calls.push(p);return {};};app.loadIdentity=async()=>calls.push('identity');
 app.loadInsights=()=>assert.fail('Benchmark should not load observed call stats');app.restoreDraft=()=>assert.fail('Must not restore pending run');app.scheduleQuote=()=>assert.fail('Must not price a run');
 await mounted.call(app);assert.deepEqual(calls,['benchmark','/meta','identity']);assert.equal(app.booted,true);
 app.saveDraft();assert.equal(stored.size,0);
 await app.prepare();assert.deepEqual(calls,['benchmark','/meta','identity']);
 app.benchCategories=[{id:'one',rows:[{score:0},{score:50}]},{id:'two',rows:[{score:90}]}];app.benchCategory='two';assert.equal(app.benchRows[0].score,90);app.benchCategory='one';assert.deepEqual(Array.from(app.benchRows,r=>r.score),[50,0]);assert.equal(app.benchCategories[0].rows[0].score,0);
});
test('Benchmark loading errors clear stale charts and support a retry',async()=>{
 const {app,runtime}=setup();app.benchCategories=[{id:'old'}];runtime.fetch=async()=>({ok:false});await app.loadBenchmark();assert.equal(app.benchCategories.length,0);assert.ok(app.benchError);assert.equal(app.benchLoading,false);
 runtime.fetch=async(url,options)=>{assert.equal(url,'/people-search');assert.equal(options.credentials,'omit');return {ok:true,text:async()=>'<html>source</html>'};};
 runtime.DOMParser=class{parseFromString(text,type){assert.equal(type,'text/html');return text;}};
 runtime.window.ArenaBench={parseDocument:()=>[{id:'b2b-prospecting',rows:[]}]};await app.loadBenchmark();assert.equal(app.benchError,'');assert.equal(app.benchCategory,'b2b-prospecting');
});

test('Email validity matches exact endpoint and input and is independent of lookup coverage',()=>{
 const {app}=setup();
 app.tasks[0].provider_previews=[[{provider:'hunter',endpoint_id:'hunter.people.email.find'},{provider:'tomba',endpoint_id:'tomba.people.email.find'}]];
 const audit={task:'people.email.find',input:'name_domain',endpoint:'hunter.people.email.find',method:'email_verifier_consensus',sample_n:40,baseline_n:200,baseline_rate:40,rate:30,checked_n:40,validity_rate:75,estimate:true,small_sample:true,verifiers:['leadmagic','millionverifier']};
 app.insights={rows:[],verification:{rows:[audit],baseline_since:'2026-01-01T00:00:00Z',baseline_until:'2026-01-03T00:00:00Z',checked_at:'2026-01-05T00:00:00Z'}};
 app.statsView='verified';assert.equal(app.chartBars.length,1);assert.equal(app.chartBars[0].value,75);assert.equal(app.verifiedRate(app.chartBars[0]),'75.0%');assert.equal(app.chartMax,100);
 assert.match(app.verifiedRateNote(app.chartBars[0]),/sampled returned emails/);
 audit.baseline_n=0;audit.baseline_rate=null;assert.equal(app.chartBars[0].value,75);
 assert.equal(app.chartRows[0].rate,null,'A frozen audit does not overwrite current returned rate');
 audit.validity_rate=0;assert.equal(app.chartBars[0].value,0,'Genuine zero remains visible');
 audit.input='linkedin_url';assert.equal(app.chartBars.length,0);
 audit.input='name_domain';audit.checked_n=19;assert.equal(app.chartBars.length,0);
 audit.checked_n=40;audit.validity_rate=NaN;assert.equal(app.chartBars.length,0);
});

test('Phone format validity uses its own metric and never implies verified hit rate',()=>{
 const {app}=setup();app.taskId='people.phone.find';
 const row={audit:{method:'phone_format',sample_n:100,checked_n:100,validity_rate:100,baseline_n:200,rate:100,verifiers:['tomba']}};
 assert.equal(app.verifiedRate(row),'—','Legacy email and projected rates must not leak into phone metrics');
 row.audit.format_validity_rate=82.5;assert.equal(app.verifiedRate(row),'82.5%');
 assert.equal(app.verifiedRateLabel,'Phone format validity');assert.match(app.verifiedExplanation,/does not confirm a live line/);
 assert.match(app.verifiedRateNote(row),/ownership are not verified/);assert.ok(app.chartViews.some(v=>v.id==='verified'));
 row.audit.format_validity_rate=0;assert.equal(app.verifiedRate(row),'0.0%');
 row.audit.checked_n=19;assert.equal(app.verifiedRate(row),'—');
 delete row.audit.checked_n;assert.equal(app.verifiedRate(row),'—');
 row.audit.checked_n=100;row.audit.format_validity_rate=101;assert.equal(app.verifiedRate(row),'—');
 row.audit.format_validity_rate=82.5;row.audit.method='email_verifier_consensus';assert.equal(app.verifiedRate(row),'—');
 app.taskId='people.email.find';assert.equal(app.verifiedRate(row),'100.0%','Email still uses email validity only');
 app.taskId='people.email.verify';assert.ok(!app.chartViews.some(v=>v.id==='verified'));
});

test('Switching to a task without verified hit rate resets the selected chart',()=>{
 const {app}=setup();app.tasks.push({id:'people.phone.find',variants:[['linkedin_url']]},{id:'people.enrich',variants:[['linkedin_url']]});app.statsView='verified';app.chooseTask('people.phone.find');assert.equal(app.statsView,'verified');
 app.chooseTask('people.enrich');assert.equal(app.statsView,'rate');
});

test('Vendor request opens without login or credits and submits the existing request payload',async()=>{
 const {app}=setup();let opened=0;app.user=null;app.balance=0;
 app.$refs={vendorRequestDialog:{showModal(){opened++;},close(){}}};
 app.prepare=()=>assert.fail('Requesting a vendor must not start a paid run');
 app.openVendorRequest();assert.equal(opened,1);
 let sent;app.api=async(path,options,team)=>{sent={path,body:JSON.parse(options.body),team};};
 app.requestForm={capability:'  Example vendor  ',note:'Docs: https://example.test',contact:'requester@example.test'};
 await app.submitVendorRequest();
 assert.equal(sent.path,'/tool-requests');assert.equal(sent.team,null);
 assert.deepEqual(sent.body,{capability:'Example vendor',query:'Enrich Arena: people.email.find',note:'Docs: https://example.test',contact:'requester@example.test',source:'web'});
 assert.equal(app.requestDone,true);assert.equal(app.requestBusy,false);assert.equal(app.requestForm.capability,'');
 assert.ok(!JSON.stringify(sent).includes('Test Person'),'Do not attach enrichment inputs');
});
test('Vendor requests validate, prevent duplicate submits and preserve fields for retry',async()=>{
 const {app}=setup();let calls=0,finish;
 app.api=()=>{calls++;return new Promise((resolve,reject)=>{finish=reject;});};
 await app.submitVendorRequest();assert.equal(calls,0);assert.match(app.requestError,/Say what/);
 app.requestForm.capability='Example vendor';const pending=app.submitVendorRequest();
 await app.submitVendorRequest();assert.equal(calls,1);
 finish(Object.assign(new Error('limit'),{status:429}));await pending;
 assert.equal(app.requestBusy,false);assert.equal(app.requestDone,false);assert.equal(app.requestForm.capability,'Example vendor');assert.match(app.requestError,/Too many requests/);
 app.api=async()=>{};await app.submitVendorRequest();assert.equal(app.requestDone,true);
});

function captureUrls(runtime){
 const urls=[];runtime.location.pathname||='/enrich-arena';runtime.location.search||='';
 runtime.window.history=Object.fromEntries(['pushState','replaceState'].map(method=>[method,(_s,_t,url)=>{urls.push({method,url});const [path,query]=url.split('?');runtime.location.pathname=path;runtime.location.search=query?'?'+query:'';}]));return urls;
}
test('Changing tasks and input types updates URL and clears stale history restoration',()=>{
 const {app,runtime,stored}=setup({pathname:'/enrich-arena',search:'?run=old&team=test-team'}),urls=captureUrls(runtime);
 app.tasks.push({id:'people.phone.find',variants:[['linkedin_url'],['email']]});app.run={id:'old',state:'completed'};stored.set('treg.arena.active.v1',JSON.stringify({id:'old'}));
 app.chooseTask('people.phone.find');assert.equal(urls.at(-1).url,'/enrich-arena?capability=people.phone.find&variant=0');assert.equal(app.run,null);assert.ok(!stored.has('treg.arena.active.v1'));
 app.chooseVariant(1);assert.equal(urls.at(-1).url,'/enrich-arena?capability=people.phone.find&variant=1');assert.equal(app.sectionUrl(true),'/enrich-arena/leaderboard?capability=people.phone.find&variant=1');
});
test('Selecting history writes a bookmarkable run URL and new query removes it',async()=>{
 const {app,runtime}=setup(),urls=captureUrls(runtime);
 app.api=async()=>({id:'saved',state:'completed',capability:app.taskId,mode:'compare',identity:app.inputs,results:[]});
 await app.loadHistory('saved');assert.equal(urls.at(-1).url,'/enrich-arena?run=saved&team=test-team');
 await app.newQuery();assert.equal(urls.at(-1).url,'/enrich-arena?capability=people.email.find&variant=0');
});
test('Explicit task URL wins over an automatically restored history run',async()=>{
 const {app,runtime,stored,mounted}=setup({pathname:'/enrich-arena',search:'?capability=people.phone.find&variant=1'});captureUrls(runtime);runtime.setInterval=()=>1;
 const tasks=[...app.tasks,{id:'people.phone.find',variants:[['linkedin_url'],['email']]}];
 stored.set('treg.arena.active.v1',JSON.stringify({id:'old',team:app.team,at:Date.now()}));
 app.loadInsights=()=>{};app.loadIdentity=async()=>{};app.api=async path=>path==='/arena/tasks'?tasks:{};app.loadHistory=()=>assert.fail('Explicit selection must not restore old history');
 await mounted.call(app);assert.equal(app.taskId,'people.phone.find');assert.equal(app.variant,1);assert.equal(app.run,null);
});
test('A direct run URL loads only that saved result in its authorized team',async()=>{
 const {app,runtime,mounted}=setup({pathname:'/enrich-arena',search:'?run=saved&team=other-team'});captureUrls(runtime);runtime.setInterval=()=>1;
 app.teams=[{slug:'test-team'},{slug:'other-team'}];let read=0;
 app.loadInsights=()=>{};app.loadIdentity=async()=>{};app.loadBalance=async()=>{};app.refreshHistory=async()=>{};app.prepare=()=>assert.fail('No quote or dispatch from saved URL');
 app.api=async(path,options,team)=>{if(path==='/arena/tasks')return app.tasks;if(path==='/meta')return {};assert.equal(path,'/arena/runs/saved');assert.equal(team,'other-team');assert.equal(options.method,undefined);read++;return {id:'saved',state:'completed',capability:app.taskId,mode:'compare',identity:app.inputs,results:[]};};
 await mounted.call(app);assert.equal(read,1);assert.equal(app.run.id,'saved');assert.equal(app.team,'other-team');
});
test('Run links require login and reject inaccessible teams without fetching results',async()=>{
 const {app}=setup({pathname:'/enrich-arena',search:'?run=saved&team=private-team'});app.user=null;let login=0;app.openLogin=async pending=>{assert.equal(pending,false);login++;};app.api=()=>assert.fail('No private result fetched');
 assert.equal(await app.restoreLinkedRun(),true);assert.equal(login,1);assert.equal(app.linkedRunPending,true);
 app.user={id:1};app.teams=[{slug:'test-team'}];await app.restoreLinkedRun();assert.match(app.error,/not available/);
});
test('Bare Arena does not reopen a cached result or rewrite itself to that result',async()=>{
 const {app,runtime,stored,mounted}=setup({pathname:'/enrich-arena',search:''}),urls=captureUrls(runtime);runtime.setInterval=()=>1;
 stored.set('treg.arena.active.v1',JSON.stringify({id:'old',team:app.team,at:Date.now()}));
 app.loadInsights=()=>{};app.loadIdentity=async()=>{};app.api=async path=>path==='/arena/tasks'?app.tasks:{};app.loadHistory=()=>assert.fail('Bare Arena is not a saved-result URL');
 await mounted.call(app);assert.equal(app.run,null);assert.equal(urls.length,0);
});
test('Vendor self-serve copy uses the published listing prompt and reports clipboard failure',async()=>{
 const {app,runtime}=setup();let copied='';runtime.navigator={clipboard:{writeText:async text=>{copied=text;}}};
 await app.copyVendorListingPrompt();assert.equal(copied,'Read https://treg.to/vendor-listing.md and add our API to the treg catalog, then open a PR.');assert.equal(app.vendorPromptCopied,true);
 runtime.navigator.clipboard.writeText=async()=>{throw Error('Denied');};await app.copyVendorListingPrompt();assert.equal(app.vendorPromptCopied,false);assert.match(app.vendorPromptError,/Select and copy/);
});

test('Email signup opens agent setup and preserves the draft without running',async()=>{
 const {app,stored}=setup({search:''});const calls=[];
 app.pendingSubmit=true;app.$refs={loginDialog:{close:()=>calls.push('close')}};
 app.api=async url=>{calls.push(url);};app.loadIdentity=async()=>calls.push('identity');
 app.restoreLinkedRun=async()=>{calls.push('restore');return true;};
 app.openSetup=async()=>calls.push('setup');app.prepare=async()=>assert.fail('Must not resume spending');
 await app.verifyEmail();
 assert.deepEqual(calls,['/auth/email/verify','close','identity','restore','setup']);
 assert.equal(app.pendingSubmit,false);
 assert.equal(JSON.parse(stored.get('treg.arena.draft.v1')).inputs.domain,'example.com');
});
test('Failed email signup does not open setup',async()=>{
 const {app}=setup();app.api=async()=>{throw new Error('Invalid code');};app.openSetup=async()=>assert.fail('Unauthenticated setup');
 await app.verifyEmail();assert.equal(app.authError,'Invalid code');
});
test('OAuth signup returns to the selected page and opens setup once',async()=>{
 for(const provider of ['google','github']){
  const {app,stored,destinations}=setup({pathname:'/enrich-arena',search:'?run=saved&team=test-team'});
  app.socialLogin(provider);
  assert.equal(destinations[0],'/auth/'+provider+'?return_to='+encodeURIComponent('/enrich-arena?run=saved&team=test-team'));
  let opened=0;app.openSetup=async()=>opened++;
  app.user=null;assert.equal(await app.resumeSignupSetup(),false);assert.equal(opened,0);
  app.user={id:1};assert.equal(await app.resumeSignupSetup(),true);assert.equal(opened,1);
  assert.equal(await app.resumeSignupSetup(),false);assert.equal(opened,1);
  stored.set('treg.arena.signup-setup.v1',JSON.stringify({at:Date.now()-600001}));
  assert.equal(await app.resumeSignupSetup(),false);assert.equal(opened,1);
 }
});

test('New signup creates a named team before showing its setup token',async()=>{
 const {app,stored}=setup();app.team='';app.teams=[];
 app.$refs={setupDialog:{showModal(){},close(){}}};await app.openSetup();assert.equal(app.setupStep,0);
 const calls=[];app.api=async(url,options,team)=>{
  calls.push(url);
  if(url==='/orgs'){assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),{name:'My team'});return {org:'my-team'};}
  assert.equal(url,'/auth/cli-token');assert.equal(team,'my-team');return {token:'fake-setup-secret'};
 };
 app.loadIdentity=async()=>{assert.equal(app.team,'my-team');};
 await app.prepareSetup();assert.equal(app.setupStep,0);assert.deepEqual(calls,[]);
 app.setupTeamName=' My team ';await app.createSetupTeam();assert.equal(app.setupStep,1);
 await app.prepareSetup();assert.equal(app.setupStep,2);assert.equal(app.setupToken,'fake-setup-secret');assert.equal(app.setupShowToken,false);
 assert.deepEqual(calls,['/orgs','/auth/cli-token']);
 assert.ok(!Array.from(stored.values()).some(v=>v.includes('fake-setup-secret')));
});
test('Failed team creation stays in the modal and duplicate submissions are blocked',async()=>{
 const {app}=setup();app.team='';app.setupStep=0;app.setupTeamName='My team';let reject,calls=0,closed=0;
 app.$refs={setupDialog:{close(){closed++;}}};app.api=()=>{calls++;return new Promise((_,r)=>reject=r);};
 const pending=app.createSetupTeam();await app.createSetupTeam();app.closeSetup();assert.equal(calls,1);assert.equal(closed,0);
 reject(new Error('Could not create team'));await pending;
 assert.equal(app.setupStep,0);assert.equal(app.setupLoading,false);assert.equal(app.setupToken,null);assert.equal(app.setupError,'Could not create team');
});
test('Successful team creation is retained if refreshing the account fails',async()=>{
 const {app}=setup();app.team='';app.setupStep=0;app.setupTeamName='My team';
 app.api=async()=>({org:'my-team'});app.loadIdentity=async()=>{throw new Error('Refresh failed');};
 await app.createSetupTeam();assert.equal(app.team,'my-team');assert.equal(app.setupStep,1);
 app.api=async(url,_,team)=>{assert.equal(url,'/auth/cli-token');assert.equal(team,'my-team');return {token:'test-token'};};
 await app.prepareSetup();assert.equal(app.setupStep,2);
});

test('New Arena visits show two editable examples without starting a run',async()=>{
 const {app,runtime,mounted}=setup({pathname:'/enrich-arena',search:''});runtime.setInterval=()=>1;
 app.tasks[0].examples=[[{full_name:'First Example',domain:'one.example'},{full_name:'Second Example',domain:'two.example'}]];
 app.inputs={};app.loadInsights=()=>{};app.loadIdentity=async()=>{app.user=null;};app.api=async path=>path==='/arena/tasks'?app.tasks:{};
 app.startRun=()=>assert.fail('Samples must never auto-run');await mounted.call(app);
 assert.equal(app.inputs.domain,'one.example');assert.equal(app.extraInputs.length,1);assert.equal(app.extraInputs[0].domain,'two.example');
 app.inputs.domain='edited.example';assert.equal(app.tasks[0].examples[0][0].domain,'one.example');
});
test('Saved drafts, including intentionally blank rows, are not replaced by samples',async()=>{
 for(const inputs of [{full_name:'My Person',domain:'mine.example'},{}]){
  const {app,runtime,stored,mounted}=setup({pathname:'/enrich-arena',search:''});runtime.setInterval=()=>1;
  app.tasks[0].examples=[[{full_name:'First Example',domain:'one.example'},{full_name:'Second Example',domain:'two.example'}]];
  stored.set('treg.arena.draft.v1',JSON.stringify({taskId:app.taskId,variant:0,inputs,extraInputs:[],at:Date.now()}));
  app.loadInsights=()=>{};app.loadIdentity=async()=>{app.user=null;};app.api=async path=>path==='/arena/tasks'?app.tasks:{};
  await mounted.call(app);assert.equal(JSON.stringify(app.inputs),JSON.stringify(inputs));assert.equal(app.extraInputs.length,0);
 }
});
test('Task and input type switches select their own examples without modifying shared samples',()=>{
 const {app}=setup();app.booted=false;
 app.tasks.push({id:'companies.enrich',variants:[['domain'],['name']],examples:[[{domain:'one.example'},{domain:'two.example'}],[{name:'One Company'},{name:'Two Company'}]],fields:[]});
 app.chooseTask('companies.enrich');assert.equal(app.inputs.domain,'one.example');assert.equal(app.extraInputs.length,1);
 app.chooseVariant(1);assert.equal(app.inputs.name,'One Company');assert.equal(app.inputs.domain,undefined);
 app.extraInputs[0].name='Edited';app.chooseVariant(0);app.chooseVariant(1);assert.equal(app.extraInputs[0].name,'Two Company');
});
test('Arena counts arrival before data loading, including a failed page-data request',async()=>{
 const {app,runtime,mounted}=setup({pathname:'/enrich-arena',search:''}),events=[];
 runtime.setInterval=()=>1;app.loadInsights=async()=>{};
 runtime.window.TregTracking={capture:(...args)=>events.push(args)};
 app.api=async()=>{assert.equal(events[0][0],'arena_page_viewed');throw new Error('Data unavailable');};
 await mounted.call(app);
 assert.equal(events.length,1);assert.equal(app.error,'Data unavailable');
});

test('Phone format column follows published data for displayed providers',()=>{
 const {app}=setup();app.taskId='people.phone.find';
 assert.equal(app.showVerifiedRateColumn,false);
 app.tasks.push({id:'people.phone.find',variants:[['linkedin_url']],provider_previews:[[{provider:'quickenrich',endpoint_id:'quickenrich.phone'}]]});
 const audit={task:app.taskId,input:app.insightInput,endpoint:'quickenrich.phone',method:'phone_format',checked_n:19,format_validity_rate:0};
 app.insights={verification:{rows:[audit]}};
 assert.equal(app.showVerifiedRateColumn,false,'Insufficient checks stay hidden');
 audit.checked_n=20;assert.equal(app.showVerifiedRateColumn,true,'Zero is a real format validity rate');
 app.customServices=true;app.services=[];assert.equal(app.showVerifiedRateColumn,false,'Hidden vendors do not keep the column visible');
 app.taskId='people.email.find';assert.equal(app.showVerifiedRateColumn,true,'Email validity visibility is unchanged');
});

test('Run costs include every charge but count found entries once across vendors',()=>{
 const {app}=setup();app.run={charged_micro:90000,results:[
  {state:'hit',entry_index:0,charged_micro:30000,verification:{charged_micro:10000}},
  {state:'hit',entry_index:0,charged_micro:20000},
  {state:'hit',entry_index:1,charged_micro:30000},
  {state:'miss',entry_index:2,charged_micro:10000}]};
 assert.equal(app.runCostSummary.total,90000);assert.equal(app.runCostSummary.found,2);assert.equal(app.runCostSummary.average,45000);
 app.resultFilter='unresolved';assert.equal(app.runCostSummary.average,45000);
 app.run.charge_pending=true;assert.equal(app.runCostSummary.average,null);
 app.run.charge_pending=false;app.run.results[0].charged_micro=null;assert.equal(app.runCostSummary.pending,true);
 app.run.results=[];assert.equal(app.runCostSummary.average,null);
 app.run={charged_micro:0,results:[{state:'hit',charged_micro:0}]};assert.equal(app.runCostSummary.average,0);
});
