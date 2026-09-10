const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const runtime={window:{}};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../src/treg/web/enrich-arena/bench.js'),'utf8'),runtime);
const {parseDocument}=runtime.window.ArenaBench;
function documentFixture(change=()=>{}){
 const groups=['Recruiting','B2B prospecting','Deterministic','Influencer'].map(name=>({name,rows:[{system:'treg',value:'0'},{system:'lessie',value:'25.5'},{system:'exa',value:'100'},{system:'claude-code',value:'40'}]}));
 change(groups);
 return {querySelectorAll:()=>groups.map(group=>({
  querySelector:()=>({textContent:group.name}),
  querySelectorAll:()=>group.rows.map(row=>({querySelector:selector=>selector==='em'?{textContent:row.value}:selector==='img'?(row.system==='treg'?null:{getAttribute:()=>'/media/people-search/'+row.system+'.png'}):row.system==='treg'?{}:null}))
 }))};
}
test('Published benchmark parser preserves category identity, genuine zero and the 100-point bound',()=>{
 const categories=parseDocument(documentFixture());assert.equal(categories.length,4);assert.equal(categories[1].id,'b2b-prospecting');
 assert.deepEqual(Array.from(categories[0].rows,r=>r.score),[0,25.5,100,40]);
 assert.deepEqual(Array.from(categories[0].rows,r=>r.label),['Claude Code + treg','Lessie','Exa','Claude Code alone']);
});
test('Changed or incomplete benchmark source fails explicitly instead of showing invented scores',()=>{
 for(const change of [g=>g.pop(),g=>g[0].rows.pop(),g=>g[0].rows[0].value='',g=>g[0].rows[0].value='NaN',g=>g[0].rows[0].value='101',g=>g[0].rows[0].value='-1',g=>g[0].rows[0].system='unexpected',g=>g[0].rows[0].system='exa',g=>g[0].name='',g=>g[1].name=g[0].name])assert.throws(()=>parseDocument(documentFixture(change)),/benchmark|Benchmark/);
});
