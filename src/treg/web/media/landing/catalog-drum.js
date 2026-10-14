/* Native document scroll drives a sticky catalog; no wheel interception. */
(()=>{
 const section=document.querySelector('#catalog'),track=section.querySelector('.catwrap');
 const viewport=document.createElement('div');viewport.className='catalog-window';track.before(viewport);viewport.append(track);
 const nodes=[...track.querySelectorAll('.pcat,.pcard,.moreline')];
 const media=matchMedia('(min-width: 901px) and (pointer: fine) and (prefers-reduced-motion: no-preference)');
 let active=false,items=[],travel=0,last=-1,height=0;
 function measure(){
  active=media.matches&&!document.documentElement.classList.contains('motion-off');
  // Preserve the scroll range while measuring: clearing it clamps scrollY near the footer.
  section.classList.toggle('catalog-drum',active);track.style.transform='';
  nodes.forEach(n=>{n.style.transform='';n.style.opacity='';n.style.pointerEvents='';});
  if(!active){section.style.height='';return;}
  height=viewport.clientHeight;
  const origin=track.getBoundingClientRect().top;
  items=nodes.map(node=>{const r=node.getBoundingClientRect();return {node,center:r.top-origin+r.height/2};});
  travel=Math.max(0,track.offsetHeight-height*.8);
  section.style.height=(innerHeight+travel)+'px';last=-1;tick();
 }
 function tick(){
  if(!active||last===scrollY)return;last=scrollY;
  const progress=Math.max(0,Math.min(travel,-section.getBoundingClientRect().top));
  const lead=height*.05;
  track.style.transform=`translate3d(0,${lead-progress}px,0)`;
  for(const {node,center} of items){
   const y=center+lead-progress,distance=(y-height*.5)/(height*.5);
   const edgePhase=Math.min(1,(distance<0?progress:travel-progress)/(height*.18));
   const bend=Math.max(0,Math.min(1,(Math.abs(distance)-.6)/.4))*edgePhase;
   node.style.transform=`perspective(950px) translateZ(${-bend*bend*160}px) rotateX(${-Math.sign(distance)*bend*64}deg)`;
   node.style.opacity=String(1-bend*.82);
   node.style.pointerEvents=y<15||y>height-15?'none':'';
  }
 }
 viewport.addEventListener('focusin',event=>{
  if(!active)return;
  const item=items.find(i=>i.node.contains(event.target));if(!item)return;
  const start=section.getBoundingClientRect().top+scrollY;
  const target=start+Math.max(0,Math.min(travel,item.center-height*.45));
  window.scrollTo({top:Math.max(0,target),behavior:'instant'});
 });
 window.addEventListener('resize',measure);media.addEventListener('change',measure);
 // Lenis updates root classes on every scroll start/stop; those are not layout changes.
 let motionOff=document.documentElement.classList.contains('motion-off');
 new MutationObserver(()=>{
  const next=document.documentElement.classList.contains('motion-off');
  if(next===motionOff)return;
  motionOff=next;measure();
 }).observe(document.documentElement,{attributes:true,attributeFilter:['class']});
 document.fonts.ready.then(measure);window.addEventListener('load',measure,{once:true});
 window.addEventListener('pageshow',event=>{if(event.persisted)measure();});
 window.tregCatalog={tick};measure();
})();
