
export default {
defaultBinding(){ return {secret_id:(this.secrets[0]?this.secrets[0].id:null), injector:'env', location:'header', name:'Authorization', format:'Bearer {secret}', secret_field:'access_token'}; },
async loadProjectsIfNeeded(){ if(this.projects.length || !this.activeOrgId || !this.canAdmin) return;
      this.projects = await this.api('/orgs/'+this.activeOrgId+'/projects').catch(()=>[]); },
openAddTool(mode){ this.addToolMenu=false; this.loadProjectsIfNeeded(); this.loadSecrets().then(()=>{
      this.tForm = mode==='cli'
        ? {id:null, mode:'cli', name:'', base_url:'', bindings:[],
           cli:{bin:'', package:'', enabled:true, auth_mechanism:'env', deny:[], deny_defaults:true,
                inject:[{via:'env', name:'', secret_id:(this.secrets[0]?this.secrets[0].id:null)}]}}
        : {id:null, mode:'endpoint', name:'', base_url:'', bindings:[this.defaultBinding()], project:null};
      if(mode==='cli') this.tForm.project=null;
      if(mode==='cli') this.loadCatalogClis();
      this.toolErr=''; this.newTool=true; }); },
openEditTool(t){ this.loadProjectsIfNeeded(); this.loadSecrets().then(()=>{ const cur=(this.projects||[]).find(p=>p.id===t.project_id);
      this.tForm={ id:t.id, mode:(t.cli?'cli':'endpoint'), name:t.name, base_url:t.base_url, project:(cur?cur.slug:null),
        bindings:(t.bindings||[]).map(b=>({secret_id:b.secret_id, injector:b.injector||'env', location:b.location||'header', name:b.name||'Authorization', format:b.format||'Bearer {secret}', secret_field:b.secret_field||'access_token'})),
        // deep-copy the cli profile — the PATCH replaces it wholesale, so every field must round-trip
        cli: t.cli ? Object.assign(JSON.parse(JSON.stringify(t.cli)), {package:t.cli.package||'', deny:(t.cli.deny||[]).slice(), deny_defaults:t.cli.deny_defaults!==false,
                inject:(t.cli.inject||[]).map(e=>Object.assign({via:'env',name:'',secret_id:null},e))}) : null };
      if(this.tForm.cli){ if(!this.tForm.cli.inject.length) this.tForm.cli.inject=[{via:'env', name:'', secret_id:null}]; this.loadCatalogClis(); }
      else if(!this.tForm.bindings.length) this.tForm.bindings=[this.defaultBinding()];
      this.toolErr=''; this.newTool=true; }); },
async loadCatalogClis(){ if(this.catalogClis) return;  // bin → catalog deny patterns, fetched once
      try{ const cat=await (await fetch('/providers.json')).json();
        const m={}; (cat.providers||cat||[]).forEach(e=>{ const c=e.cli; if(c&&c.bin) m[c.bin]=(c.deny||[]); }); this.catalogClis=m; }
      catch(e){ this.catalogClis={}; } },
addBinding(){ const b=this.defaultBinding();
      // avoid cloning a duplicate header name (which would fail on save) - pick a fresh one
      const used=new Set(this.tForm.bindings.filter(x=>(x.location||'header')==='header').map(x=>(x.name||'Authorization').toLowerCase()));
      if(used.has('authorization')){ b.name='X-Api-Key'; b.format='{secret}'; }
      this.tForm.bindings.push(b); },
removeBinding(i){ this.tForm.bindings.splice(i,1); },
async saveTool(){ const f=this.tForm;
      if(!/^https?:\/\/.+/i.test(f.base_url.trim())){ this.toolErr=(f.mode==='cli'?'The provider API base URL':'Base URL')+' must start with http:// or https://'; return; }  // client-side URL check
      let body;
      if(f.mode==='cli'){
        const bin=(f.cli.bin||'').trim();
        if(!f.id && !bin){ this.toolErr='The CLI command (e.g. gh) is required.'; return; }
        const inject=f.cli.inject.filter(e=>(e.name||'').trim()||e.secret_id).map(e=>({via:'env', name:(e.name||'').trim(), secret_id:e.secret_id}));
        if(inject.some(e=>!e.name||!e.secret_id)){ this.toolErr='Each binding key needs a secret AND the env var to inject it as.'; return; }
        const deny=f.cli.deny.map(p=>p.trim()).filter(Boolean);
        for(const p of deny){ try{ new RegExp(p); }catch(e){ this.toolErr='Deny pattern is not a valid regex: '+p; return; } }
        const cli=Object.assign({},f.cli,{bin, inject, deny, deny_defaults:!!f.cli.deny_defaults, enabled:!!f.cli.enabled});
        if(!(cli.package||'').trim()) delete cli.package; else cli.package=cli.package.trim();
        // PATCH omits `bindings` on purpose: editing the CLI must not clobber any HTTP bindings the tool also has
        body = f.id ? {base_url:f.base_url.trim(), cli, project:f.project||null} : {name:bin.replace(/_/g,'-'), base_url:f.base_url.trim(), bindings:[], cli, project:f.project||null};
      } else {
        if(!f.id && !f.name.trim()){ this.toolErr='Name and base URL are required.'; return; }
        if(!f.bindings.length || f.bindings.some(b=>!b.secret_id)){ this.toolErr='Every binding needs a secret.'; return; }
        const hdr=f.bindings.filter(b=>(b.location||'header')==='header').map(b=>(b.name||'Authorization').toLowerCase());
        if(new Set(hdr).size!==hdr.length){ this.toolErr='Two bindings target the same header - give each a distinct name.'; return; }  // catch the collision inline, clearly
        const bindings=f.bindings.map(b=>({secret_id:b.secret_id, injector:b.injector, location:b.location, name:b.name, format:b.format, secret_field:b.secret_field}));
        body = f.id ? {base_url:f.base_url.trim(), bindings, project:f.project||null} : {name:f.name.trim(), base_url:f.base_url.trim(), bindings, project:f.project||null};
      }
      this.toolBusy=true; this.toolErr='';
      try{
        if(f.id){ await this.api('/tools/'+f.id,{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify(body)}); }
        else { await this.api('/tools',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}); }
        const created = f.id ? null : body.name;
        this.newTool=false; await this.loadAll();
        if(this.view==='detail') this.loadDetail();  // a detail-page ⚙ Configure save must show the new values
        if(created) await this.remindCustomizedAccess(created); }
      catch(e){ this.toolErr='Save tool failed: '+(e.detail||e.status); } finally{ this.toolBusy=false; } },
async remindCustomizedAccess(toolName){  // a NEW tool auto-applies to 'all-tools' members but NOT to customized ones
      try{ const members=await this.api('/orgs/'+this.activeOrgId+'/members');
        const n=(members||[]).filter(m=>m.tool_access!==null && m.role!=='owner').length;
        if(n>0) this.accessNote='New tool "'+toolName+'" is available to members who have access to all tools. '+n+' member(s) have a customized selection and won\'t see it until you add it (Team → their Tools).';
      }catch(e){/* non-admin creator can't list members → no reminder */} },
localRunTitle(t){ const bin=(t.cli&&t.cli.bin)||t.name; return t.cli&&t.cli.enabled ? ('Local runs ON - members can: treg run '+t.name+'. Click to disable.') : ('Local runs OFF - click to let members run '+bin+' locally with this key injected.'); },
async toggleLocalRun(t){ if(!t.cli) return; const turningOn=!t.cli.enabled; const cli=Object.assign({},t.cli,{enabled:turningOn});
      try{ await this.api('/tools/'+t.id,{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({cli})}); await this.loadAll();
        this.runNote = turningOn ? t.name : ''; }
      catch(e){ this.err='Could not change local-run: '+(e.detail||e.status); } },
canCall(t){ return !t.cli || (t.bindings&&t.bindings.length); },
// an HTTP verb needs a binding (or it's a plain HTTP tool)
    canRun(t){ return !!t.server_runnable; },
// server-computed: cli profile + allow-listed bin
    credChips(t){  // one chip per credential KIND (env/oauth/…), covering HTTP bindings + cli.inject alike
      const kinds=new Map();
      (t.bindings||[]).forEach(b=>{ const k=b.injector||'env'; kinds.set(k,(kinds.get(k)||0)+1); });
      ((t.cli&&t.cli.inject)||[]).forEach(()=>{ kinds.set('env',(kinds.get('env')||0)+1); });
      return [...kinds.keys()].map(k=>({label:k, kind:k, title:'credential injected by the registry ('+kinds.get(k)+' wire'+(kinds.get(k)>1?'s':'')+': proxy call and/or CLI run) - the value never appears here'}));
    },
openUse(t){ this.tryTool=t; this.useMode=this.canRun(t)?'run':(this.canCall(t)?'call':'run'); this.runOut=null;  // last resort: run mode, whose server 422 explains itself (never an unauthenticated HTTP call)
      const ex=(t.examples||[])[0], hp=t.health_check&&t.health_check.path;  // prefill a real, runnable path so Send just works
      this.tryPath=ex?ex.path:(hp||this.samplePath(t.host)||''); this.tryMethod=ex?(ex.method||'GET'):'GET'; this.tryResp=null; this.tryBody=''; },
async doRun(){ if(this.running || !this.tryTool) return; this.running=true; this.runOut=null;
      // shell-ish tokenizer so quoted arguments ("a b") survive as one argv entry
      const args=(this.runArgsStr.match(/"([^"]*)"|'([^']*)'|\S+/g)||[]).map(a=>a.replace(/^["']|["']$/g,''));
      try{ this.runOut=await this.api('/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({tool:this.tryTool.name,args,timeout_s:60}),encode:true}); }
      catch(e){ this.runOut={exit_code:-1, duration_ms:0, detail:(e&&e.detail)||('request failed: '+(e&&e.status||e))}; }
      finally{ this.running=false; } },
async deleteTool(t){ if(this.confirmDelTool!==t.id){ this.confirmDelTool=t.id; return; } this.confirmDelTool=null; this.err=''; this.toolErr='';
      try{ await this.api('/tools/'+t.id,{method:'DELETE'}); await this.loadAll(); }
      catch(e){ this.err='Delete tool failed: '+(e.detail||e.status); } },
async deleteRecipe(r){ if(this.confirmDelBundle!==r.id){ this.confirmDelBundle=r.id; return; } this.confirmDelBundle=null; this.err='';
      try{ await this.api('/bundles/'+r.id,{method:'DELETE'}); await this.loadAll(); }
      catch(e){ this.err='Delete recipe failed: '+(e.detail||e.status); } }
}
