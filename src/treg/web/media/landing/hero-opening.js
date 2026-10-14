/* Before-paint guard with a failure timeout and immediate skip. */
(()=>{
 if(!matchMedia('(min-width:901px) and (pointer:fine) and (prefers-reduced-motion:no-preference)').matches||location.hash&&location.hash!=='#top')return;
 const root=document.documentElement;root.classList.add('opening-pending','opening-stage');
 const events=new AbortController();let timer;
 function release(){clearTimeout(timer);root.classList.remove('opening-pending','opening-stage');events.abort();window.dispatchEvent(new Event('treg:skip-opening'));}
 window.tregOpening={release};timer=setTimeout(release,10000);
 window.addEventListener('wheel',release,{once:true,passive:true,signal:events.signal});
 window.addEventListener('keydown',release,{once:true,signal:events.signal});
})();
