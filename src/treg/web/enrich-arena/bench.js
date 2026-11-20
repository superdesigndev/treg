/* Read the published benchmark from the existing landing page; keep scores in one place. */
(function(root){
  const systems={
    treg:{id:'treg',label:'Claude Code + treg',icon:'/favicon.svg',color:'#548A7B'},
    lessie:{id:'lessie',label:'Lessie',icon:'/media/people-search/lessie.png',color:'#A5B49A'},
    exa:{id:'exa',label:'Exa',icon:'/media/people-search/exa.png',color:'#99AAC5'},
    claude:{id:'claude',label:'Claude Code alone',icon:'/media/people-search/claude-code.png',color:'#BEA4A0'}
  };
  function parseDocument(doc){
    const groups=Array.from(doc.querySelectorAll('#bench .bgroup'));
    if(groups.length!==4)throw new Error('The published benchmark format has changed.');
    const categories=groups.map(group=>{
      const name=group.querySelector('.bglab')?.textContent.trim();
      if(!name)throw new Error('Benchmark category is missing.');
      const rows=Array.from(group.querySelectorAll('.bcol')).map(column=>{
        const src=column.querySelector('img')?.getAttribute('src')||'';
        const key=src.endsWith('/lessie.png')?'lessie':src.endsWith('/exa.png')?'exa':src.endsWith('/claude-code.png')?'claude':column.querySelector('svg')&&column.querySelector('.win')?'treg':null;
        const value=column.querySelector('em')?.textContent.trim()||'';
        const score=Number(value);
        if(!key||!/^\d+(\.\d+)?$/.test(value)||!Number.isFinite(score)||score<0||score>100)throw new Error('Benchmark scores are unavailable.');
        return {...systems[key],score};
      });
      if(rows.length!==4||new Set(rows.map(row=>row.id)).size!==4)throw new Error('Benchmark systems are incomplete.');
      return {id:name.toLowerCase().replace(/[^a-z0-9]+/g,'-'),name,rows};
    });
    if(new Set(categories.map(category=>category.id)).size!==4)throw new Error('Benchmark categories are incomplete.');
    return categories;
  }
  root.ArenaBench={parseDocument};
})(typeof window==='undefined'?globalThis:window);
