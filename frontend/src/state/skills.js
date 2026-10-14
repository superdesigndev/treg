
export default {
openRecipeCopy(r){ this.copyRecipe=r; this.recipeTab='cURL'; this.copied=false; },
async openRecipeView(r){ this.err='';
      try{ const b=await this.api('/bundles/'+r.id); const rec=b.recipe||''; this.viewRecipe={id:r.id, name:r.name, recipe:rec, orig:rec}; this.copied=false; this.recipeSaved=false; }
      catch(e){ this.err='Could not load recipe: '+(e.detail||e.status); } },
async saveRecipe(){ if(!this.viewRecipe) return; this.err='';
      try{ await this.api('/bundles/'+this.viewRecipe.id,{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({recipe:this.viewRecipe.recipe})});
        this.viewRecipe.orig=this.viewRecipe.recipe; this.recipeSaved=true; setTimeout(()=>{ this.recipeSaved=false; },1400); await this.loadAll(); }
      catch(e){ this.err='Save failed: '+(e.detail||e.status); } },
recipeSnippet(tab){ const r=this.copyRecipe; if(!r) return {text:'',html:''};
      const proxy=(this.proxy||location.origin).replace(/\/$/,'');
      const tok=this.myToken||'$TREG_TOKEN', tokS=tok;  // full token in the preview — matches the copied text
      const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
      const P=s=>`<span class="s-proxy">${esc(s)}</span>`, A=s=>`<span class="s-path">${esc(s)}</span>`,
            T=s=>`<span class="s-token">${esc(s)}</span>`, C=s=>`<span class="s-cmt">${esc(s)}</span>`, K=s=>`<span class="s-kw">${esc(s)}</span>`;
      switch(tab){
        case 'cURL': return {
          text:`TREG_API_TOKEN=${tok}\ncurl -fsSL "${proxy}/skills/${r.name}/install.sh?token=$TREG_API_TOKEN" | sh`,
          html:`TREG_API_TOKEN=${T(tokS)}\n${K('curl')} -fsSL "${P(proxy)}${C('/skills/')}${A(r.name)}${C('/install.sh?token=')}${T('$TREG_API_TOKEN')}" | sh` };
        case 'CLI': return { text:`treg skill install ${r.name}`, html:`${P('treg skill install')} ${A(r.name)}` };
        case 'Claude Code': return {
          text:`# Install once - Claude Code then loads it from .claude/skills/ automatically.\ntreg skill install ${r.name}\n# ...then just ask Claude to use the "${r.name}" skill.`,
          html:`${C('# Install once - Claude Code then loads it from .claude/skills/ automatically.')}\n${P('treg skill install')} ${A(r.name)}\n${C('# ...then just ask Claude to use the "'+r.name+'" skill.')}` };
      } },
openAddSkill(){ this.skillErr=''; this.skillMode='folder'; this.skillFiles=[]; this.detected=null;
      this.skillSel={}; this.skillVals={}; this.skillResults=null; this.newSkill=true; this._seedSkillJson(); },
_seedSkillJson(){ this.skillJson = JSON.stringify({
        name:"my-skill", recipe:"# my-skill\nWhat it does.",
        secrets:[{local_name:"key", value:"sk-…", kind:"env"}],
        tools:[{name:"my-tool", base_url:"https://api.example.com",
          bindings:[{secret:"key", injector:"env", location:"header", name:"Authorization", format:"Bearer {secret}"}]}]
      }, null, 2); this.newSkill=true; },
async addSkill(){ let payload; try{ payload=JSON.parse(this.skillJson); }catch(e){ this.skillErr='Not valid JSON: '+e.message; return; }
      // Structural validation (the tour promises the JSON is checked before it's sent): a name, at
      // least one well-formed tool, and every binding.secret must match a declared local_name.
      if(!payload || !payload.name){ this.skillErr='A skill needs a "name".'; return; }
      if(!Array.isArray(payload.tools) || !payload.tools.length){ this.skillErr='A skill needs at least one tool.'; return; }
      const locals=new Set((payload.secrets||[]).map(s=>s.local_name));
      for(const t of payload.tools){
        if(!t.name || !t.base_url){ this.skillErr='Each tool needs a name and base_url.'; return; }
        for(const b of (t.bindings||[])){
          if(b.secret!=null && !locals.has(b.secret)){ this.skillErr='Binding references unknown secret "'+b.secret+'" - declare it in secrets[].local_name.'; return; }
        }
      }
      this.skillBusy=true; this.skillErr='';
      try{ await this.api('/skills',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});
        this.newSkill=false; await this.loadAll(); }
      catch(e){ this.skillErr='Register failed: '+(e.detail||e.status); } finally{ this.skillBusy=false; } },
async onSkillFolder(e){ this.skillErr=''; this.detected=null; this.skillResults=null;
      const want=p=>/(^|\/)(SKILL\.md|skill\.md|treg\.json)$/i.test(p) || /\.(sh|js|mjs|ts|py)$/i.test(p)
        || /(^|\/)\.env$/i.test(p) || /(^|\/)\.secrets?\//i.test(p) || /(^|\/)(client_secret|token|developer_token)/i.test(p);
      const picked=[...e.target.files].filter(f=>want(f.webkitRelativePath) && f.size<524288).slice(0,600);
      if(!picked.length){ this.skillErr='No skill files here - a skill folder needs a SKILL.md.'; return; }
      this.skillFiles=await Promise.all(picked.map(async f=>({path:f.webkitRelativePath, content:await f.text()})));
      await this.analyzeSkills(); },
async analyzeSkills(){ if(!this.skillFiles.length) return;
      const payload=JSON.stringify({files:this.skillFiles});
      if(payload.length>8*1048576){ this.skillErr='That folder is large ('+Math.round(payload.length/1048576)+' MB, '+this.skillFiles.length+' files). Pick a single skill folder or a smaller subfolder.'; return; }
      this.skillBusy=true; this.skillErr='';
      try{ const r=await this.api('/skills/analyze',{method:'POST',headers:{'content-type':'application/json'},body:payload,encode:true});
        this.detected=r.skills; this.skillSel={}; this.skillVals={};
        for(const s of r.skills){ this.skillSel[s.name]=s.ready && !s.already; }
        if(!r.skills.length) this.skillErr='No SKILL.md found in that folder.'; }
      catch(e){ this.skillErr='Analyze failed: '+(e.detail||e.message||(e.status?('HTTP '+e.status):'request failed - try a single skill or a smaller subfolder')); } finally{ this.skillBusy=false; } },
skillCliNote(s){ const c=s.cli; if(!c) return '';
      if(c.source==='unsupported') return '⌘ '+c.bin+' CLI: not supported for local runs - '+c.reason;
      if(c.source==='contract') return '⌘ local runs: members can `treg run '+s.name+'`'+(c.enabled?'':' (enable after import)');
      return '⌘ '+c.bin+' CLI: local runs available via `treg run` once an owner enables it'+(c.verified?' (verified)':''); },
skillSummary(s){ return s.kind==='recipe_only' ? 'Recipe-only - installs the SKILL.md as a shareable recipe.'
        : 'API tool - registers a tool calling '+(s.base_url||'?')+' + the recipe.'; },
async importSkills(){ const select=Object.keys(this.skillSel).filter(n=>this.skillSel[n]);
      if(!select.length){ this.skillErr='Select at least one skill to register.'; return; }
      this.skillBusy=true; this.skillErr='';
      try{ const r=await this.api('/skills/import',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({files:this.skillFiles, select, env_values:this.skillVals}),encode:true});
        this.skillResults=r.results; await this.loadAll();
        if(r.results.length && r.results.every(x=>x.ok)){ setTimeout(()=>{ this.newSkill=false; }, 1000); } }
      catch(e){ this.skillErr='Import failed: '+(e.detail||e.message||(e.status?('HTTP '+e.status):'request failed - try registering fewer skills at once')); } finally{ this.skillBusy=false; } }
}
