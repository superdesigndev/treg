
export default {
// ---- agents: a member identity for a machine caller ----
    pollAgentConnected(){
      this.stopAgentPoll();
      const target=this.agentConnectionTarget, orgId=this.activeOrgId;
      if(this.view!=='orgs' || !this.canAdmin || !target || target.org_id!==orgId || target.connected) return;
      const poll={controller:new AbortController(), timer:null};
      this.elements.agentPoll=poll;
      const live=()=>this.elements.agentPoll===poll && !poll.controller.signal.aborted
        && this.view==='orgs' && this.activeOrgId===orgId && this.canAdmin
        && this.agentConnectionTarget===target;
      let tries=0;
      const tick=async()=>{
        if(!live()){ if(this.elements.agentPoll===poll) this.stopAgentPoll(); return; }
        tries++;
        try{
          const status=await this.api('/orgs/'+orgId+'/agents/'+target.user_id+'/connection?api_key_id='+target.api_key_id,
            {signal:poll.controller.signal, cache:'no-store'});
          if(!live()) return;
          if(status.connected){ target.connected=true; this.stopAgentPoll(); return; }
        }catch(e){
          if(!live()) return;
          if([401,403,404].includes(e.status)){ this.invalidateCredentialIssue(target); this.stopAgentPoll(); return; }
        }
        // Schedule only after settlement: a slow response cannot overlap another poll.
        if(live() && tries<40) poll.timer=setTimeout(tick,3000);
        else if(this.elements.agentPoll===poll) this.stopAgentPoll();
      };
      poll.timer=setTimeout(tick,3000); },
// The poll belongs to the Team page, which shows the card: it stops when the page unmounts and
// starts again when the page comes back while the new agent has not checked in yet.
stopAgentPoll(){ const poll=this.elements.agentPoll; this.elements.agentPoll=null;
      if(poll){ clearTimeout(poll.timer); poll.controller.abort(); } },
resumeAgentPoll(){ if(!this.elements.agentPoll) this.pollAgentConnected(); },
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
      if(isNaN(this.agentCap)){ this.agentErr='Daily cap must be a whole number of calls, or empty for no limit.'; return; }
      if(!this.agentAccessMode){ this.agentErr='Choose All tools or Choose tools before creating the agent.'; return; }
      // An admin agent can manage this team's tools, secrets and members. That is a real step up from
      // 'can call things', so make it a deliberate choice rather than a dropdown you skimmed past.
      if(this.agentRole==='admin' && !confirm(
          'Create "'+name+'" as an ADMIN agent?\n\nAn admin agent can register and delete tools and '
          +'secrets, invite members, and set access for this team — not just call tools.\n\n'
          +'Most agents only need "member".')) return;
      const issue=this.beginCredentialIssue('agent'); if(!issue) return;
      const orgId=issue.org_id, live=this.ticket('agentIssue');
      this.agentBusy=true; this.agentErr='';
      const body={name, role:this.agentRole, daily_call_cap:this.agentCap};
      body.tool_access=this.agentAccessMode==='all' ? null : this.accessNames.filter(n=>this.agentToolSel[n]);
      // Only send project_access when the admin actually narrowed it — all checked = every project.
      const picked=this.projects.filter(p=>this.agentProjSel[p.id]).map(p=>p.id);
      if(this.projects.length && picked.length<this.projects.length) body.project_access=picked;
      if(this.promotePending){ body.promoted_member=this.promotePending.member; body.promoted_client=this.promotePending.client; }
      try{ const r=await this.api('/orgs/'+orgId+'/agents',{method:'POST',headers:{'content-type':'application/json'},
             body:JSON.stringify(body)});
           await this.finishCredentialIssue(issue,{...r,connected:false,rotated:false});
           if(live()){ this.snipAgent=null; this.agentName=''; this.agentAccessMode=null; this.agentToolSel={}; this.promoteHint=''; this.promotePending=null; }
           if(this.view==='orgs' && this.credentialIssue===issue) await this.loadOrgAdmin(); }
      catch(e){ this.failCredentialIssue(issue); }
      finally{ if(live()) this.agentBusy=false; } },
// Rotate = create with the SAME name: the server replaces the token hash, so the old one dies.
    // We deliberately send only name/role/cap — the server leaves every field we DON'T send as it is,
    // so the agent's tool ACL and project scope survive a rotate (they used to be silently cleared).
    async rotateAgent(a, confirmed=false){ const mark='rotate-'+a.user_id;
      if(this.credentialIssue) return;
      if(!confirmed && this.confirmAgent!==mark){ this.confirmAgent=mark; return; }
      const issue=this.beginCredentialIssue('agent'); if(!issue) return;
      const orgId=issue.org_id, live=this.ticket('agentIssue');
      this.confirmAgent=null; this.agentBusy=true; this.agentErr='';
      try{ const r=await this.api('/orgs/'+orgId+'/agents',{method:'POST',headers:{'content-type':'application/json'},
             body:JSON.stringify({name:a.name, role:a.role, daily_call_cap:a.daily_call_cap})});
           await this.finishCredentialIssue(issue,{...r,connected:false,rotated:true});
           if(this.view==='orgs' && this.credentialIssue===issue){ this.snipAgent=null; await this.loadOrgAdmin(); } }
      catch(e){ this.failCredentialIssue(issue); }
      finally{ if(live()) this.agentBusy=false; } },
showAgentSetup(a){ if(this.credentialIssue) return; this.agentSnip='prompt';
      this.snipAgent=(this.snipAgent && this.snipAgent.user_id===a.user_id) ? null : a; },
async revokeAgent(a){ if(this.credentialIssue) return; const mark='revoke-'+a.user_id; if(this.confirmAgent!==mark){ this.confirmAgent=mark; return; }
      this.confirmAgent=null;
      try{ await this.api('/orgs/'+this.activeOrgId+'/agents/'+a.user_id,{method:'DELETE'}); await this.loadOrgAdmin();
        this.orgMsg=a.name+' was removed and all its keys were revoked. Its Activity history remains; create and configure it again to restore it.'; }
      catch(e){ this.agentErr='Revoke failed: '+(e.detail||e.status); } }
}
