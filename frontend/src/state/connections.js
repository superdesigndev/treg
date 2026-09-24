
export default {
// ---- connections (registry OAuth) ----
    async loadConnections(){
      this.connErr='';
      try{
        const [ps, cs]=await Promise.all([
          fetch('/oauth/providers').then(r=>r.json()).catch(()=>[]),
          this.api('/connections').catch(()=>[]),
        ]);
        this.providers=ps||[]; this.connections=cs||[];
      }catch(e){ this.connErr=String(e.message||e); }
      this.loadPlatforms();  // fire-and-forget: the catalog must never hold up the connect UI
    },
authorizationMethodSpec(providerName, methodName){
      const provider=(this.providers||[]).find(item=>item.service===providerName);
      return provider && (provider.authorization_methods||[]).find(method=>method.name===methodName);
    },
authorizationMethodLabel(providerName, methodName){
      const method=this.authorizationMethodSpec(providerName,methodName);
      return (method&&method.display_name)||methodName;
    },
async connectProvider(p, capability, conn){
      // Consent happens in a popup so the dashboard keeps its state; we poll for the result
      // rather than depending on the popup being able to talk back to us.
      this.connErr=''; this.connBusy=true;
      try{
        const d=await this.api('/oauth/start',{method:'POST',headers:{'content-type':'application/json'},
          // connection_id present = reconnect/widen THAT account; absent = attach another one.
          body:JSON.stringify({provider:p.service, capability, connection_id:conn?conn.id:null})});
        const w=window.open(d.consent_url,'treg-connect','width=560,height=720');
        if(!w){ this.connErr='Popup blocked — allow popups, or open: '+d.consent_url; this.connBusy=false; return; }
        for(let i=0;i<150;i++){
          await new Promise(r=>setTimeout(r,2000));
          let s; try{ s=await this.api('/oauth/status/'+d.state); }catch(e){ continue; }
          if(s.status==='done'){
            try{w.close();}catch(e){}
            await this.loadConnections(); await this.loadAll(); this.connBusy=false;
            // Connecting is only half the job — a connection with no target can't be used. If this
            // provider has something to choose between, ask straight away rather than leaving the
            // user staring at a row that looks finished but isn't.
            const fresh=(this.connections||[]).find(c=>c.id===s.secret_id);
            if(fresh && fresh.supports_discovery && !fresh.resource_ref) await this.openResources(fresh);
            return;
          }
          if(s.status==='error'){ this.connErr='Connect failed: '+(s.detail||'unknown'); this.connBusy=false; return; }
        }
        this.connErr='Timed out waiting for authorization.';
      }catch(e){ this.connErr=String(e.message||e); }
      this.connBusy=false;
    },
async enableCapability(c, cap){
      // Providers never backfill scopes onto an issued grant, so widening access means re-running
      // consent for the bigger scope set. The callback rebinds the existing tool, so nothing
      // downstream is rewired — the user just sees the new capability appear.
      const p=this.providers.find(x=>x.service===c.provider);
      if(!p){ this.connErr='Unknown provider for this connection: '+c.provider; return; }
      await this.connectProvider(p, cap);
    },
async reconnect(c){
      // Re-run consent for the same provider. The callback rebinds the existing tool to the new
      // credential, so nothing downstream has to be rewired.
      const p=this.providers.find(x=>x.service===c.provider);
      if(!p){ this.connErr='Unknown provider for this connection: '+c.provider; return; }
      const method=(p.authorization_methods||[]).find(m=>m.name===c.authorization_method);
      const options=method ? method.capabilities : (c.capabilities||p.capabilities||['read']);
      const granted=new Set(c.capabilities||[]);
      const cap=[...options].filter(option=>granted.has(option)).sort((a,b)=>
        ((p.scope_detail[a]||[]).length-(p.scope_detail[b]||[]).length)).slice(-1)[0]
        || this.methodCapability(p,method);
      await this.connectProvider(p, cap, c);
    },
capOptions(ask){
      // Authorization-method capabilities are declared narrowest → broadest for scope
      // containment. Present the choice broadest → narrowest, matching every existing provider.
      if(ask && ask.method) return [...(ask.method.capabilities||[])].reverse();
      if(!ask || !ask.conn) return (ask&&ask.provider.capabilities)||[];
      const method=(ask.provider.authorization_methods||[]).find(m=>m.name===ask.conn.authorization_method);
      return method ? method.capabilities : (ask.provider.capabilities||[]);
    },
article(w){ return /^[aeiou]/i.test(w||'') ? 'an' : 'a'; },
// "an account", not "a account"
    mkCapabilityMethod(cap){ return ((this.mkProvider&&this.mkProvider.authorization_methods)||[]).find(m=>
      m.capability_details && (m.capability_details[cap]||[]).length); },
mkCapabilityLabel(cap){ const m=this.mkCapabilityMethod(cap); return (m&&m.capability_labels&&m.capability_labels[cap])||cap; },
mkCapabilityIntro(cap){ const m=this.mkCapabilityMethod(cap); return (m&&m.capability_intros&&m.capability_intros[cap])||''; },
mkCapabilityDetails(cap){
      const m=this.mkCapabilityMethod(cap), labels=m&&m.capability_details[cap];
      return labels ? labels.map(label=>({scope:'',label})) : (((this.mkProvider&&this.mkProvider.scope_detail)||{})[cap]||[]);
    },
capMethod(ask, cap){
      if(ask&&ask.method) return ask.method;
      if(ask&&ask.conn) return (ask.provider.authorization_methods||[]).find(m=>m.name===ask.conn.authorization_method);
      return (ask&&ask.provider.authorization_methods||[]).find(m=>(m.capabilities||[]).includes(cap));
    },
capLabel(cap, ask){ const m=this.capMethod(ask,cap), custom=m&&m.capability_labels&&m.capability_labels[cap]; return custom || {read:'Read only', draft:'Read and draft', post:'Read and publish', write:'Read and write', manage:'Full access'}[cap] || cap; },
capHelp(cap, ask){ const m=this.capMethod(ask,cap), custom=m&&m.capability_help&&m.capability_help[cap]; return custom || {
      read:'Your agent can view data. It can never change anything.',
      // TikTok's `draft` is a genuinely weaker grant, not a softer word for post: video.upload can
      // only drop a video into the creator's inbox for them to finish by hand, and TikTok bins it
      // after 24h if they don't. Nothing reaches the profile without a human.
      draft:'Your agent can view data and prepare content, but only as a draft you finish and post yourself.',
      // `post` is the YouTube middle ground: uploading a video and being able to edit or delete
      // one are separate Google scopes, so publish-without-touching-the-back-catalogue is a real
      // choice rather than a hedge.
      post:'Your agent can view data and publish new content. It cannot edit or delete anything already there.',
      write:'Your agent can view data and also create, update or publish.',
      manage:'Your agent can view and manage everything in the account.',
    }[cap] || 'Requests the '+cap+' scopes.'; },
capInReview(cap, ask){ const m=this.capMethod(ask,cap); return !!(m&&(m.capabilities_in_review||[]).includes(cap)); },
capDefault(ask){
      if(ask&&ask.method&&ask.method.connect_capability) return ask.method.connect_capability;
      return ask&&(ask.provider.connect_default_capability||ask.provider.default_capability);
    },
isRecommendedMethod(p, method){ return !!(method && (method.capabilities||[]).includes(p.connect_default_capability||p.default_capability)); },
selectedMethod(ask){ return ask && (ask.provider.authorization_methods||[]).find(m=>m.name===ask.selected); },
methodCapability(p, method){
      const caps=(method&&method.capabilities)||[];
      const preferred=p.connect_default_capability||p.default_capability;
      return method&&method.connect_capability || (caps.includes(preferred) ? preferred : (caps[caps.length-1]||preferred));
    },
startConnect(p, conn){
      // A pasted-secret provider has no consent screen — the user brings their own bot token (Slack)
      // or API key (Apollo, TikHub, …), so setup is a form, not a redirect.
      if(p.auth_kind==='token' || p.auth_kind==='key') return void (this.tokenAsk={provider:p, token:'', err:'', busy:false, conn});
      // Several separate grants are one Add-account decision. Providers with zero or one method
      // keep the old one-click behavior, so LinkedIn and every existing single-method flow do not
      // inherit an extra dialog. Reconnects also stay pinned to their stored method.
      const methods=p.authorization_methods||[];
      if(methods.length>1 && !conn){
        const available=methods.filter(method=>method.configured);
        const recommended=available.find(m=>this.isRecommendedMethod(p,m)) || available[0] || methods[0];
        this.methodAsk={provider:p,selected:recommended.name}; return;
      }
      if(methods.length===1 && !conn) return this.connectProvider(p,this.methodCapability(p,methods[0]));
      // One capability means there's nothing to ask — don't put a dialog in the way.
      if((p.capabilities||[]).length<2) return this.connectProvider(p, p.default_capability, conn);
      this.capAsk={provider:p, conn};
    },
async continueMethod(){
      const ask=this.methodAsk, method=this.selectedMethod(ask); if(!method || !method.configured) return;
      this.methodAsk=null;
      if((method.capabilities||[]).length>1){ this.capAsk={provider:ask.provider,conn:null,method}; return; }
      await this.connectProvider(ask.provider,this.methodCapability(ask.provider,method));
    },
async submitToken(){
      const t=this.tokenAsk; if(!t.token.trim()) return;
      t.busy=true; t.err='';
      try{
        await this.api('/connections/token',{method:'POST',headers:{'content-type':'application/json'},
          body:JSON.stringify({provider:t.provider.service, token:t.token.trim()})});
        this.tokenAsk=null; await this.loadConnections(); await this.loadAll();
      }catch(e){ t.err=(e.detail||e.message||e); t.busy=false; }
    },
async chooseCapability(cap){
      const p=this.capAsk.provider, conn=this.capAsk.conn; this.capAsk=null;
      await this.connectProvider(p, cap, conn);
    },
connProvider(c){ return (this.providers||[]).find(p=>p.service===c.provider)||null; },
async saveExtraCred(c){
      const v=(this.extraCred[c.id]||'').trim(); if(!v) return;
      this.extraBusy=c.id; this.connErr='';
      try{
        await this.api('/connections/'+c.id+'/extra-credential',{method:'POST',
          headers:{'content-type':'application/json'}, body:JSON.stringify({value:v})});
        this.extraCred[c.id]=''; await this.loadConnections(); await this.loadAll();
      }catch(e){ this.connErr=(e.detail||e.message||e); }
      this.extraBusy=null;
    },
async openResources(c){
      this.connErr='';
      const pv=this.connProvider(c)||{}; const label=pv.resource_label||'resource'; const plural=pv.resource_plural||(label+'s');
      // Open FIRST. Discovery is a live upstream round-trip and can take seconds; waiting for it
      // before showing anything makes the button look dead and invites a second click.
      this.resPick={id:c.id, label, plural, rows:[], selected:c.resource_ref||'', loading:true, err:''};
      try{
        const d=await this.api('/connections/'+c.id+'/resources');
        if(!this.resPick || this.resPick.id!==c.id) return;  // user closed it or opened another
        this.resPick={id:c.id, label:d.resource_label||label, plural:d.resource_plural||plural,
                      rows:d.resources||[], selected:d.selected||'', loading:false,
                      err:d.setup_required?(d.setup_detail||'Account setup is required.'):''};
        // Discovery can change the row underneath us — it backfills a missing label and records
        // that the credential works — so pull the list again rather than leaving stale text on screen.
        this.loadConnections();
      }catch(e){
        // e.detail carries the server's (and often the upstream's) actual words; e.message is just 'http'.
        if(!this.resPick || this.resPick.id!==c.id) return;
        this.resPick.loading=false;
        this.resPick.err='Could not list '+plural+': '+(e.detail||e.message||e);
      }
    },
async chooseResource(r){
      try{
        await this.api('/connections/'+this.resPick.id+'/resource',{method:'POST',
          headers:{'content-type':'application/json'},
          body:JSON.stringify({resource_ref:r.id, resource_name:r.label||''})});
        this.resPick=null; await this.loadConnections();
      }catch(e){ this.connErr=String(e.message||e); }
    },
async disconnect(c){
      if(this.confirmDisc!==c.id){ this.confirmDisc=c.id; setTimeout(()=>{ if(this.confirmDisc===c.id) this.confirmDisc=null; },4000); return; }
      this.confirmDisc=null;
      try{ await this.api('/connections/'+c.id,{method:'DELETE'}); await this.loadConnections(); await this.loadAll(); }
      catch(e){ this.connErr=String(e.message||e); }
    }
}
