const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const web=path.join(__dirname,'../../src/treg/web');
// Vue's browser compiler only needs this DOM shim to decode HTML entities.
const decode=value=>value.replace(/&(?:quot|apos|lt|gt|amp|#\d+|#x[\da-f]+);/gi,entity=>{
 const named={'&quot;':'"','&apos;':"'",'&lt;':'<','&gt;':'>','&amp;':'&'};
 return named[entity]??String.fromCodePoint(entity[2].toLowerCase()==='x'?parseInt(entity.slice(3,-1),16):Number(entity.slice(2,-1)));
});
function compiler(){
 const runtime={document:{createElement:()=>({set innerHTML(value){this.textContent=decode(value);this.children=[{getAttribute:()=>decode(value.slice(10,-2))}];}})}};
 vm.runInNewContext(fs.readFileSync(path.join(web,'vendor/vue-3.5.41.global.prod.js'),'utf8'),runtime);
 return runtime.Vue.compile;
}
test('The actual Vue runtime compiles the shared Arena page and every component template',()=>{
 const html=fs.readFileSync(path.join(web,'enrich-arena.html'),'utf8'),compile=compiler();
 const main=html.slice(html.indexOf('<div id="arena"'),html.indexOf('<template id="arena-result-table-template">'));
 assert.equal(typeof compile(main),'function');
 const templates=[...html.matchAll(/<template id="([^"]+)">([\s\S]*?)<\/template>\s*(?=<template id=|<script)/g)];
 assert.ok(templates.length>0);
 for(const [,id,template]of templates)assert.doesNotThrow(()=>compile(template),id);
});
test('Template compilation catches nested interpolation inside an expression',()=>{
 assert.throws(()=>compiler()(`<span>{{true?'ok':'{{false?'nested':'bad'}}'}}</span>`),/missing|Unexpected|Syntax/);
});

test('Batch outcome badges render in the root Arena context',()=>{
 const html=fs.readFileSync(path.join(web,'enrich-arena.html'),'utf8');
 const matrix=html.slice(html.indexOf('class="results-table batch-matrix"'),html.indexOf('<template id="arena-result-table-template">'));
 const badge=matrix.match(/<span class="outcome"[^>]*>[\s\S]*?<\/span>/)[0];
 const result={state:'hit'};
 const vnode=compiler()(badge)({r:result,outcomeClass:r=>r.state==='hit'?'hit':'miss',outcomeLabel:()=> 'Found'},[]);
 assert.equal(vnode.props.class,'outcome hit');
 assert.equal(vnode.children,'Found');
});
