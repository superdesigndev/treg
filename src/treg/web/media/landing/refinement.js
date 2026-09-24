/* One scroll clock. Original copy, role scene, catalog and product animation remain owned by landing.html. */
(()=>{
 const root=document.documentElement, reduced=matchMedia('(prefers-reduced-motion: reduce)'), touch=matchMedia('(pointer: coarse)');
 const events=new AbortController(),signal=events.signal;
 const progress=document.createElement('div');progress.className='motion-progress';progress.ariaHidden='true';document.body.append(progress);
 // Keep preference handling, but no floating motion-toggle UI.
 const control=document.createElement('button');control.className='motion-control';control.type='button';
 let off=false,lenis=null,frame=0,observer=null;const animations=new Map();
 const enabled=()=>!off&&!reduced.matches;
 const sections=[...document.querySelectorAll('.bens .ben,.zeroband')];
 const benefitParts=node=>[...node.querySelectorAll(':scope > .bentext,:scope > .keywall,:scope > .win')];
 const seen=new WeakSet();
 const ending=document.querySelector('.close'),footer=document.querySelector('footer');
 const reveal=document.querySelector('.ending-reveal');
 let lastEndScroll=-1,lastEndHeight=-1;
 function endParallax(){
  if(lastEndScroll===scrollY&&lastEndHeight===innerHeight)return;
  lastEndScroll=scrollY;lastEndHeight=innerHeight;
  const active=enabled()&&!touch.matches&&innerWidth>640;
  const boundary=reveal.getBoundingClientRect();
  const revealProgress=Math.max(0,Math.min(1,(innerHeight-boundary.top)/(innerHeight*.45)));
  root.style.setProperty('--top-veil-opacity',(1-revealProgress).toFixed(4));
  document.body.toggleAttribute('data-dark-nav',boundary.top<80);
  const a=ending.getBoundingClientRect(),b=footer.getBoundingClientRect();
  const opening=r=>{
   const settleTop=Math.max(innerHeight*.3,innerHeight-r.height);
   const t=Math.max(0,Math.min(1,(innerHeight-r.top)/Math.max(1,innerHeight-settleTop)));
   return 1-Math.pow(1-t,2);
  };
  // White edge travels at native speed; the entire black layer travels at 35%.
  // Measure the untransformed wrapper to avoid feedback from the moving layer.
  // Short endings may never reach the viewport top: settle by document end
  // so the heading is never left clipped beneath the white reveal edge.
  const restTop=Math.max(0,innerHeight-boundary.height);
  const offset=active?-Math.max(0,Math.min(innerHeight,boundary.top-restTop))*.65:0;
  reveal.style.setProperty('--reveal-offset',offset.toFixed(2)+'px');
  const curtain=active?opening({top:boundary.top,height:ending.offsetHeight}):1;
  const foot=active?opening(b):1;
  reveal.style.setProperty('--curtain-open',curtain.toFixed(4));
  reveal.style.setProperty('--closing-rise',((1-curtain)*90).toFixed(2)+'px');
  footer.style.setProperty('--footer-rise',((1-foot)*60).toFixed(2)+'px');
  ending.style.setProperty('--end-drift','0px');
  footer.style.setProperty('--mark-drift',((1-foot)*80).toFixed(2)+'px');
 }
 function clear(){cancelAnimationFrame(frame);frame=0;lenis?.destroy();lenis=null;observer?.disconnect();animations.forEach(a=>a.cancel());animations.clear();document.querySelectorAll('.entrance-waiting').forEach(n=>n.classList.remove('entrance-waiting'));document.querySelectorAll('.keywall').forEach(n=>n.classList.remove('motion-visible'));}
 function finish(node){animations.get(node)?.cancel();animations.delete(node);}
 function play(node){
  if(!enabled()||touch.matches||innerWidth<800)return;
  finish(node);
  const visual=node.matches('.keywall,.win');
  const frames=[
   {opacity:0,filter:'blur(10px)',transform:`translate3d(0,${visual?54:44}px,0)`},
   {opacity:1,filter:'blur(0px)',transform:'translate3d(0,0,0)'}
  ];
  const stagger=node.matches('.foot-col')?[...node.parentElement.children].indexOf(node)*90:visual?180:0;
  const animation=node.animate(frames,{duration:1450,delay:stagger,fill:'backwards',easing:'cubic-bezier(.2,.65,.3,1)'});
  animations.set(node,animation);animation.onfinish=()=>{animation.cancel();animations.delete(node);};
 }
 function tick(time){if(document.hidden){frame=0;return;}lenis?.raf(time);window.tregModel?.tick(time);window.tregParticles?.tick(time);window.tregEndingParticles?.tick(time);window.tregBenefitParticles?.tick(time);window.tregCatalog?.tick();endParallax();progress.style.transform='scaleX('+Math.min(1,scrollY/Math.max(1,document.documentElement.scrollHeight-innerHeight))+')';frame=requestAnimationFrame(tick);}
 function configure(){
  lastEndScroll=-1;lastEndHeight=-1;
  clear();root.classList.toggle('motion-off',!enabled());control.innerHTML=enabled()?'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 5v10M13 5v10"/></svg>':'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7 4 8 6-8 6Z"/></svg>';control.setAttribute('aria-label',enabled()?'Pause motion':'Enable motion');control.setAttribute('aria-pressed',String(!enabled()));
  if(enabled()&&!touch.matches&&window.Lenis)lenis=new Lenis({lerp:.12,smoothWheel:true,syncTouch:false,anchors:true,prevent:n=>!!n.closest('.scrim')});
  const canEnter=enabled()&&!touch.matches&&innerWidth>=901;
  sections.forEach(n=>n.classList.toggle('entrance-waiting',canEnter&&!seen.has(n)));
  observer=new IntersectionObserver(entries=>entries.forEach(e=>{
   const node=e.target,benefit=node.matches('.ben'),parts=benefit?benefitParts(node):[node];
   node.querySelector('.keywall')?.classList.toggle('motion-visible',e.isIntersecting&&enabled());
   // Each section owns its entrance; do not start at the first intersecting pixel.
   if(e.isIntersecting&&e.intersectionRatio>=.3&&!seen.has(node)&&canEnter){
    seen.add(node);node.classList.remove('entrance-waiting');parts.forEach(play);
   }
   if(!e.isIntersecting)parts.forEach(finish);
  }),{threshold:[0,.3],rootMargin:'0px 0px -8% 0px'});
  sections.forEach(n=>observer.observe(n));frame=requestAnimationFrame(tick);
 }
 control.addEventListener('click',()=>{off=!off;configure();},{signal});reduced.addEventListener('change',configure,{signal});touch.addEventListener('change',configure,{signal});
 matchMedia('(min-width: 901px)').addEventListener('change',configure,{signal});
 document.querySelector('.bens').addEventListener('focusin',e=>{const node=e.target.closest('.ben');if(node){seen.add(node);node.classList.remove('entrance-waiting');benefitParts(node).forEach(finish);}},{signal});
 document.addEventListener('visibilitychange',()=>{if(document.hidden){cancelAnimationFrame(frame);frame=0;document.querySelectorAll('.keywall').forEach(n=>n.classList.remove('motion-visible'));}else configure();},{signal});
 const scrim=document.querySelector('#signin-scrim');const modalObserver=new MutationObserver(()=>{if(scrim.classList.contains('open'))lenis?.stop();else lenis?.start();});if(scrim)modalObserver.observe(scrim,{attributes:true,attributeFilter:['class']});
 configure();
 // BFCache restores without rerunning initialization.
 window.addEventListener('pagehide',event=>{
  if(event.persisted){cancelAnimationFrame(frame);frame=0;lenis?.stop();return;}
  clear();events.abort();modalObserver.disconnect();
 },{signal});
 window.addEventListener('pageshow',event=>{if(event.persisted)configure();},{signal});
})();
