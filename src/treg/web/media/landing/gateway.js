/* The original ROLES / setRole remain the single source of product content. */
(()=>{
 const scene=document.getElementById('scene'),svg=scene.querySelector('.gateway-routes'),object=scene.querySelector('.gateway-sculpture'),tools=document.getElementById('sc-tools');
 const ns='http://www.w3.org/2000/svg',events=new AbortController(),signal=events.signal;
 const reduced=matchMedia('(prefers-reduced-motion: reduce)'),coarse=matchMedia('(pointer: coarse)');
 let selected=-1,layoutFrame=0,visible=true;
 const canMove=()=>visible&&!document.hidden&&!reduced.matches&&!document.documentElement.classList.contains('motion-off');
 function select(i){selected=i;[...tools.querySelectorAll('.htool:not(.more)')].forEach((t,n)=>{t.classList.toggle('route-active',n===i);t.setAttribute('aria-pressed',String(n===i));});svg.querySelectorAll('[data-route]').forEach(p=>p.classList.toggle('route-active',+p.dataset.route===i));}
 function draw(){
  layoutFrame=0;const bounds=scene.getBoundingClientRect(),center=object.getBoundingClientRect(),agent=scene.querySelector('.aghead').getBoundingClientRect(),mobile=innerWidth<=640;
  svg.setAttribute('viewBox',`0 0 ${bounds.width} ${bounds.height}`);svg.replaceChildren();
  const c={x:center.left-bounds.left+center.width*.57,y:center.top-bounds.top+center.height*.52};
  const a={x:mobile?agent.left-bounds.left+agent.width*.5:agent.right-bounds.left,y:mobile?agent.top-bounds.top:agent.top-bounds.top+agent.height*.5};
  function path(d,index,pulse=false){const p=document.createElementNS(ns,'path');p.setAttribute('d',d);p.setAttribute('pathLength','100');p.dataset.route=index;if(pulse)p.classList.add('route-pulse');svg.append(p);}
  [...tools.querySelectorAll('.htool:not(.more)')].forEach((tile,i)=>{const r=tile.getBoundingClientRect(),t={x:mobile?r.left-bounds.left+r.width*.5:r.left-bounds.left,y:mobile?r.top-bounds.top:r.top-bounds.top+r.height*.5};const d=mobile?`M${a.x},${a.y} C${a.x},${c.y} ${c.x},${a.y-50} ${c.x},${c.y} C${c.x},${t.y-60} ${t.x},${c.y+40} ${t.x},${t.y}`:`M${a.x},${a.y} C${a.x+90},${a.y} ${c.x-100},${c.y} ${c.x},${c.y} C${c.x+110},${c.y} ${t.x-75},${t.y} ${t.x},${t.y}`;path(d,i);path(d,i,true);});select(selected);
 }
 function schedule(){if(!layoutFrame)layoutFrame=requestAnimationFrame(draw);}
 function sync(){selected=-1;tools.querySelectorAll('.htool:not(.more)').forEach((span,i)=>{const b=document.createElement('button');b.type='button';b.className=span.className;b.innerHTML=span.innerHTML;b.dataset.tool=i;b.setAttribute('aria-label',`Highlight ${ROLES[roleI].tools[i][1]} connection`);span.replaceWith(b);});schedule();}
 // Delegate listeners so automatic role changes do not retain discarded tiles.
 for(const kind of ['pointerover','focusin','click'])tools.addEventListener(kind,e=>{const b=e.target.closest('button[data-tool]');if(!b)return;select(+b.dataset.tool);},{signal});
 tools.addEventListener('pointerout',e=>{const next=e.relatedTarget?.closest?.('button[data-tool]');const focused=tools.querySelector('button:focus-visible');select(next?+next.dataset.tool:focused?+focused.dataset.tool:-1);},{signal});
 tools.addEventListener('focusout',()=>{const hovered=tools.querySelector('button:hover');select(hovered?+hovered.dataset.tool:-1);},{signal});
 const agentCard=scene.querySelector('.aghead');
 agentCard.setAttribute('role','button');agentCard.tabIndex=0;
 agentCard.setAttribute('aria-label','Next agent scenario');
 let agentFeedback;
 function nextScenario(){
  if(document.documentElement.classList.contains('opening-stage'))return;
  agentFeedback?.cancel();
  const quiet=reduced.matches||document.documentElement.classList.contains('motion-off');
  agentFeedback=agentCard.animate([
   {scale:quiet?'1':'.985',boxShadow:'0 0 36px #b4edcf55'},
   {scale:quiet?'1':'1.008',offset:.45,boxShadow:'0 0 28px #b4edcf38'},
   {scale:'1',boxShadow:getComputedStyle(agentCard).boxShadow}
  ],{duration:quiet?160:420,easing:'cubic-bezier(.2,.7,.25,1)'});
  setRole(roleI+1);
  // Restart the existing clock so a manual switch gets a full reading interval.
  clearInterval(roleTimer);userTouched=false;startRotation();
 }
 agentCard.addEventListener('click',nextScenario,{signal});
 agentCard.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();nextScenario();}},{signal});
 function motionState(){scene.classList.toggle('gateway-paused',!canMove());if(!canMove()){object.style.removeProperty('--rx');object.style.removeProperty('--ry');object.style.removeProperty('--gy');}}
 scene.addEventListener('pointermove',e=>{if(!canMove()||coarse.matches)return;const r=scene.getBoundingClientRect();object.style.setProperty('--ry',((e.clientX-r.left)/r.width-.5)*7+'deg');object.style.setProperty('--rx',-((e.clientY-r.top)/r.height-.5)*5+'deg');},{signal});
 scene.addEventListener('pointerleave',()=>{object.style.setProperty('--rx','0deg');object.style.setProperty('--ry','0deg');},{signal});
 window.addEventListener('scroll',()=>{if(canMove()&&!coarse.matches)object.style.setProperty('--gy',Math.max(-9,Math.min(9,-scrollY*.025))+'px');},{passive:true,signal});
 window.addEventListener('treg:rolechange',sync,{signal});window.addEventListener('resize',schedule,{signal});document.addEventListener('visibilitychange',motionState,{signal});reduced.addEventListener('change',motionState,{signal});
 const size=new ResizeObserver(schedule);size.observe(scene);const visibility=new IntersectionObserver(es=>{visible=es[0].isIntersecting;motionState();});visibility.observe(scene);const pref=new MutationObserver(motionState);pref.observe(document.documentElement,{attributes:true,attributeFilter:['class']});
 sync();motionState();document.fonts.ready.then(schedule);window.addEventListener('pagehide',event=>{if(event.persisted)return;events.abort();size.disconnect();visibility.disconnect();pref.disconnect();cancelAnimationFrame(layoutFrame);},{signal});
 window.addEventListener('pageshow',event=>{if(event.persisted){schedule();motionState();}},{signal});
})();
