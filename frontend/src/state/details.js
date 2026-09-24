
export default {
openDetail(kind, name, fromPop){ this.resetConfirms();
      this.detail={kind, name}; this.view='detail'; this.detailFile='SKILL.md'; this.detailCopied=''; this.detailNote='';
      if(!fromPop) history.pushState({detail:{kind,name}}, '', '/app/'+(kind==='skill'?'skills':'tools')+'/'+encodeURIComponent(name));
      this.loadDetail(); },
async loadDetail(){ if(!this.detail) return; this.detailErr=''; this.detailLoading=true; this.detailData=null;
      try{ this.detailData=await this.api((this.detail.kind==='skill'?'/bundles/by-name/':'/tools/by-name/')+encodeURIComponent(this.detail.name)); }
      catch(e){
        if(e.status===404 && await this.findDetailOrg()) return void (this.detailLoading=false);  // it lives in another of my teams — switched
        this.detailErr = e.status===404
          ? 'Not found in your teams. This link needs an invite — ask the person who shared it to invite you (Share… on their side), then click the link again.'
          : 'Could not load: '+(e.detail||e.status); }
      finally{ this.detailLoading=false; } },
async fullTool(t){  // skill pages carry tool SUMMARIES — resolve the full record (bindings + cli) before acting on it
      let full=(this.tools||[]).find(x=>x.id===t.id);
      if(!full && !(t.bindings||t.cli)){ try{ full=(await this.api('/tools')).find(x=>x.id===t.id); }catch(e){} }
      return full||t; },
async configureTool(t){ this.cfgMenu=false; this.openEditTool(await this.fullTool(t)); },
async tryDetailTool(t){ this.tryMenu=false; this.openUse(await this.fullTool(t)); },
async copyDetail(text, tag){ if(!(await this.toClipboard(text))) return; this.detailCopied=tag; setTimeout(()=>{ if(this.detailCopied===tag) this.detailCopied=''; },1500); },
rowTarget(t){  // where a tools-list row leads: a skill-born tool opens its SKILL (the shareable thing), a bare endpoint opens the tool page
      if(t.bundle_id){ const b=this.bundles.find(x=>x.id===t.bundle_id); if(b) return {kind:'skill', name:b.name}; }
      return {kind:'tool', name:t.name}; },
rowHref(t){ const d=this.rowTarget(t); return '/app/'+(d.kind==='skill'?'skills':'tools')+'/'+encodeURIComponent(d.name); },
rowOpen(t){ const d=this.rowTarget(t); this.openDetail(d.kind, d.name); },
async autoAcceptShare(route){  // a share-link invite: clicking the emailed "Sign in & accept" IS the consent — accept silently, enter that team
      // Match by the email link's org param, or — for a DM'd bare link — by an invite whose landing IS this page.
      const path=route?'/app/'+(route.kind==='skill'?'skills':'tools')+'/'+encodeURIComponent(route.name):null;
      const inv=this.pendingInvites.find(i=>i.org_id===this.inviteLinkOrg)
              ||(path&&this.pendingInvites.find(i=>i.landing===path));
      if(!inv) return false;
      try{
        const r=await this.api('/invites/'+inv.id+'/accept',{method:'POST'});
        this.pendingInvites=this.pendingInvites.filter(i=>i.id!==inv.id);
        this.onboarded=true; try{ await this.api('/onboard/skip',{method:'POST'}); }catch(e){}
        if(r&&r.org){ this.activeSlug=r.org; localStorage.setItem('treg-active',r.org); }
        await this.loadAll();
        this.inviteLinkOrg=null;
        this.orgMsg='You joined '+((r&&r.name)||inv.name)+' — this page was shared with you.';
        return true;
      }catch(e){ return false; }  // revoked/used → the detail page's not-found message explains
    },
async findDetailOrg(){  // the link may belong to another of MY teams — probe them, switch silently on a unique hit
      if(!this.detail) return false;
      const others=(this.myOrgs||[]).filter(o=>o.slug!==this.activeSlugNow);
      const path=(this.detail.kind==='skill'?'/bundles/by-name/':'/tools/by-name/')+encodeURIComponent(this.detail.name);
      for(const o of others){
        let h;
        if(this.sessionMode) h={'X-Treg-Org':o.slug};
        else { const t=(this.cfg.orgs[o.slug]||{}).token; if(!t) continue; h={'X-Treg-Token':t}; }
        try{
          const r=await fetch(path,{credentials:'include',headers:{'ngrok-skip-browser-warning':'1',...h}});
          if(!r.ok) continue;
          const data=await r.json();
          if(this.sessionMode){ this.activeSlug=o.slug; localStorage.setItem('treg-active',o.slug); }
          else { this.cfg.active=o.slug; this.save(); }
          this.detailData=data; this.detailErr='';
          this.orgMsg='Switched to '+(o.name||o.slug)+' — this '+this.detail.kind+' lives there.';
          await this.loadAll();
          return true;
        }catch(_){}
      }
      return false;
    },
openShare(){
      this.share={on:true, email:'', role:'viewer', full:true, busy:false, err:'', sent:null, member:null};
      if(this.canAdmin) this.loadOrgAdmin().catch(()=>{}); },
async sendShare(){ const email=(this.share.email||'').trim(); if(!email){ this.share.err='Enter their email.'; return; }
      this.share.member=(this.orgMembers||[]).find(m=>(m.email||'').toLowerCase()===email.toLowerCase())||null;
      if(this.share.member) return;  // already on the team — the modal now says "just send the link"
      this.share.busy=true; this.share.err='';
      const landing='/app/'+(this.detail.kind==='skill'?'skills':'tools')+'/'+encodeURIComponent(this.detail.name);
      // Full access (default) = no restriction. Unchecked = scope to the skill itself + its
      // bundled tools (the access list also gates which skills a restricted member can SEE).
      let tool_access=null;
      if(!this.share.full){
        tool_access = this.detail.kind==='skill'
          ? [this.detail.name].concat((((this.detailData&&this.detailData.tools)||[]).map(t=>t.name)))
          : [this.detail.name];
        tool_access=[...new Set(tool_access)];
      }
      try{
        const r=await this.api('/orgs/'+this.activeOrgId+'/invites',{method:'POST',headers:{'content-type':'application/json'},
          body:JSON.stringify({email, role:this.share.role, landing, tool_access, local_run_enabled:false})});
        this.share.sent=r;
      }catch(e){ this.share.err='Invite failed: '+(e.detail||e.status); }
      finally{ this.share.busy=false; } }
}
