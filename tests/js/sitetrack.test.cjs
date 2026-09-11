const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../src/treg/web/sitetrack.js'),'utf8');
function setup({pathname='/enrich-arena',search='',cookies='',storage=new Map(),blocked=false}={}){
 const calls=[],jar=new Map(cookies.split(';').filter(Boolean).map(v=>v.trim().split('=')));
 const document={referrer:'',get cookie(){return [...jar].map(([k,v])=>k+'='+v).join('; ');},set cookie(v){const [k,value]=v.split(';')[0].split('=');jar.set(k,value);}};
 const ph=Object.fromEntries(['capture','identify','reset','resetGroups','group'].map(k=>[k,(...args)=>calls.push([k,...args])]));
 const localStorage={getItem(k){if(blocked)throw Error('blocked');return storage.get(k);},setItem(k,v){if(blocked)throw Error('blocked');storage.set(k,v);},removeItem(k){storage.delete(k);}};
 const window={location:{pathname,search,protocol:'https:',hostname:'registry'},posthog:ph};
 vm.runInNewContext(source,{window,document,localStorage,URLSearchParams});
 return {tracking:window.TregTracking,calls,jar,storage};
}
test('First landing surface survives Arena -> dashboard and checkout source stays separate',()=>{
 const a=setup({search:'?capability=people.email.find&run=private-run'});
 assert.equal(a.jar.get('treg_entry_surface'),'arena');
 const b=setup({pathname:'/app',search:'?from=enrich-arena',cookies:'treg_entry_surface=arena'});
 assert.equal(b.tracking.checkoutSource(),'arena');
 b.tracking.capture('test');assert.equal(b.calls[0][2].entry_surface,'arena');assert.equal(b.calls[0][2].surface,'app');
 assert.equal(JSON.stringify(b.calls).includes('private-run'),false);
 assert.equal(setup({pathname:'/app'}).tracking.checkoutSource(),'app');
 assert.equal(setup({cookies:'treg_entry_surface=private%40email.com'}).jar.get('treg_entry_surface'),'arena');
});
test('Signup identity links anonymous visits without reset, switches teams, and resets a different account',()=>{
 const {tracking,calls}=setup();
 tracking.identify('person@example.test','');
 assert.equal(calls[0][0],'identify');assert.equal(calls[0][3].entry_surface,'arena');
 assert.equal(calls.some(c=>c[0]==='reset'),false);
 tracking.identify('person@example.test','team-a');
 tracking.identify('person@example.test','team-b');
 assert.deepEqual(calls.filter(c=>c[0]==='group').map(c=>c.slice(1)),[['team','team-a'],['team','team-b']]);
 tracking.identify('other@example.test','');assert.equal(calls.filter(c=>c[0]==='reset').length,1);
 tracking.identify('','');assert.equal(calls.filter(c=>c[0]==='reset').length,2);
});
test('Blocked storage does not prevent identity linking or break the page',()=>{
 const {tracking,calls}=setup({blocked:true});tracking.identify('person@example.test','team');
 assert.equal(calls[0][0],'identify');assert.equal(calls.at(-1)[0],'group');
});
