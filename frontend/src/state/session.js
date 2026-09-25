import { requestJson } from '../api'
import { LS } from './constants.js'

export default {
focusOverlay(open){ if(!open) return; this.$nextTick(()=>{ const el=document.querySelector('.scrim input,.scrim textarea,.scrim button,.drawer input,.drawer button'); el&&el.focus(); }); },
servedOn(tier){ return ({anonymous:'public provider route (no key)',platform:'treg key',credential:'your key',tool:'your registered tool','platform-overflow':'treg overflow'})[tier]||tier; },
headers(tok){
      const h={'ngrok-skip-browser-warning':'1'};
      if(tok){ h['X-Treg-Token']=tok; }                          // explicit (token login validation)
      else if(this.sessionMode){ const a=this.activeOrg; if(a) h['X-Treg-Org']=a.slug; }  // cookie carries identity
      else if(this.token){ h['X-Treg-Token']=this.token; }       // token mode
      return h; },
async api(path, opts={}){
      return requestJson(path, opts, this.headers(), () => {
        this.sessionMode=false; location.reload();
      }, () => this.sessionMode);
    },
save(){ localStorage.setItem(LS, JSON.stringify(this.cfg)); },
connected(slug){ return this.sessionMode || !!this.cfg.orgs[slug]; },
toggleTheme(){ this.theme=this.theme==='dark'?'light':'dark'; document.documentElement.dataset.theme=this.theme; localStorage.setItem('treg-theme',this.theme); },
_stashNext(){  // OAuth callbacks land on /app, losing a /app/skills/<x> deep link — stash it to restore after boot
      const d=this.routeFromPath(location.pathname)||this.mkFromPath(location.pathname);
      if(d) localStorage.setItem('treg-next', location.pathname); },
githubLogin(){ this._stashNext(); location.href='/auth/github'; },
googleLogin(){ this._stashNext(); location.href='/auth/google'; },
openSignin(){ this.demo.signin=true; },
async emailStart(){ const e=(this.emailInput||'').trim(); if(!e){ this.loginErr='Enter your email address.'; return; } this.busy=true; this.loginErr='';
      try{ const r=await this.api('/auth/email/start',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({email:e})});
        this.emailStage=true; this.devCode=r.dev_code||''; this.codeInput=''; }
      catch(err){ this.loginErr='Could not send a code - try again.'; } finally{ this.busy=false; } },
async emailVerify(){ const code=(this.codeInput||'').trim(); if(!code) return; this.busy=true; this.loginErr='';
      try{ await this.api('/auth/email/verify',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({email:this.emailInput.trim(),code})});
        location.reload(); }  // verify sets the session cookie → reload lands in session mode (like GitHub)
      catch(err){ this.loginErr = err.status===401?'Wrong or expired code.':'Sign-in failed.'; } finally{ this.busy=false; } },
async acceptInvite(inv){ try{ await this.api('/invites/'+inv.id+'/accept',{method:'POST'});
        this.pendingInvites=this.pendingInvites.filter(i=>i.id!==inv.id); await this.loadAll(); }
      catch(e){ this.err='Accept failed: '+(e.detail||e.status); } },
logout(){ window.TregTracking?.identify('',''); try{ if(window.Intercom) window.Intercom('shutdown'); }catch(e){}  // drop the Intercom cookie so the next user on this machine can't read these conversations
      if(this.sessionMode){ fetch('/auth/logout',{method:'POST',credentials:'include'}).finally(()=>{localStorage.removeItem('treg-active');location.reload();}); } else { this.cfg={active:null,orgs:{}}; this.save(); location.reload(); } },
async addToken(tok, isAdd){ tok=(tok||'').trim(); if(!tok) return; this.busy=true; this.loginErr='';
      try{ const orgs=await this.api('/orgs',{headers:this.headers(tok)}); const active=orgs.find(o=>o.active)||orgs[0];
        if(!active){ this.loginErr='Token has no org.'; return; }
        this.cfg.orgs[active.slug]={token:tok, role:active.role, name:active.name, org_id:active.org_id};
        this.cfg.active=active.slug; this.save(); this.tokenInput=''; this.addOrg=false; await this.loadAll();
      }catch(e){ this.loginErr = e.status===401?'Invalid token.':('Error: '+(e.detail||e.status)); }
      finally{ this.busy=false; } },
switchOrg(o){ this.orgMenu=false; this.newAgent=null; this.snipAgent=null; this.newApiKey=null; this.keyMsg=null;
      if(this.sessionMode){ this.activeSlug=o.slug; localStorage.setItem('treg-active',o.slug); this.loadAll(); this.intercomUpdate(); return; }
      if(!this.connected(o.slug)){ this.addOrg=true; return; }
      this.cfg.active=o.slug; this.save(); this.loadAll(); this.intercomUpdate(); },
async loadAll(){ this.err=''; this.loading=true;
      try{
        // /invites/mine needs no team, so it runs alongside /orgs; the bearer runs alongside the
        // team's data once the active team is known. The boot waits on one round trip per step.
        const invites=this.sessionMode ? this.api('/invites/mine').catch(()=>[]) : null;
        this.myOrgs=await this.api('/orgs');
        if(!this.sessionMode && !this.me){ const who=await this.api('/auth/me').catch(()=>null); if(who){ this.me=who.email; this.isAdmin=!!who.is_superadmin; } }  // token mode: learn our own email + superadmin flag (isPersonal / join-by-code)
        if(this.sessionMode && (!this.activeSlug || !this.myOrgs.some(o=>o.slug===this.activeSlug)) && this.myOrgs.length){
          // Land on the org that actually has tools (most first). Tie / all-empty -> prefer a TEAM over
          // the personal org (first-run confusion killer). Fixes: imports living in the personal space
          // while the default opened an empty team.
          const byTools=[...this.myOrgs].sort((a,b)=> (b.tool_count||0)-(a.tool_count||0)
            || ((this.isPersonal(a)?1:0)-(this.isPersonal(b)?1:0)) );
          this.activeSlug=byTools[0].slug; localStorage.setItem('treg-active',this.activeSlug);
        }
        // Re-mint the bearer whenever the ACTIVE org changes: the token now bakes the org slug in
        // (so it works as a bare MCP Authorization bearer), and a stale one would name the old team.
        // Signed derivation — cheap; the selected Default row contributes its team-local generation.
        const token=(this.myToken===null || this._myTokenOrg!==this.activeSlugNow) ? this.loadDefaultToken() : null;
        if(invites) this.pendingInvites=await invites;  // BEFORE the no-orgs early return: an invited user has 0 orgs but DOES have a pending invite — maybeOnboard needs it to offer joining instead of forcing create-team
        if(!this.myOrgs.length){ await token; this.tools=[]; this.bundles=[]; this.health={}; return; }  // brand-new user: no team yet → the mandatory welcome (maybeOnboard) creates the first one; skip org-scoped fetches (they'd 400). finally{} clears loading.
        this.loadConnections();  // fire-and-forget: connections must never block the tools view
        const [tools, health, bundles]=await Promise.all([this.api('/tools'), this.api('/health').catch(()=>[]), this.api('/bundles').catch(()=>[]), token]);
        this.tools=tools; this.bundles=bundles||[]; this.health={}; (health||[]).forEach(h=>this.health[h.secret_id]=h.status);
        // isAdmin (super-admin) comes from /auth/me at boot - NOT a /admin/stats probe, which 403s on
        // every load/switch for the 99% of users who aren't super-admins (console-error noise + wasted request).
        this.loadBilling();  // fire-and-forget, self-guards on canAdmin — feeds the sidebar balance card on every load/switch
        if(this.view==='orgs'){ this.loadOrgAdmin(); this.loadMyUsage(); }  // refresh team panel after a switch
        // Activity is one view with two tabs now, so a team switch has to refresh whichever is open
        // — both are org-scoped and would otherwise keep showing the previous team's numbers.
        if(this.view==='activity'){ this.loadCalls(); if(this.actTab==='usage') this.loadUsage(); }
        if(this.view==='secrets') this.loadSecrets();  // …and the Secrets view (was showing the previous org's)
        if(this.view==='resources') this.loadTeamResources();
      }catch(e){ this.err='Failed to load: '+(e.detail||e.status); }
      finally{ this.loading=false; } }
}
