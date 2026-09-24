
export default {
// ---- agents: a member identity for a machine caller ----
    pollAgentConnected(){
      // Flip the token card to ✓ the moment the setup instruction's final check-in lands.
      if(this._agentPoll) clearInterval(this._agentPoll);
      let tries=0;
      this._agentPoll=setInterval(async()=>{
        if(!this.newAgent || this.agentConnected || ++tries>40){ clearInterval(this._agentPoll); this._agentPoll=null; return; }
        await this.loadOrgAdmin(); }, 3000); },
promoteObserved(o){
      // Promotion = mint a real identity for a runtime we've only SEEN so far. Prefill the form;
      // the admin still picks role/cap/projects and presses Create, then swaps the env key.
      // promotePending links the mint to this (member, runtime) pair, so the detected row
      // disappears the moment the agent exists (and returns if the agent is revoked).
      const who=(o.member.split('@')[0]||'agent').replace(/[^a-zA-Z0-9-]/g,'-');
      this.promotePending={member:o.member, client:o.client};
      this.showAddAgent=true; this.showInvite=false;
      this.agentAccessMode=null; this.agentToolSel={};
      this.agentName=who+'-'+o.client; this.promoteHint='Creating a token for "'+o.client+'" running as '
        +o.member+'. After Create, put the new token in that runtime\'s TREG_TOKEN — from then on it '
        +'acts as itself, with its own cap and scope, instead of as '+o.member+'.'; },
openAddAgent(){ this.showAddAgent=!this.showAddAgent; if(this.showAddAgent){ this.showInvite=false; this.agentAccessMode=null; this.agentToolSel={}; } },
async createAgent(){ const name=(this.agentName||'').trim();
      if(!name){ this.agentErr='Give the agent a name, e.g. ci-bot.'; return; }
      if(!this.agentAccessMode){ this.agentErr='Choose All tools or Choose tools before creating the agent.'; return; }
      // An admin agent can manage this team's tools, secrets and members. That is a real step up from
      // 'can call things', so make it a deliberate choice rather than a dropdown you skimmed past.
      if(this.agentRole==='admin' && !confirm(
          'Create "'+name+'" as an ADMIN agent?\n\nAn admin agent can register and delete tools and '
          +'secrets, invite members, and set access for this team — not just call tools.\n\n'
          +'Most agents only need "member".')) return;
      this.agentBusy=true; this.agentErr='';
      const body={name, role:this.agentRole, daily_call_cap:this.agentCap};
      body.tool_access=this.agentAccessMode==='all' ? null : this.accessNames.filter(n=>this.agentToolSel[n]);
      // Only send project_access when the admin actually narrowed it — all checked = every project.
      const picked=this.projects.filter(p=>this.agentProjSel[p.id]).map(p=>p.id);
      if(this.projects.length && picked.length<this.projects.length) body.project_access=picked;
      if(this.promotePending){ body.promoted_member=this.promotePending.member; body.promoted_client=this.promotePending.client; }
      try{ const r=await this.api('/orgs/'+this.activeOrgId+'/agents',{method:'POST',headers:{'content-type':'application/json'},
             body:JSON.stringify(body)});
           this.agentTokens[r.user_id]=r.token; this.newApiKey=null; this.newAgent={...r,rotated:false}; this.snipAgent=null; this.agentName=''; this.agentAccessMode=null; this.agentToolSel={}; this.promoteHint=''; this.promotePending=null; await this.loadOrgAdmin(); this.pollAgentConnected(); }
      catch(e){ this.agentErr='Create failed: '+(e.detail||e.status); }
      this.agentBusy=false; },
// Rotate = create with the SAME name: the server replaces the token hash, so the old one dies.
    // We deliberately send only name/role/cap — the server leaves every field we DON'T send as it is,
    // so the agent's tool ACL and project scope survive a rotate (they used to be silently cleared).
    async rotateAgent(a, confirmed=false){ const mark='rotate-'+a.user_id;
      if(!confirmed && this.confirmAgent!==mark){ this.confirmAgent=mark; return; }
      this.confirmAgent=null; this.agentBusy=true; this.agentErr='';
      try{ const r=await this.api('/orgs/'+this.activeOrgId+'/agents',{method:'POST',headers:{'content-type':'application/json'},
             body:JSON.stringify({name:a.name, role:a.role, daily_call_cap:a.daily_call_cap})});
           this.agentTokens[r.user_id]=r.token; this.newApiKey=null; this.newAgent={...r,rotated:true}; this.snipAgent=null; await this.loadOrgAdmin(); this.pollAgentConnected(); }
      catch(e){ this.agentErr='Rotate failed: '+(e.detail||e.status); }
      this.agentBusy=false; },
showAgentSetup(a){ this.newAgent=null; this.agentSnip='prompt';
      this.snipAgent=(this.snipAgent && this.snipAgent.user_id===a.user_id) ? null : a; },
async revokeAgent(a){ const mark='revoke-'+a.user_id; if(this.confirmAgent!==mark){ this.confirmAgent=mark; return; }
      this.confirmAgent=null;
      try{ await this.api('/orgs/'+this.activeOrgId+'/agents/'+a.user_id,{method:'DELETE'}); await this.loadOrgAdmin();
        this.orgMsg=a.name+' was removed and all its keys were revoked. Its Activity history remains; create and configure it again to restore it.'; }
      catch(e){ this.agentErr='Revoke failed: '+(e.detail||e.status); } }
}
