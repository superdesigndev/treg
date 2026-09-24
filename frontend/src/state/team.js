
export default {
// ---- Phase 2a: org lifecycle (writes; all endpoints already exist) ----
    async joinByCode(){ const code=(this.joinCode||'').trim(); if(!code){ this.joinErr='Enter the invite code.'; return; } this.joinBusy=true; this.joinErr='';
      try{ const r=await this.api('/invites/accept',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({code, email:this.me})});
        this.showJoin=false; this.joinCode=''; await this.loadAll(); this.switchOrg({slug:r.org}); }
      catch(e){ this.joinErr = e.status===404?'Invalid or already-used code.':(e.status===403?'That code is for a different email.':(e.status===409?'You are already a member.':('Join failed: '+(e.detail||e.status)))); }
      finally{ this.joinBusy=false; } },
async createOrg(){ const name=(this.newOrgName||'').trim(); if(!name){ this.orgErr='Enter a team name.'; return; } this.orgBusy=true; this.orgErr='';
      try{ const o=await this.api('/orgs',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name})});
        // Token mode has no session - persist the new org's token (returned once) or switchOrg would
        // pop the "paste a token" modal for a token we just discarded.
        if(!this.sessionMode && o.token){ this.cfg.orgs[o.org]={token:o.token, role:o.role, name:o.name, org_id:o.org_id}; this.save(); }
        this.newOrg=false; this.newOrgName=''; await this.loadAll(); this.switchOrg({slug:o.org}); }
      catch(e){ this.orgErr='Could not create: '+(e.detail||e.status); } finally{ this.orgBusy=false; } },
async loadOrgAdmin(){ this.orgMembers=[]; this.orgInvites=[]; this.lastInvite=null;
      if(!this.canAdmin && !['keys','danger'].includes(this.orgTab)) this.orgTab='keys';
      this.orgErr=''; this.confirmDel=''; this.confirmLeave=false; this.confirmRemove=null;
      if(!this.activeOrgId) return; const id=this.activeOrgId;
      await this.loadApiKeys();
      if(!this.canAdmin) return;
      this.agentErr=''; this.confirmAgent=null;
      try{ this.orgMembers=await this.api('/orgs/'+id+'/members'); this.orgInvites=await this.api('/orgs/'+id+'/invites');
           this.projects=await this.api('/orgs/'+id+'/projects'); this.denyRules=await this.api('/orgs/'+id+'/deny');
           this.cliDeny=await this.api('/orgs/'+id+'/policy/cli-deny').catch(()=>[]);
           // agents live in the same roster now (an agent IS a membership)
           this.agents=await this.api('/orgs/'+id+'/agents').catch(()=>[]);
           const sel={}; this.projects.forEach(p=>{ sel[p.id]=true; }); this.agentProjSel=sel;
           this.observedAgents=await this.api('/orgs/'+id+'/agents/observed').catch(()=>[]); }
      catch(e){ this.orgErr='Load team failed: '+(e.detail||e.status); } },
async loadMyUsage(){ if(!this.activeOrgId){ this.myUsage=null; return; }
      this.myUsage=await this.api('/usage/me').catch(()=>null); },
// the caller's own used/cap (any member)
    async setCap(m, val){ const cap=parseInt(val,10);
      if(isNaN(cap)||cap<-1){ this.orgErr='Daily cap must be -1 (unlimited) or 0 and above.'; await this.loadOrgAdmin(); return; }
      if(cap===m.daily_call_cap) return;
      try{ await this.api('/orgs/'+this.activeOrgId+'/members/'+m.user_id+'/cap',{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({daily_call_cap:cap})}); }
      catch(e){ this.orgErr='Set cap failed: '+(e.detail||e.status); }
      await this.loadOrgAdmin(); },
// reload → reverts the input to the true cap on failure
    async sendInvite(){ const email=(this.inviteEmail||'').trim(); if(!email){ this.orgErr='Enter an email address to invite.'; return; }
      // Invites attach to an email - the server only trims/lowercases, so guard the format here or a
      // typo like "foo bar" becomes a permanently-unacceptable dead invite cluttering the list.
      if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)){ this.orgErr='Enter a valid email address.'; return; }
      this.orgBusy=true; this.orgErr='';
      const chosen=this.accessNames.filter(n=>this.inviteToolSel[n]);
      const tool_access = !this.inviteCustomize ? null : (chosen.length===this.accessNames.length ? null : chosen);
      try{ this.lastInvite=await this.api('/orgs/'+this.activeOrgId+'/invites',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({email,role:this.inviteRole,tool_access,local_run_enabled:this.inviteLocalRun})});
        this.inviteEmail=''; this.inviteCustomize=false; this.orgInvites=await this.api('/orgs/'+this.activeOrgId+'/invites'); }
      catch(e){ this.orgErr='Invite failed: '+(e.detail||e.status); } finally{ this.orgBusy=false; } },
openInviteCustomize(){ this.inviteCustomize=true; const d={}; this.accessNames.forEach(n=>d[n]=true); this.inviteToolSel=d; },
openAccess(m){ if(m.role==='owner') return;
      if(this.editAccess===m.user_id){ this.editAccess=null; return; }
      this.editAccess=m.user_id; const d={}; this.accessNames.forEach(n=>{ d[n]=(m.tool_access===null || m.tool_access.includes(n)); }); this.accessDraft=d;
      const pd={}; this.projects.forEach(p=>{ pd[p.id]=(m.project_access===null || m.project_access.includes(p.id)); }); this.projDraft=pd; },
setAllAccess(v){ const d={}; this.accessNames.forEach(n=>d[n]=v); this.accessDraft=d; },
async saveAccess(m){ const names=this.accessNames.filter(n=>this.accessDraft[n]);
      const tool_access = (names.length===this.accessNames.length) ? null : names;  // all checked → 'all' (NULL)
      const picked=this.projects.filter(p=>this.projDraft[p.id]).map(p=>p.id);
      const project_access = (!this.projects.length || picked.length===this.projects.length) ? null : picked;  // all checked → 'all'
      try{ await this.api('/orgs/'+this.activeOrgId+'/members/'+m.user_id+'/access',{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({tool_access, project_access, local_run_enabled:m.local_run_enabled})}); this.editAccess=null;
        if(m.is_agent) this.orgMsg=m.name+' access updated. Its key is unchanged, and the new tool and project permissions apply immediately.'; }
      catch(e){ this.orgErr='Set access failed: '+(e.detail||e.status); }
      await this.loadOrgAdmin(); },
async setLocalRun(m, val){
      try{ await this.api('/orgs/'+this.activeOrgId+'/members/'+m.user_id+'/access',{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({tool_access:m.tool_access, local_run_enabled:val})}); }
      catch(e){ this.orgErr='Set local-run failed: '+(e.detail||e.status); }
      await this.loadOrgAdmin(); },
async setRole(m, role){ if(role===m.role) return;
      let err='';
      try{ await this.api('/orgs/'+this.activeOrgId+'/members/'+m.user_id,{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({role})}); }
      catch(e){ err='Role change failed: '+(e.detail||e.status); }
      await this.loadOrgAdmin();  // ALWAYS reload → reverts the (:value-bound) select to the true role on failure
      if(err) this.orgErr=err;    // set the message AFTER the reload (loadOrgAdmin resets orgErr)
    },
async removeMember(m){ if(this.confirmRemove!==m.user_id){ this.confirmRemove=m.user_id; return; } this.confirmRemove=null;
      try{ await this.api('/orgs/'+this.activeOrgId+'/members/'+m.user_id,{method:'DELETE'}); await this.loadOrgAdmin(); }
      catch(e){ this.orgErr='Remove failed: '+(e.detail||e.status); } },
async revokeInvite(inv){ try{ await this.api('/orgs/'+this.activeOrgId+'/invites/'+inv.id,{method:'DELETE'});
        this.orgInvites=this.orgInvites.filter(i=>i.id!==inv.id); }
      catch(e){ this.orgErr='Revoke failed: '+(e.detail||e.status); } },
forgetActiveOrg(){  // drop the now-dead active org in whichever mode we're in, then fall back to another
      if(this.sessionMode){ this.activeSlug=null; localStorage.removeItem('treg-active'); }
      else { const s=this.cfg.active; if(s) delete this.cfg.orgs[s]; this.cfg.active=Object.keys(this.cfg.orgs)[0]||null; this.save(); } },
async leaveOrg(){ if(!this.confirmLeave){ this.confirmLeave=true; return; } this.confirmLeave=false;
      try{ await this.api('/orgs/'+this.activeOrgId+'/leave',{method:'POST'});
        this.forgetActiveOrg(); await this.loadAll(); }
      catch(e){ this.orgErr='Leave failed: '+(e.detail||e.status); } },
resetRenameForm(){ const a=this.activeOrg||{}; this.renameName=a.name||''; this.renameSlug=a.slug||''; this.renameErr=''; },
async renameOrg(){ this.renameBusy=true; this.renameErr='';
      const body={}, a=this.activeOrg||{}, n=this.renameName.trim(), sl=this.renameSlug.trim();
      if(n && n!==a.name) body.name=n; if(sl && sl!==a.slug) body.slug=sl;
      try{ const r=await this.api('/orgs/'+this.activeOrgId,{method:'PATCH', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
        const old=this.activeSlugNow;
        if(this.sessionMode){ this.activeSlug=r.org; localStorage.setItem('treg-active',r.org); }
        else if(r.org!==old){ this.cfg.orgs[r.org]=Object.assign({},this.cfg.orgs[old],{name:r.name}); delete this.cfg.orgs[old]; this.cfg.active=r.org; this.save(); }
        else { this.cfg.orgs[old].name=r.name; this.save(); }
        await this.loadAll(); this.resetRenameForm(); }
      catch(e){ this.renameErr=this._errMsg(e, 'could not rename the team'); }
      finally{ this.renameBusy=false; } },
async deleteOrg(){ if(this.confirmDel!==this.activeSlugNow) return;
      try{ await this.api('/orgs/'+this.activeOrgId+'?confirm='+encodeURIComponent(this.activeSlugNow),{method:'DELETE'});
        this.confirmDel=''; this.forgetActiveOrg(); await this.loadAll(); }
      catch(e){ this.orgErr='Delete failed: '+(e.detail||e.status); } }
}
