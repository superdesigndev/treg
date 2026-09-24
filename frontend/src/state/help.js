
export default {
async resetDemo(){ try{ await this.api('/onboard/reset',{method:'POST'}); this.onboarded=true; await this.loadAll(); this.orgMsg='Demo teammates removed.'; }
      catch(e){ this.err='Reset failed: '+(e.detail||e.status); } },
readMore(section){ try{ window.open('/tutorial'+(section?('#'+section):''),'_blank'); }catch(e){} },
tutGo(i){ this.tut.i=Math.max(0,Math.min(this.tutSteps.length-1,i)); this.tutCopied=false; },
xtutGo(i){ this.xtut.i=Math.max(0,Math.min(this.xtutSteps.length-1,i)); this.tutCopied=false; },
tutHL(text,kind){ return (window.tregHL?window.tregHL(text, window.tregLang(text,kind)):text); },
async tutCopy(text){ if(!(await this.toClipboard(text))) return; this.tutCopied=true; setTimeout(()=>this.tutCopied=false,1500); },
async toClipboard(text){  // guarded copy with a legacy fallback - navigator.clipboard is undefined on non-secure contexts
      try{ if(navigator.clipboard){ await navigator.clipboard.writeText(text); return true; } }catch(e){}
      try{ const ta=document.createElement('textarea'); ta.value=text; ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta); ta.select(); const ok=document.execCommand('copy'); document.body.removeChild(ta); return ok; }catch(e){ return false; } },
personaLabel(who){ return ((this.tutData.personas||{})[who]||{}).label || who; },
personaTour(who){ return (this.tourData.personas||{})[who] || who; },
tourMatColor(part){ const c=this.tourData.colors; return `var(${c[this.tourParts.indexOf(part)%c.length]})`; },
when(iso){ try{ const d=new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(iso)?iso:iso+'Z'); const s=(Date.now()-d)/1000; if(s<90)return 'just now'; if(s<5400)return Math.round(s/60)+'m ago'; if(s<172800)return Math.round(s/3600)+'h ago'; return d.toISOString().slice(0,10);}catch(e){return iso;} },
until(iso){ try{ const d=new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(iso)?iso:iso+'Z'); const s=(d-Date.now())/1000; if(s<=0)return 'expired'; if(s<5400)return 'in '+Math.round(s/60)+'m'; if(s<172800)return 'in '+Math.round(s/3600)+'h'; return 'in '+Math.round(s/86400)+'d';}catch(e){return iso;} },
// future countdown - when() is an "ago" formatter and mislabels a future expiry as "just now"
    toolStatus(t){ const ids=(t.bindings||[]).map(b=>b.secret_id).filter(x=>x!=null); if(!ids.length) return '-';
      const st=ids.map(i=>this.health[i]||'unknown'); if(st.includes('invalid'))return 'invalid'; if(st.every(s=>s==='ok'))return 'ok'; return 'unknown'; }
}
