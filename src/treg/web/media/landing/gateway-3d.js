import * as T from 'three';
import {RoomEnvironment} from 'three/addons/environments/RoomEnvironment.js';
import {FontLoader} from 'three/addons/loaders/FontLoader.js';
import {createGateway} from './gateway-model.js';
import {createIntro} from './gateway-intro.js';

export async function mountGateway(host,{studio=false}={}){
 let renderer;
 try{renderer=new T.WebGLRenderer({alpha:true,antialias:true,powerPreference:'low-power'});}catch(error){host.dataset.modelState='fallback';throw error;}
 renderer.setPixelRatio(Math.min(devicePixelRatio,studio?2:1.5));renderer.setClearColor(0x000000,0);renderer.toneMapping=T.ACESFilmicToneMapping;
 renderer.toneMappingExposure=1.1;renderer.domElement.className='gateway-webgl';renderer.domElement.tabIndex=0;renderer.domElement.setAttribute('role','img');renderer.domElement.setAttribute('aria-label','3D layered metal and glass gateway. Drag to rotate; arrow keys rotate; Home resets.');host.append(renderer.domElement);
 const scene=new T.Scene(),camera=new T.PerspectiveCamera(22,1,.1,80);camera.position.set(0,0,12.8);camera.lookAt(0,0,0);
 const pmrem=new T.PMREMGenerator(renderer),room=new RoomEnvironment();
 room.traverse(o=>{if(o.isLight)o.intensity*=.45;if(o.isMesh){const ms=Array.isArray(o.material)?o.material:[o.material];for(const m of ms)m.color?.multiplyScalar(.55);}});
 // Broad upper softbox reflection, baked once rather than added as a render pass.
 const lightPixels=new Uint8Array(128*32*4);
 for(let y=0;y<32;y++)for(let x=0;x<128;x++){
  const nx=x/127*2-1,ny=y/31*2-1,k=(y*128+x)*4;
  lightPixels[k]=lightPixels[k+1]=lightPixels[k+2]=255;
  // Feather the light source itself, leaving the polished alloy reflection sharp.
  lightPixels[k+3]=Math.round(255*Math.exp(-2*nx*nx-3*ny*ny)*Math.pow(1-nx*nx,2)*Math.pow(1-ny*ny,2));
 }
 const lightMap=new T.DataTexture(lightPixels,128,32);lightMap.needsUpdate=true;lightMap.magFilter=T.LinearFilter;lightMap.minFilter=T.LinearFilter;
 const softbox=new T.Mesh(new T.PlaneGeometry(11,3.4),new T.MeshBasicMaterial({color:new T.Color(7.2,7.4,7.1),map:lightMap,transparent:true,depthWrite:false,side:T.DoubleSide}));
 // Match the reflected view vector of the default face, not its apparent top.
 softbox.position.set(5,-1.3,3.5);softbox.lookAt(0,0,0);room.add(softbox);
 const environment=pmrem.fromScene(room,.04);scene.environment=environment.texture;room.dispose();lightMap.dispose();pmrem.dispose();
 scene.add(new T.HemisphereLight(0xffffff,0x737873,1.3));
 const key=new T.DirectionalLight(0xfffdf8,3);key.position.set(1,3,6);scene.add(key);
 const upper=new T.SpotLight(0xf3fff9,85,14,.29,1,2);upper.position.set(4,-.3,4.5);upper.target.position.set(0,.88,.66);scene.add(upper,upper.target);
 const rim=new T.DirectionalLight(0xc9f4e4,2.5);rim.position.set(4,1,-2);scene.add(rim);
 let font;try{font=new FontLoader().parse(await (await fetch(import.meta.resolve('three/fonts/helvetiker_regular.typeface.json'))).json());}catch{}
 const {root,materials}=createGateway(T,font);scene.add(root);
 const layers=root.children.filter(o=>o.name.startsWith('Glass cassette')).map((group,index)=>{
  group.traverse(o=>{if(o.isMesh){o.material=o.material.clone();o.userData.baseEmission=o.material.emissiveIntensity;o.material.emissive.set(0x64e5b8);}});
  return {group,index,amount:0,target:0};
 });
 let active=-1,cycleTime=0;
 function selectGlass(index){if(active===index)return;active=index;host.dataset.activeGlass=index<0?'none':String(index+1);dirty=true;}
 function advanceGlass(dt){
  if(!enabled()){layers.forEach(l=>{l.target=0;});selectGlass(-1);return;}
  // Overlapping smooth pulses: the next glass rises while its neighbour falls.
  const stagger=.22,duration=.72,period=layers.length*stagger;
  cycleTime=(cycleTime+dt)%period;
  for(const layer of layers){
   const phase=(cycleTime-(layers.length-1-layer.index)*stagger+period)%period;
   layer.target=phase<duration?Math.sin(Math.PI*phase/duration)**2:0;
  }
  selectGlass(layers.reduce((best,l)=>l.target>layers[best].target?l.index:best,0));
  dirty=true;
 }
 // Deterministic micro-grooves in the material, not an image of the object.
 const data=new Uint8Array(256*256*4);let seed=47;
 for(let y=0;y<256;y++){seed=(seed*1664525+1013904223)>>>0;const v=110+(seed%72);for(let x=0;x<256;x++){const k=(y*256+x)*4;data[k]=data[k+1]=data[k+2]=v;data[k+3]=255;}}
 const brush=new T.DataTexture(data,256,256);brush.wrapS=brush.wrapT=T.RepeatWrapping;brush.repeat.set(1,14);brush.needsUpdate=true;materials.metal.bumpMap=brush;materials.metal.bumpScale=.0025;materials.metal.needsUpdate=true;
 const reduced=matchMedia('(prefers-reduced-motion: reduce)'),coarse=matchMedia('(pointer: coarse)'),events=new AbortController(),signal=events.signal;
 const initialView={yaw:.52,pitch:.055};
 let visible=true,last=0,dirty=true,drag=false,startX=0,startY=0,yaw=initialView.yaw,pitch=initialView.pitch,targetYaw=yaw,targetPitch=pitch,hoverX=0,hoverY=0,spread=0,targetSpread=0,auto=false,disposed=false;
 root.rotation.set(pitch,yaw,0);
 const enabled=()=>!reduced.matches&&!document.documentElement.classList.contains('motion-off');
 function size(){const r=host.getBoundingClientRect();if(!r.width||!r.height)return;renderer.setSize(r.width,r.height,false);camera.aspect=r.width/r.height;camera.position.z=(studio?8.2:7.6)*1.475*Math.max(1,.9/camera.aspect);camera.updateProjectionMatrix();dirty=true;}
 const resize=new ResizeObserver(size);resize.observe(host);size();
 const intro=studio?null:createIntro(host,root,layers);
 function tick(now){
  if(disposed||!visible||document.hidden){last=0;return;}
  // Follow every shared animation frame while settling, including after release.
  // Input must not render faster than its damping tail. Idle still exits below.
  const dt=last?Math.min((now-last)/1000,.06):0;last=now;
  advanceGlass(dt);
  if(intro?.update(dt,enabled()))dirty=true;
  if(auto&&enabled())targetYaw+=dt*.22;
  const wantedY=targetYaw+(enabled()?hoverX:0),wantedX=targetPitch+(enabled()?hoverY:0);
  const unsettled=Math.abs(yaw-wantedY)+Math.abs(pitch-wantedX)+Math.abs(spread-targetSpread)>.0001||layers.some(l=>Math.abs(l.amount-l.target)>.001);
  if(!dirty&&!unsettled&&!(auto&&enabled()))return;
  const ease=enabled()?1-Math.exp(-dt*8):1;yaw+=(wantedY-yaw)*ease;pitch+=(wantedX-pitch)*ease;spread+=(targetSpread-spread)*ease;
  root.rotation.set(pitch,yaw,0);for(const part of root.children)part.position.z=part.userData.baseZ*(1+spread);
  for(const layer of layers){
   layer.amount+=(layer.target-layer.amount)*(enabled()?1-Math.exp(-dt*16):1);
   layer.group.position.y=enabled()?layer.amount*.18:0;
   layer.group.traverse(o=>{if(!o.isMesh)return;const glass=o.name==='Jade optical frame';
    o.material.emissiveIntensity=(o.name==='Polished outer rim'?0:glass?0:o.userData.baseEmission)+layer.amount*(glass?1.35:4.8);
   });
  }
  const lift=String(Math.round(Math.max(...layers.map(l=>l.amount))*100));
  if(host.dataset.glassLift!==lift)host.dataset.glassLift=lift;
  intro?.apply();renderer.render(scene,camera);dirty=false;
 }
 const canvas=renderer.domElement;
 canvas.addEventListener('pointerdown',e=>{if(e.button!==0||host.dataset.intro==='running')return;dirty=true;drag=true;startX=e.clientX;startY=e.clientY;canvas.setPointerCapture(e.pointerId);},{signal});
 canvas.addEventListener('pointermove',e=>{if(drag){const dx=e.clientX-startX,dy=e.clientY-startY;targetYaw+=dx*.007;targetPitch=T.MathUtils.clamp(targetPitch+dy*.005,-.85,.85);startX=e.clientX;startY=e.clientY;dirty=true;}},{signal});
 const interactionArea=host.closest('.hero')||host;
 interactionArea.addEventListener('pointermove',e=>{
  if(drag||coarse.matches)return;
  const r=host.getBoundingClientRect(),dx=(e.clientX-r.left-r.width/2)/r.width,dy=(e.clientY-r.top-r.height/2)/r.height;
  const strength=.14+.86*Math.exp(-(dx*dx+dy*dy)*1.3);
  hoverX=enabled()?T.MathUtils.clamp(dx,-1,1)*strength*.48:0;
  hoverY=enabled()?T.MathUtils.clamp(dy,-1,1)*strength*.27:0;
 },{signal});
 function clearHover(){hoverX=hoverY=0;dirty=true;}
 interactionArea.addEventListener('pointerleave',clearHover,{signal});
 window.addEventListener('blur',clearHover,{signal});
 function release(e){drag=false;if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId);}
 canvas.addEventListener('pointerup',release,{signal});canvas.addEventListener('pointercancel',e=>{release(e);clearHover();},{signal});
 function reset(){if(host.dataset.intro==='running')return;targetYaw=initialView.yaw;targetPitch=initialView.pitch;clearHover();cycleTime=0;targetSpread=0;auto=false;dirty=true;}
 canvas.addEventListener('dblclick',reset,{signal});
 canvas.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home'].includes(e.key))return;e.preventDefault();if(e.key==='Home')reset();else{targetYaw+=(e.key==='ArrowRight'?.12:e.key==='ArrowLeft'?-.12:0);targetPitch=T.MathUtils.clamp(targetPitch+(e.key==='ArrowDown'?.1:e.key==='ArrowUp'?-.1:0),-.85,.85);dirty=true;}},{signal});
 canvas.setAttribute('aria-label','Interactive 3D gateway. Six glass layers automatically rise and illuminate in sequence. Drag or use arrow keys to rotate; Home resets.');
 canvas.addEventListener('blur',clearHover,{signal});
 const observe=new IntersectionObserver(es=>{visible=es[0].isIntersecting;dirty=true;last=0;});observe.observe(host);
 let motionEnabled=enabled();
 const prefs=new MutationObserver(()=>{const next=enabled();if(next!==motionEnabled){motionEnabled=next;clearHover();}});prefs.observe(document.documentElement,{attributes:true,attributeFilter:['class']});reduced.addEventListener('change',clearHover,{signal});
 canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();host.classList.remove('model-ready');host.dataset.modelState='context-lost';},{signal});canvas.addEventListener('webglcontextrestored',()=>{dirty=true;host.classList.add('model-ready');host.dataset.modelState='ready';},{signal});
 intro?.apply();renderer.render(scene,camera);host.classList.add('model-ready');host.dataset.modelState='ready';host.dataset.modelMeshes=String(countMeshes(root));
 function dispose(){disposed=true;intro?.stop();events.abort();resize.disconnect();observe.disconnect();prefs.disconnect();const gs=new Set(),ms=new Set();root.traverse(o=>{if(o.geometry)gs.add(o.geometry);if(o.material)(Array.isArray(o.material)?o.material:[o.material]).forEach(m=>ms.add(m));});gs.forEach(g=>g.dispose());ms.forEach(m=>m.dispose());brush.dispose();environment.dispose();renderer.dispose();canvas.remove();host.classList.remove('model-ready');}
 window.addEventListener('pagehide',event=>{if(!event.persisted)dispose();},{signal});
 window.addEventListener('pageshow',event=>{if(event.persisted){last=0;dirty=true;size();}},{signal});
 return {tick,reset,setSpread:v=>{targetSpread=v;dirty=true;},setAuto:v=>{auto=v;dirty=true;},dispose};
}
function countMeshes(root){let n=0;root.traverse(o=>{if(o.isMesh)n++;});return n;}
const landing=document.querySelector('.gateway-sculpture');
if(landing)mountGateway(landing).then(api=>{window.tregModel=api;}).catch(error=>{landing.dataset.modelState='fallback'; window.tregOpening?.release(); console.warn('3D gateway unavailable; showing the treg mark.',error);});
