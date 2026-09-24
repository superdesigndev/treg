
export default {
async loadApiKeys(){ if(!this.activeOrgId) return; this.keyErr='';
      try{ this.apiKeys=await this.api('/orgs/'+this.activeOrgId+'/api-keys'); }
      catch(e){ this.keyErr='Could not load keys: '+(e.detail||e.status); } },
async loadDefaultToken(){
      const issued=await this.api('/auth/cli-token').catch(()=>null);
      this._myTokenOrg=this.activeSlugNow;
      this.defaultKeyId=issued&&issued.default_key_id;
      this.defaultKeyState=issued&&issued.default_key_state;
      this.myToken=(this.defaultKeyState==='disabled')?null:((issued&&issued.token)||null);
    },
async enableDefaultKey(){ if(!this.defaultKeyId) return; this.keyBusy=true; this.keyErr='';
      try{ await this.api('/orgs/'+this.activeOrgId+'/api-keys/'+this.defaultKeyId+'/enable',{method:'POST'}); await this.loadDefaultToken(); await this.loadApiKeys(); }
      catch(e){ this.keyErr='Could not enable key: '+(e.detail||e.status); } finally{ this.keyBusy=false; } },
keyKind(kind){ return ({default_human:'Default',additional_human:'Additional',legacy_human:'Legacy',agent:'Agent'})[kind]||kind; },
maskedKey(k){ return k.safe_prefix ? k.safe_prefix+'••••••••' : (k.kind==='legacy_human'?'prefix unavailable — older key':'prefix unavailable'); },
async createApiKey(){ const name=(this.keyName||'').trim(); if(!name){ this.keyErr=''; this.keyNameInvalid=true; return; } this.keyBusy=true; this.keyErr='';
      try{ this.newApiKey=await this.api('/orgs/'+this.activeOrgId+'/api-keys',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name})}); this.keyName=''; this.keyNameInvalid=false; await this.loadApiKeys(); }
      catch(e){ this.keyErr='Could not create key: '+(e.detail||e.status); } finally{ this.keyBusy=false; } },
async renameApiKey(k){ const name=(this.editKeyName||'').trim(); if(!name){ this.keyErr='Enter a key name.'; return; } this.keyBusy=true;
      try{ await this.api('/orgs/'+this.activeOrgId+'/api-keys/'+k.id,{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({name})}); this.editKey=null; await this.loadApiKeys(); }
      catch(e){ this.keyErr='Could not rename key: '+(e.detail||e.status); } finally{ this.keyBusy=false; } },
keyHasMore(k){ return k.can_rename||k.can_disable||k.can_enable||k.can_revoke||(k.can_hide&&k.state==='revoked'); },
toggleKeyMenu(k,e){ if(this.keyMenu&&this.keyMenu.key.id===k.id){ this.keyMenu=null; return; } const r=e.currentTarget.getBoundingClientRect(); this.keyMenu={key:k,top:r.bottom+6,right:Math.max(12,innerWidth-r.right)}; },
requestKeyAction(k,action){ this.keyMenu=null; if(['rotate','disable','revoke','hide'].includes(action)){ this.keyConfirm={key:k,action}; return; } this.keyAction(k,action); },
confirmKeyAction(){ if(!this.keyConfirm)return; const c=this.keyConfirm; this.keyConfirm=null; this.keyAction(c.key,c.action); },
async keyAction(k,action){ this.keyBusy=true; this.keyErr=''; this.keyMsg=null;
      try{ const r=await this.api('/orgs/'+this.activeOrgId+'/api-keys/'+k.id+'/'+action,{method:'POST'});
        if(r.secret && k.kind==='default_human'){ this.myToken=r.secret; this.defaultKeyId=k.id; this.defaultKeyState='active'; this._myTokenOrg=this.activeSlugNow; this.startTokenShow=false; this.newApiKey={...r,rotated:true}; }
        else if(r.secret){ this.newAgent=null; this.snipAgent=null; this.agentSnip='prompt'; this.newApiKey={...r, assigned_name:k.assigned_name, assigned_type:k.assigned_type, user_id:k.user_id, org:this.activeSlugNow, rotated:true}; }
        if(r.agent_revoked) this.keyMsg={agent:true,text:k.assigned_name+' was removed from this team, and all its keys were revoked. Historical Activity remains available.'};
        if(k.kind==='default_human' && action!=='rotate') await this.loadDefaultToken();
        if(r.agent_revoked) await this.loadOrgAdmin(); else await this.loadApiKeys(); }
      catch(e){ this.keyErr='Key action failed: '+(e.detail||e.status); } finally{ this.keyBusy=false; } },
showKeyActivity(k){ this.activityKey=String(k.id); this.go('activity'); }
}
