const clamp=v=>Math.max(0,Math.min(1,v));
const smooth=v=>{const t=clamp(v);return t*t*(3-2*t);};
export function introPose(time,index=0){
 // One shared, eased Bezier timeline: grow while travelling, then settle.
 const t=smooth(time/3.6),u=1-t;
 const dock=3*u*u*t*.05+3*u*t*t*.92+t*t*t;
 const scale=.3*u*u*u+3*1.3*u*u*t+3*1.12*u*t*t+t*t*t;
 const settle=1-smooth((time-.35-index*.075)/1.8);
 const pulse=clamp((time-1.2-(5-index)*.13)/.7);
 return {yaw:.36*(1-t),pitch:-.13*(1-t),
  spread:.65*(1-smooth((time-.45)/2.05)),lift:(.19+index*.035)*settle,
  glow:Math.sin(pulse*Math.PI)**2,scale,dock};
}
export function createIntro(host,root,layers){
 const hero=host.closest('.hero'),html=document.documentElement;
 if(!hero||!html.classList.contains('opening-stage')||html.classList.contains('motion-off')||scrollY>innerHeight*.35){window.tregOpening?.release();return null;}
 let elapsed=0,active=true;
 const rect=host.getBoundingClientRect(),dx=innerWidth*.5-(rect.left+rect.width*.5),dy=innerHeight*.43-(rect.top+rect.height*.5);
 const originalTranslate=host.style.translate,originalOpacity=host.style.opacity,items=[];
 // Fade the composed model, preserving its glass and metal material settings.
 host.style.opacity='0';
 function reveal(selector,start,duration=.85){
  document.querySelectorAll(selector).forEach((node,i)=>{
   items.push({node,start:start+i*.055,duration,opacity:node.style.opacity,filter:node.style.filter,translate:node.style.translate,pointer:node.style.pointerEvents,visibility:node.style.visibility,transition:node.style.transition});
   // Agent hover transitions affect only surface/scale, not the entrance properties.
   // Keep them live when pointer access resumes during the opening sequence.
   if(!node.matches('.aghead'))node.style.transition='none';
   node.style.visibility='hidden';node.style.opacity='0';node.style.pointerEvents='none';
  });
 }
 reveal('.navwrap',0,.7);reveal('.hero .kicker',1.95);reveal('.hero .hero-h1',2.05);
 reveal('.hero .hero-particles',.05,.9);reveal('.hero .gateway-routes',2.2,1.0);
 reveal('.hero .hcell:not(.mid) .sl',2.25);reveal('.hero .aghead',2.3);
 reveal('.hero .hsteps',2.5);reveal('.hero .htool:not(.more)',2.4,.85);
 reveal('.hero .hfoot,.hero .htool.more',2.75);reveal('.hero .hcli',2.7);
 reveal('.hero .givebox',2.8,.9);
 html.classList.remove('opening-pending');host.dataset.intro='running';
 function stop(){
  if(!active)return;active=false;
  items.forEach(({node,opacity,filter,translate,pointer,visibility,transition})=>{node.style.opacity=opacity;node.style.filter=filter;node.style.translate=translate;node.style.pointerEvents=pointer;node.style.visibility=visibility;node.style.transition=transition;});
  host.style.translate=originalTranslate;host.style.opacity=originalOpacity;root.scale.setScalar(1);
  window.tregParticles?.setOpening(-1);
  html.style.removeProperty('--opening-background');html.classList.remove('opening-pending','opening-stage');
  host.dataset.intro='complete';window.removeEventListener('treg:skip-opening',stop);window.removeEventListener('resize',stop);window.tregOpening?.release();
 }
 function update(dt,enabled){
  if(!active)return false;
  if(!enabled||scrollY>innerHeight*.35){stop();return false;}
  elapsed+=dt;if(elapsed>=6.05){stop();return false;}
  host.style.opacity=String(smooth(elapsed/1.2));
  window.tregParticles?.setOpening(elapsed,.5,innerHeight*.43/hero.clientHeight);
  for(const item of items){const p=smooth((elapsed-item.start)/item.duration);
   item.node.style.visibility=p>0?item.visibility:'hidden';
   item.node.style.opacity=String(p);item.node.style.filter=`blur(${(1-p)*7}px)`;
   item.node.style.translate=`0 ${(1-p)*24}px`;item.node.style.pointerEvents=p>.65?item.pointer:'none';
  }
  html.style.setProperty('--opening-background',1-smooth((elapsed-1.8)/1.8));return true;
 }
 function apply(){
  if(!active)return;
  const pose=introPose(elapsed);root.scale.setScalar(pose.scale);
  host.style.translate=`${dx*(1-pose.dock)}px ${dy*(1-pose.dock)}px`;
  root.rotation.y+=pose.yaw;root.rotation.x+=pose.pitch;
  for(const part of root.children)part.position.z+=part.userData.baseZ*pose.spread;
  layers.forEach((layer,i)=>{const p=introPose(elapsed,i);layer.group.position.y+=p.lift;
   layer.group.traverse(o=>{if(o.isMesh)o.material.emissiveIntensity+=p.glow*(o.name==='Jade optical frame'?.7:2.4);});
  });
 }
 window.tregParticles?.setOpening(0,.5,innerHeight*.43/hero.clientHeight);
 window.addEventListener('treg:skip-opening',stop);window.addEventListener('resize',stop);
 return {update,apply,stop};
}
