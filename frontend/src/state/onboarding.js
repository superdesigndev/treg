import { storageGet, storageRemove } from './storage.js'

export default {
maybeOnboard(){  // first-run: a brand-new user with no team yet is asked to NAME THEIR TEAM upfront
      // An invite LINK landed here (?invite_org= from /auth/invite-signin's POST)? Open the accept
      // modal even for an onboarded user already in teams — a second-team invite must surface too.
      if(this.inviteLinkOrg!==null && this.pendingInvites.length){ this.openInviteChoice(); return; }
      if(this.inviteLinkOrg!==null && !this.pendingInvites.length && this.sessionMode){
        this.orgMsg='That invite was already used or revoked — ask your teammate to re-invite you if you still need access.'; }
      if(this.onboarded || !this.sessionMode) return;
      // The server's first-run flow: a new account gets its team, a lookup and a first call there.
      // One that already has a team only resumes a flow it started (a reload mid-way).
      if(this.onboardingV2 && !this.pendingInvites.length){
        if(!this.myOrgs.some(o=>!this.isPersonal(o))){ this.onboardingV2On=true; return; }
        this.api('/onboarding').then(v=>{ if(v && v.status!=='none' && !this.onboarded) this.onboardingV2On=true; }).catch(()=>{});
        return; }
      // The experiment (a work address, so the server offers it): PostHog's `onboarding-v2` flag picks
      // the arm, `test` the first-run flow and anything else, unanswered included, the team-name modal.
      // A reload mid-way resumes a flow already started, without reading the flag again.
      if(this.onboardingV2Exp && !this.pendingInvites.length){
        if(this.myOrgs.some(o=>!this.isPersonal(o))){
          this.api('/onboarding').then(v=>{ if(v && v.status!=='none' && !this.onboarded) this.onboardingV2On=true; }).catch(()=>{});
          return; }
        this.featureVariant('onboarding-v2', 1500).then(v=>{
          if(this.onboarded) return;
          if(v==='test'){ this.onboardingV2On=true; return; }
          this.openWelcome(); });
        return; }
      if(this.myOrgs.some(o=>!this.isPersonal(o))) return;
      // Invited here? Show an ACCEPT-INVITE page (join those teams) instead of forcing them to create a
      // throwaway team of their own (confusing: they'd end up with two). Decline → create-team.
      if(this.pendingInvites.length){ this.openInviteChoice(); return; }
      this.openWelcome(); },
openWelcome(){ this.welcome.name=this._suggestTeamName(); this._welcomeAgentFromRef(); this.welcome.on=true; },
_welcomeAgentFromRef(){  // /grokbot's "Setup treg" CTA → the welcome already has Grok Bot picked; any other ref is ignored
      const r=storageGet('treg-ref'); storageRemove('treg-ref');
      if(r && this.welcomeAgents.concat(this.welcomeMoreAgents).some(a=>a.id===r)) this.welcome.agent=r; },
_restoreAgent(){  // the picked agent survives a reload, so Getting started keeps showing the right setup steps
      const r=storageGet('treg-agent');
      if(r && this.welcomeAgents.concat(this.welcomeMoreAgents).some(a=>a.id===r)) this.welcome.agent=r; },
openInviteChoice(){  // seed the multi-select: ALL pending invites checked by default
      this.inviteErr=''; this.inviteSel={}; this.pendingInvites.forEach(i=>{ this.inviteSel[i.id]=true; });
      this.inviteChoice=true; },
declineInvite(){ this.inviteChoice=false; this.inviteLinkOrg=null;
      if(this.inviteFirstRun){ this.openWelcome(); } },
// first-run: "create my own team instead"; otherwise just close
    async acceptSelectedInvites(){  // accept every checked invite, then drop into the linked (or first) team
      const picked=this.selectedInvites; if(!picked.length) return;
      this.inviteBusy=true; this.inviteErr='';
      const joined=[], failed=[];
      for(const inv of picked){
        try{ const r=await this.api('/invites/'+inv.id+'/accept',{method:'POST'});
          joined.push({inv, r}); this.pendingInvites=this.pendingInvites.filter(i=>i.id!==inv.id); delete this.inviteSel[inv.id]; }
        catch(e){ failed.push(inv.name); }
      }
      try{
        if(!joined.length){  // everything failed → keep the modal open with the error (or fall back on first run)
          this.inviteErr='Could not join '+failed.join(', ')+' — the invite may have been revoked.';
          if(this.inviteFirstRun && !this.pendingInvites.length){ this.inviteChoice=false; this.welcome.name=this._suggestTeamName(); this.welcome.on=true; }
          return; }
        this.onboarded=true; try{ await this.api('/onboard/skip',{method:'POST'}); }catch(e){}
        await this.loadAll();
        // Enter the team the email link pointed at when it was accepted; otherwise the first accepted.
        const linked=joined.find(j=>j.inv.org_id===this.inviteLinkOrg)||joined[0];
        if(linked.r&&linked.r.org) this.switchOrg({slug:linked.r.org});
        this.inviteChoice=false; this.inviteLinkOrg=null;
        if(this.detail) this.openDetail(this.detail.kind, this.detail.name, true); else this.go('tools');  // accepting while on a shared page = stay on it
        this.orgMsg='You joined '+joined.map(j=>(j.r&&j.r.name)||j.inv.name).join(', ')
          +(failed.length?' (could not join '+failed.join(', ')+')':'')
          +'. Here are the shared tools & skills — call any with no key on your machine.';
      }finally{ this.inviteBusy=false; }
    },
_suggestTeamName(){  // a friendly default from the email domain: sam@acme.dev → "Acme"
      const dom=((this.me||'').split('@')[1]||'').split('.')[0]||'';
      const generic=['gmail','outlook','hotmail','yahoo','icloud','proton','protonmail','me','qq','163'];
      return (dom && !generic.includes(dom.toLowerCase())) ? dom.charAt(0).toUpperCase()+dom.slice(1) : ''; },
async welcomeCreate(){ const name=(this.welcome.name||'').trim(); if(!name){ this.welcome.err='Give your team a name.'; return; }
      this.welcome.busy=true; this.welcome.err='';
      try{ const o=await this.api('/orgs',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name})});
        this.onboarded=true; try{ await this.api('/onboard/skip',{method:'POST'}); }catch(e){}  // don't re-prompt
        await this.loadAll(); this.switchOrg({slug:o.org});
        this.analyticsIdentify(); this.intercomUpdate(); this.track('onboarding_team_created',{team:o.org});
        // Google Ads `treg Signup (web)`: the browser-side twin of the server outbox row, for Google's
        // cross-device and modeled attribution; secondary in the account, so it never double counts.
        try{ if(typeof window.tregSignupConversion==='function') window.tregSignupConversion(o.org_id||o.org); }catch(e){}
        this.welcome.step=1; }  // stay in the modal: pick your agent → get the setup line
      catch(e){ this.welcome.err='Could not create the team: '+(e.detail||e.status); }
      finally{ this.welcome.busy=false; } },
welcomeFinish(){ this.track('onboarding_finished',{agent:this.welcome.agent, step:this.welcome.step}); this.welcome.on=false;
      // Someone who signed up on the way to a platform (a /search result) stays on it; otherwise
      // Getting started, where the setup line lives.
      if(this.view!=='platform') this.go('start');
      this.orgMsg='Team created. Send your agent the setup line any time — it lives on Getting started.'; },
agentIcon(icon){ if(icon.startsWith('/')) return icon;  // bundled under /logos — same mark in both themes
      return 'https://unpkg.com/@lobehub/icons-static-png@latest/'+(this.theme==='dark'?'dark':'light')+'/'+icon+'.png'; },
// For logos that sit ON a .btn.primary: its background is the theme's INVERSE, so the icon
    // variant has to flip too or a dark glyph lands on a dark button.
    agentIconInv(icon){ if(icon.startsWith('/')) return icon;
      return 'https://unpkg.com/@lobehub/icons-static-png@latest/'+(this.theme==='dark'?'light':'dark')+'/'+icon+'.png'; },
welcomeTryProvider(service,group){this.track('tryit_oauth_clicked',{service,group,from:'onboarding'});this.welcome.on=false;this.openProvider(service);},
async copyStart(text, tag){
      this.startCopyError=''; this.startCopied='';
      try {
        await navigator.clipboard.writeText(text);
        this.startCopied=tag;
        setTimeout(()=>{ if(this.startCopied===tag) this.startCopied=''; },1400);
      } catch(_) {
        this.startCopyError='Could not copy. Select the text and copy it manually. For an API key, choose Show key first.';
      }
    }
}
