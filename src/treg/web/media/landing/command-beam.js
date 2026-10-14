/* Arc-length colour ribbon: segments bend around corners instead of rotating a blob.
   Palette reference: Libraries.dev BorderBeam colorful; independent native SVG renderer. */
(()=>{
 const host=document.querySelector('.hero .gb-cmd');if(!host)return;
 const ns='http://www.w3.org/2000/svg';
 const svg=document.createElementNS(ns,'svg');svg.classList.add('command-beam');
 svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');host.prepend(svg);
 const palette=['#ff3264','#288cff','#32c850','#1eb9aa','#6446ff','#f032b4','#ff7828'];
 function colorAt(value){
  const base=Math.floor(value),f=value-base;
  const channels=hex=>[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16));
  const a=channels(palette[base%palette.length]),b=channels(palette[(base+1)%palette.length]);
  return `rgb(${a.map((c,i)=>Math.round(c+(b[i]-c)*f)).join(',')})`;
 }
 function resize(){
  const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;
  const r=Math.max(1,Math.min(parseFloat(getComputedStyle(host).borderRadius)||16,h/2-1,w/2-1));
  const x=1,y=1,right=w-1,bottom=h-1;
  const d=`M ${x+r} ${y} H ${right-r} A ${r} ${r} 0 0 1 ${right} ${y+r} V ${bottom-r} A ${r} ${r} 0 0 1 ${right-r} ${bottom} H ${x+r} A ${r} ${r} 0 0 1 ${x} ${bottom-r} V ${y+r} A ${r} ${r} 0 0 1 ${x+r} ${y} Z`;
  svg.setAttribute('viewBox',`0 0 ${w} ${h}`);svg.replaceChildren();
  const probe=document.createElementNS(ns,'path');probe.setAttribute('d',d);svg.append(probe);
  const perimeter=probe.getTotalLength();probe.remove();
  // A broad, feathered ribbon covers half the perimeter, at constant CSS px/second.
  const length=perimeter*.52,segments=32,step=length/segments;
  svg.style.setProperty('--beam-duration',`${perimeter/180}s`);
  for(const [name,stroke] of [['beam-wash',30],['beam-rim',1.2]]){
   const group=document.createElementNS(ns,'g');group.classList.add(name);svg.append(group);
   for(let i=0;i<segments;i++){
    const path=document.createElementNS(ns,'path'),phase=(i+.5)/segments;
    path.setAttribute('d',d);path.setAttribute('stroke-width',stroke);
    path.setAttribute('stroke-dasharray',`${step+.5} ${perimeter-step-.5}`);
    path.setAttribute('opacity',Math.pow(Math.sin(Math.PI*phase),1.3));
    const start=-i*step,color=phase*palette.length;
    path.style.setProperty('--beam-start',String(start));path.style.setProperty('--beam-end',String(start-perimeter));
    path.style.setProperty('--beam-color-a',colorAt(color));
    path.style.setProperty('--beam-color-b',colorAt(color+2));
    path.style.setProperty('--beam-color-c',colorAt(color+4));
    path.setAttribute('stroke',colorAt(color));group.append(path);
   }
  }
 }
 const observer=new ResizeObserver(resize);observer.observe(host);resize();
 window.addEventListener('pagehide',event=>{if(!event.persisted){observer.disconnect();svg.remove();}});
 window.addEventListener('pageshow',event=>{if(event.persisted)resize();});
})();
