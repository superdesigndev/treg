/* Original Treg implementation of a travelling glyph field + cursor trail.
   One GPU point draw, no textures/video, no full-screen per-pixel trail loop. */
(()=>{
function mountField(hero,closing=false,benefits=false){
 if(!hero)return;
 const canvas=document.createElement('canvas');canvas.className=benefits?'benefit-particles':closing?'ending-particles':'hero-particles';canvas.setAttribute('aria-hidden','true');hero.prepend(canvas);
 const gl=canvas.getContext('webgl',{alpha:true,antialias:false,depth:false,stencil:false,premultipliedAlpha:true,powerPreference:'low-power'});
 if(!gl){canvas.remove();return;}
 const vertex=`
 precision highp float;
 attribute vec3 aPoint;
 uniform vec2 uSize,uDrift;
 uniform float uTime,uDpr,uCell;
 uniform vec3 uOpening;
 uniform mediump float uClosing;
 uniform float uBenefits;
 uniform vec3 uPointer;
 uniform vec3 uTrail[24];
 uniform vec4 uTextAreas[8];
 uniform vec2 uTextMotion[8];
 varying float vAlpha,vKind,vHeat;
 float segment(vec2 p,vec2 a,vec2 b){vec2 ab=b-a;float t=clamp(dot(p-a,ab)/max(dot(ab,ab),.001),0.,1.);return length(p-a-t*ab);}
 void main(){
  vec2 p=aPoint.xy+uDrift;
  if(uBenefits>.5){
   p.x+=sin(uTime*.65+aPoint.y*.014)*4.;
   p.y+=cos(uTime*.55+aPoint.x*.012)*5.;
  }
  float proximity=0.;
  if(uClosing>.5){
   // Quiet, continuous currents even when the pointer is idle.
   p.x+=sin(aPoint.y*.012-uTime*.55)*2.5;
   p.y+=sin(aPoint.x*.009+aPoint.y*.004-uTime*.42)*3.;
   vec2 away=p-uPointer.xy;float d=length(away);
   proximity=exp(-pow(d/155.,2.))*uPointer.z;
   p+=away/max(d,1.)*proximity*16.;
  }
  float ripple=0.,reveal=1.;
  if(uOpening.z>=0.){
   vec2 radial=p-uOpening.xy*uSize;
   float distance=length(radial),speed=length(uSize)*.17;
   float radius=max(0.,uOpening.z-.12)*speed;
   // One broad, quiet wave; no second pulse or repeated cycle.
   float ring=exp(-pow((distance-radius)/70.,2.));
   ripple=ring*(1.-smoothstep(3.8,5.7,uOpening.z));
   reveal=mix(1.-smoothstep(radius-110.,radius+110.,distance),1.,smoothstep(3.6,5.3,uOpening.z));
   p+=radial/max(distance,1.)*ripple*4.;
  }
  float w=0.5+0.30*sin(p.x*.010+p.y*.009-uTime*.65)+0.20*sin(p.x*-.006+p.y*.013-uTime*.43);
  float heat=0.;
  for(int i=0;i<23;i++){
   float life=min(uTrail[i].z,uTrail[i+1].z);
   float d=segment(p,uTrail[i].xy,uTrail[i+1].xy);
   if(uClosing>.5){
    heat=max(heat,exp(-pow(d/70.,2.))*smoothstep(0.,.85,life));
   }else if(life>0. && d<4.)heat=1.;
  }
  float edge=smoothstep(70.,200.,p.y)*(1.-smoothstep(uSize.y-160.,uSize.y,p.y));
  float quiet=1.-.55*exp(-pow((p.x-uSize.x*.5)/(uSize.x*.24),2.)-pow((p.y-uSize.y*.23)/120.,2.));
  vAlpha=((.035+.24*pow(w,1.65))*quiet*reveal+ripple*.07)*edge*step(.10,aPoint.z);
  vHeat=max(heat,min(1.,ripple*.12))*edge*step(.10,aPoint.z);vKind=mod(floor(aPoint.z*31.),3.);
  // Feather only the point field behind copy; retain the underlying colour wash.
  if(uClosing<.5 && uBenefits<.5){
   float readability=1.;
   for(int i=0;i<8;i++){
    vec4 area=uTextAreas[i];
    if(area.z>0.){
     area.y+=uTextMotion[i].y;
     vec2 outside=max(abs(p-area.xy)-area.zw,vec2(0.));
     float cover=1.-smoothstep(0.,96.,length(outside));
     readability=min(readability,1.-cover*.82*uTextMotion[i].x);
    }
   }
   vAlpha*=readability;vHeat*=readability;
  }
  gl_Position=vec4(p.x/uSize.x*2.-1.,1.-p.y/uSize.y*2.,0.,1.);
  gl_PointSize=(min(uCell*.82,5.0)+ripple*.35)*uDpr;
  if(uClosing>.5){
   float depth=clamp(aPoint.y/uSize.y,0.,1.);
   float current=.5+.5*sin(aPoint.x*.008+aPoint.y*.011-uTime*.7+sin(aPoint.x*.003+uTime*.2));
   float bottomQuiet=1.-.38*smoothstep(.5,1.,depth);
   vAlpha=(.008+.16*pow(depth,1.8))*bottomQuiet*(.58+.42*current)*smoothstep(0.,.2,depth)*step(.10,aPoint.z);
   vHeat=max(proximity*.38,heat*.26)*step(.10,aPoint.z);
   gl_PointSize=(min(uCell*.82,5.0)+proximity*.8)*uDpr;
  }
  if(uBenefits>.5){
   // Long diagonal streams, with highlights travelling along—not across—the flow.
   float along=p.x*.78-p.y*.63;
   float across=p.x*.63+p.y*.78;
   float bend=sin(along*.003-uTime*.24)*48.;
   float lane=abs(sin((across+bend-uTime*10.)*.006));
   float stream=1.-smoothstep(.18,.65,lane);
   float current=.5+.5*sin(along*.016-uTime*1.3+sin(across*.008));
   float readability=1.;
   for(int i=0;i<8;i++){
    vec4 area=uTextAreas[i];
    if(area.z>0.){
     vec2 outside=max(abs(p-area.xy)-area.zw,vec2(0.));
     readability=min(readability,.12+.88*smoothstep(0.,64.,length(outside)));
    }
   }
   float boundary=smoothstep(0.,180.,p.y)*(1.-smoothstep(uSize.y-220.,uSize.y,p.y));
   vAlpha=stream*(.07+.13*current)*boundary*readability;
   // Fine solid dots: mix tiny squares and round points, without hollow rings.
   vHeat=0.;vKind=3.+step(.5,aPoint.z);gl_PointSize=2.2*uDpr;
  }
 }`;
 const fragment=`
 precision mediump float;
 uniform mediump float uClosing;
 varying float vAlpha,vKind,vHeat;
 float box(vec2 p,vec2 c,float r){return 1.-smoothstep(r-.045,r+.045,max(abs(p.x-c.x),abs(p.y-c.y)));}
 void main(){
  vec2 p=gl_PointCoord-.5;float d=length(p);float glyph;
  if(vKind<.5)glyph=1.-smoothstep(.17,.23,d);
  else if(vKind<1.5)glyph=(1.-smoothstep(.35,.42,d))*smoothstep(.12,.19,d);
  else if(vKind<2.5)glyph=max(box(p,vec2(-.12,-.12),.20),box(p,vec2(.12,.12),.20));
  else if(vKind<3.5)glyph=box(p,vec2(0.),.34);
  else glyph=1.-smoothstep(.29,.40,d);
  float a=glyph*mix(vAlpha,.64,vHeat);
  vec3 col=mix(vec3(.19,.40,.34),vec3(.02,.59,.35),vHeat);
  if(uClosing>.5)col=mix(vec3(.88,.92,.96),vec3(1.),vHeat);
  gl_FragColor=vec4(col*a,a);
 }`;
 function shader(type,source){const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)){const message=gl.getShaderInfoLog(s);gl.deleteShader(s);throw Error(message);}return s;}
 let program,buffer,vs,fs;
 try{
  vs=shader(gl.VERTEX_SHADER,vertex);fs=shader(gl.FRAGMENT_SHADER,fragment);
  program=gl.createProgram();gl.attachShader(program,vs);gl.attachShader(program,fs);gl.linkProgram(program);
  if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(program));
 }catch(error){if(vs)gl.deleteShader(vs);if(fs)gl.deleteShader(fs);if(program)gl.deleteProgram(program);canvas.remove();console.warn('Particle background unavailable; retained gradient.',error);return;}
 gl.useProgram(program);buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
 const attribute=gl.getAttribLocation(program,'aPoint');gl.enableVertexAttribArray(attribute);gl.vertexAttribPointer(attribute,3,gl.FLOAT,false,0,0);
 const uniforms=Object.fromEntries(['uSize','uDrift','uTime','uDpr','uCell','uOpening','uClosing','uBenefits','uPointer','uTrail[0]','uTextAreas[0]','uTextMotion[0]'].map(n=>[n,gl.getUniformLocation(program,n)]));
 gl.enable(gl.BLEND);gl.blendFunc(gl.ONE,gl.ONE_MINUS_SRC_ALPHA);
 const reduced=matchMedia('(prefers-reduced-motion: reduce)'),coarse=matchMedia('(pointer: coarse)'),events=new AbortController(),signal=events.signal;
 let width=1,height=1,count=0,dpr=1,cell=7,visible=true,lost=false,disposed=false,dirty=true,last=0,time=0;
 let dx=0,dy=0,tx=0,ty=0,trail=[];const packed=new Float32Array(72);
 let opening=-1,openingX=.5,openingY=.43;
 const textAreas=new Float32Array(32);let textDirty=true;
 const textMotion=new Float32Array(16);let textNodes=[];
 const textShift=node=>parseFloat((node.style.translate||'').split(/\s+/)[1])||0;
 function measureText(){
  textAreas.fill(0);textDirty=false;if(closing)return;
  const origin=hero.getBoundingClientRect();
  textNodes=[...hero.querySelectorAll(benefits?'.bentext':'.hero-h1,.hcell:not(.mid) .sl,.hsteps,.hfoot,.htool.more,.hcli')].slice(0,8);
  textNodes.forEach((node,i)=>{
   const r=node.getBoundingClientRect();if(!r.width||!r.height)return;
   textAreas.set([r.left-origin.left+r.width/2,r.top-origin.top+r.height/2-(benefits?0:textShift(node)),r.width/2+8,r.height/2+6],i*4);
  });
 }
 let px=-1000,py=-1000,pointerX=-1000,pointerY=-1000,power=0,targetPower=0;
 const enabled=()=>!reduced.matches&&!document.documentElement.classList.contains('motion-off');
 function size(){
  textDirty=true;
  width=hero.clientWidth;height=hero.clientHeight;
  if(benefits){const last=hero.querySelector('.ben:last-of-type')||[...hero.querySelectorAll('.ben')].at(-1);if(last)height=last.getBoundingClientRect().bottom-hero.getBoundingClientRect().top;canvas.style.height=height+'px';}
  dpr=Math.min(devicePixelRatio||1,1.25);cell=Math.max(benefits?(coarse.matches?7:5):(coarse.matches?8:6),Math.sqrt(width*height/(benefits?150000:60000)));
  canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);gl.viewport(0,0,canvas.width,canvas.height);
  const points=[];let seed=71;
  const addPoint=(x,y)=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;points.push(x,y,seed/4294967296);};
  for(let y=-cell;y<height+cell;y+=cell)for(let x=-cell;x<width+cell;x+=cell)addPoint(x,y);
  count=points.length/3;gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(points),gl.STATIC_DRAW);
  canvas.dataset.points=String(count);dirty=true;
 }
 function clear(){tx=ty=0;trail=[];targetPower=0;power=0;px=py=pointerX=pointerY=-1000;dirty=true;}
 hero.addEventListener('pointermove',e=>{
  if(benefits||!enabled()||e.pointerType==='touch')return;
  const r=hero.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,now=performance.now();
  if(closing){
   // Begin at the entry point, then let the light follow just behind the cursor.
   if(power<.01){px=x;py=y;}
   pointerX=x;pointerY=y;targetPower=1;dirty=true;return;
  }
  else{tx=(x/width-.5)*-16;ty=(y/height-.5)*-12;}
  const prev=trail[trail.length-1];
  if(!prev||Math.hypot(x-prev.x,y-prev.y)>(closing?12:4)||now-prev.t>40){trail.push({x,y,t:now});if(trail.length>(closing?12:24))trail.shift();canvas.dataset.trailPeak=String(Math.max(Number(canvas.dataset.trailPeak)||0,trail.length));}
 },{signal,passive:true});
 hero.addEventListener('pointerleave',()=>{tx=ty=0;targetPower=0;},{signal});window.addEventListener('blur',clear,{signal});
 const resize=new ResizeObserver(size);resize.observe(hero);
 const observe=new IntersectionObserver(es=>{visible=es[0].isIntersecting;last=0;if(benefits)canvas.classList.toggle('field-visible',visible);if(!visible){clear();canvas.dataset.state='offscreen';}dirty=true;});observe.observe(benefits?canvas:hero);
 let motion=enabled();const prefs=new MutationObserver(()=>{const next=enabled();if(next!==motion){motion=next;clear();}});
 prefs.observe(document.documentElement,{attributes:true,attributeFilter:['class']});reduced.addEventListener('change',clear,{signal});
 const textObserver=new MutationObserver(()=>{textDirty=true;dirty=true;});
 if(!closing&&!benefits){
  textObserver.observe(hero,{childList:true,subtree:true});
  textObserver.observe(document.documentElement,{attributes:true,attributeFilter:['class']});
 }
 canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();lost=true;canvas.style.visibility='hidden';},{signal});
 canvas.addEventListener('webglcontextrestored',()=>{dispose();},{signal});
 function tick(now){
  if(disposed||lost||!visible||document.hidden){last=0;return;}
  const moving=enabled();if(!moving&&!dirty)return;
  if(textDirty)measureText();
  if(!closing&&!benefits){
   const pending=document.documentElement.classList.contains('opening-pending');
   textNodes.forEach((node,i)=>{
    // Reuse the UI's actual entrance values, without per-frame layout reads.
    textMotion[i*2]=pending?0:node.style.opacity===''?1:Number(node.style.opacity);
    textMotion[i*2+1]=textShift(node);
   });
  }
  const dt=last?Math.min((now-last)/1000,.05):0;last=now;if(moving)time+=dt;
  const ease=1-Math.exp(-dt*7);dx+=(tx-dx)*ease;dy+=(ty-dy)*ease;
  power+=(targetPower-power)*(1-Math.exp(-dt*(targetPower?5:closing?2.8:2.2)));
  if(closing&&moving){
   const follow=1-Math.exp(-dt*9);
   px+=(pointerX-px)*follow;py+=(pointerY-py)*follow;
   // Sample the softened path at a fixed cadence, independent of mouse event rate.
   const prev=trail[trail.length-1];
   if(targetPower&&(!prev||now-prev.t>=80)){
    trail.push({x:px,y:py,t:now});if(trail.length>12)trail.shift();
    canvas.dataset.trailPeak=String(Math.max(Number(canvas.dataset.trailPeak)||0,trail.length));
   }
  }
  const lifetime=closing?1100:550;
  packed.fill(0);trail=trail.filter(p=>now-p.t<lifetime);
  if(moving)for(let i=0;i<trail.length;i++){
   packed[i*3]=trail[i].x;packed[i*3+1]=trail[i].y;packed[i*3+2]=1-(now-trail[i].t)/lifetime;
  }
  gl.uniform2f(uniforms.uSize,width,height);gl.uniform2f(uniforms.uDrift,moving?dx:0,moving?dy:0);
  gl.uniform1f(uniforms.uClosing,closing?1:0);gl.uniform3f(uniforms.uPointer,px,py,moving?power:0);
  gl.uniform1f(uniforms.uBenefits,benefits?1:0);
  gl.uniform4fv(uniforms['uTextAreas[0]'],textAreas);
  gl.uniform2fv(uniforms['uTextMotion[0]'],textMotion);
  gl.uniform3f(uniforms.uOpening,openingX,openingY,moving?opening:-1);
  gl.uniform1f(uniforms.uTime,time);gl.uniform1f(uniforms.uDpr,dpr);gl.uniform1f(uniforms.uCell,cell);gl.uniform3fv(uniforms['uTrail[0]'],packed);
  gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT);gl.drawArrays(gl.POINTS,0,count);dirty=false;
  const state=moving?'running':'paused';if(canvas.dataset.state!==state)canvas.dataset.state=state;
  const lit=String(trail.length);if(canvas.dataset.trail!==lit)canvas.dataset.trail=lit;
 }
 function dispose(){disposed=true;events.abort();resize.disconnect();observe.disconnect();prefs.disconnect();textObserver.disconnect();gl.deleteBuffer(buffer);gl.deleteProgram(program);gl.deleteShader(vs);gl.deleteShader(fs);canvas.remove();}
 window.addEventListener('pagehide',event=>{if(!event.persisted)dispose();},{signal});
 window.addEventListener('pageshow',event=>{if(event.persisted){last=0;dirty=true;textDirty=true;size();}},{signal});size();
 const api={tick,dispose,setOpening:(time,x=.5,y=.43)=>{opening=time;openingX=x;openingY=y;dirty=true;}};
 if(benefits){window.tregBenefitParticles=api;}
 else if(closing){window.tregEndingParticles=api;hero.classList.add('has-particles');}
 else{window.tregParticles=api;mountField(document.querySelector('.ending-layer'),true);mountField(document.querySelector('.bens'),false,true);}
 return api;
}
// The dashboard's /search page mounts the same hero field on its own page area (and drives `tick`
// from its own frame loop), so the two first screens share one implementation.
window.tregMountField=mountField;
mountField(document.querySelector('.hero'));
})();
