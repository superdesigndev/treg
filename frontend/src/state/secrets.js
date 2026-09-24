
export default {
// ---- Phase 2b: resource registration (secrets + tools) ----
    async loadSecrets(){ try{ this.secrets=await this.api('/secrets'); }catch(e){ this.secrets=[]; } },
// Paste a whole .env into the name/value field (Render/Vercel-style): it splits into rows
    // client-side — comments/blank lines skipped, `export ` stripped, one balanced quote pair removed.
    parseEnvText(t){ const out=[];
      for(let line of t.split(/\r?\n/)){ line=line.trim();
        if(!line||line.startsWith('#')||!line.includes('=')) continue;
        let k=line.slice(0,line.indexOf('=')).trim().replace(/^export\s+/i,''), v=line.slice(line.indexOf('=')+1).trim();
        if(v.length>=2&&v[0]===v[v.length-1]&&(v[0]==='"'||v[0]==="'")) v=v.slice(1,-1);
        if(k) out.push({name:k,value:v,kind:'env'}); }
      return out; },
// The nudge under a secret row: an exact provider name gets a confirmation, a near miss
    // (APOLLO_API_KEY, tikhub-key, DATAFORSEO_TOKEN) gets the exact name the ladder matches on.
    // Anything else — a genuinely custom secret — gets silence, not a warning: most secrets are
    // for the org's own tools and have every right to any name.
    secretNameHint(row){
      const raw=(row.name||'').trim(); if(!raw) return null;
      const provs=this.keyNameSuggestions;
      const exact=provs.find(p=>p.service===raw);  // the ladder compares exactly — "Apollo" is a near miss, not a hit
      if(exact) return {ok:true, service:exact.service,
        text:(exact.display_name||exact.service)+' catalog calls will use this key automatically'};
      const norm=raw.toLowerCase().replace(/[^a-z0-9]+/g,'');
      const near=provs.find(p=>{ const s=p.service.replace(/[^a-z0-9]+/g,'');
        return norm===s+'apikey'||norm===s+'key'||norm===s+'token'||norm===s+'api'||norm===s; });
      if(near) return {ok:false, service:near.service,
        text:(near.display_name||near.service)+' catalog calls only find a key named exactly “'+near.service+'” —'};
      return null;
    },
pasteEnv(e,i,field){ const t=(e.clipboardData||window.clipboardData).getData('text')||'';
      const rows=this.parseEnvText(t);
      if(!rows.length) return;                       // not env-shaped → normal paste
      // Single-line pastes: only a NAME=value into the *name* field splits; into the value field it's
      // a secret that happens to contain '=' (base64 pad, connection strings) → normal paste.
      if(!t.includes('\n')&&(field!=='name'||t.indexOf('=')<1)) return;
      e.preventDefault();
      const seen=new Set(this.secretRows.filter((r,j)=>j!==i&&r.name).map(r=>r.name));
      const fresh=rows.filter(r=>!seen.has(r.name));
      this.secretRows.splice(i,1,...(fresh.length?fresh:[{name:'',value:'',kind:'env'}])); },
async addSecrets(){ const rows=this.secretRows.filter(r=>(r.name||'').trim()&&r.value);
      if(!rows.length){ this.secretErr='A secret needs both a name and a value.'; return; }
      this.secretBusy=true; this.secretErr='';
      const errs=[];
      for(const r of rows){ const n=r.name.trim();
        if(this.secrets.some(s=>s.name===n)){ errs.push(n+': already exists'); continue; }  // dup-name guard
        try{ await this.api('/secrets',{method:'POST',encode:true,headers:{'content-type':'application/json'},body:JSON.stringify({name:n,value:r.value,kind:r.kind})}); }
        catch(e){ errs.push(n+': '+(e.detail||e.status)); } }
      this.secretRows=[{name:'',value:'',kind:'env'}]; await this.loadSecrets();
      if(errs.length) this.secretErr='Some secrets failed — '+errs.join(' · ');
      this.secretBusy=false; },
async deleteSecret(s){ if(this.confirmDelSecret!==s.id){ this.confirmDelSecret=s.id; return; } this.confirmDelSecret=null; this.secretErr='';
      try{ await this.api('/secrets/'+s.id,{method:'DELETE'}); await this.loadSecrets(); }
      catch(e){ this.secretErr='Delete secret failed: '+(e.detail||e.status); } }
}
