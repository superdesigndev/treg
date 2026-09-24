
export default {
// ---- marketplace endpoint Try-it ----
    openEpTry(e){
      this.closeEpTry();
      this.epTry=e; this.epTryTab='agent'; this.epTryResp=''; this.epTryStatus=null; this.epTryMs=null; this.epTryCost=null;
      this.epTryAccess=null; this.epTryAccessByMethod={}; this.epTryBusy=false;
      this.epTryAuthMethod=e.authorization_method||((e.authorization_methods||[])[0])||'';
      const secs=this.paramSections(e);
      const tr=e.test_request||{};  // the live-verified request — the best possible prefill
      const trq={...(tr.queryParams||{}), ...(tr.pathParams||{}), ...(tr.headers||{})};
      const qp=[];  // query + path params (path placeholders are consumed from query server-side)
      for(const s of secs) if(s.key!=='body') for(const p of s.rows)
        qp.push({name:p.name, required:!!p.required, authorization_methods:p.authorization_methods||[],
                 location:s.key==='headers'?'header':s.key,
                 value: trq[p.name]!=null ? this.fmtExample(trq[p.name])
                        : (p.example!=null ? this.fmtExample(p.example) : '')});
      // test_request may carry params the input spec doesn't list — keep them, they made the call work
      for(const [k,v] of Object.entries(trq)) if(!qp.some(p=>p.name===k)) qp.push({name:k, required:false, location:'query', value:this.fmtExample(v)});
      this.epTryParams=qp;
      const bodySec=secs.find(s=>s.key==='body');
      this.epTryBodyType=(bodySec&&bodySec.type)||'json'; this.epTryMultipart=[]; this.epTryFiles={};
      if(this.epTryBodyType==='multipart' && bodySec){
        this.epTryMultipart=bodySec.rows.map(p=>({name:p.name,type:p.type||'string',required:!!p.required,
          value:tr.body&&tr.body[p.name]!=null?this.fmtExample(tr.body[p.name]):(p.example!=null?this.fmtExample(p.example):'')}));
      }
      let body='';
      if((e.method||'GET')!=='GET' && this.epTryBodyType!=='multipart'){
        if(tr.body!=null){ body=JSON.stringify(tr.body,null,2); }  // verbatim — array-vs-object is ground truth here
        else{
          if(bodySec){ const o={}; for(const p of bodySec.rows) if(p.example!=null||p.required) o[p.name]=p.example!=null?p.example:null;
            body=JSON.stringify(o,null,2); }
        }
      }
      this.epTryBody=body;
      if(e.provider==='fishaudio') this.loadFishVoices(); else this.fishVoices=[];
      // Voice listing has a first-party unified read. The raw /call endpoint stays a faithful Fish
      // relay, while this action selects BYOK or organization-owned platform resources server-side.
      if(e.id==='fishaudio.voices.list') this.epTryAccess={tier:'platform',detail:'Uses your connected Fish Audio account when present; otherwise lists voices owned by this treg team. No BYOK key is required for team voices.'};
      else this.loadEpTryAccessPolicy();
    },
epTryParamAllowed(p){ return !p.authorization_methods || !p.authorization_methods.length
      || p.authorization_methods.includes(this.epTryAuthMethod); },
async loadEpTryAccessPolicy(){
      const e=this.epTry; if(!e) return;
      const methods=this.epTryAuthMethods;
      if(!methods.length){ this.loadEpTryAccess(); return; }
      const rows=await Promise.all(methods.map(async m=>{
        try{ return [m, await this.api('/catalog/endpoints/'+e.id+'/access?authorization_method='+encodeURIComponent(m))]; }
        catch(_){ return [m, null]; }
      }));
      if(this.epTry!==e) return;
      this.epTryAccessByMethod=Object.fromEntries(rows);
      const connected=this.epTryConnectedMethods;
      if(connected.length===1) this.epTryAuthMethod=connected[0];
      else this.epTryAuthMethod=e.authorization_method||methods[0]||'';
      this.epTryAccess=this.epTryAccessByMethod[this.epTryAuthMethod]||null;
    },
loadEpTryAccess(){
      if(!this.epTry) return;
      if(this.epTryAccessByMethod[this.epTryAuthMethod]){
        this.epTryAccess=this.epTryAccessByMethod[this.epTryAuthMethod]; return;
      }
      const q=this.epTryAuthMethod?'?authorization_method='+encodeURIComponent(this.epTryAuthMethod):'';
      this.epTryAccess=null;
      this.api('/catalog/endpoints/'+this.epTry.id+'/access'+q).then(a=>{
        this.epTryAccess=a;
        if(this.epTryAuthMethod) this.epTryAccessByMethod={...this.epTryAccessByMethod,[this.epTryAuthMethod]:a};
      }).catch(()=>{});
    },
async runEpTry(){
      const e=this.epTry; if(!e) return; this.epTryBusy=true; const t0=performance.now();
      try{
        if(this.epTryAudioUrl) URL.revokeObjectURL(this.epTryAudioUrl);
        this.epTryAudioUrl='';
        if(e.id==='fishaudio.voices.list'){
          const r=await fetch('/orgs/'+this.activeOrgId+'/provider-resources?provider=fishaudio&kind=voice',
            {credentials:'include',headers:{...this.headers()}});
          const text=await r.text(); this.epTryStatus=r.status; this.epTryMs=Math.round(performance.now()-t0);
          if(!r.ok) throw new Error(text||('HTTP '+r.status));
          const payload=text?JSON.parse(text):[]; this.epTryResp=JSON.stringify(payload,null,2);
          this.fishVoices=this.fishVoiceRows(payload);
          this.fishVoicesSource=r.headers.get('X-Treg-Resource-Source')||'platform';
          this.fishVoiceNote=this.fishVoicesSource==='byok'
            ?'These voices come from your connected Fish Audio account.'
            :'These voices belong only to this treg team.';
          return;
        }
        const qs=this.epTryVisibleParams.filter(p=>['query','path'].includes(p.location)&&String(p.value)!=='').map(p=>encodeURIComponent(p.name)+'='+encodeURIComponent(p.value)).join('&');
        const opts={method:e.method||'GET', credentials:'include', headers:{...this.headers()}};
        if(this.epTryAuthMethod) opts.headers['X-Treg-Authorization-Method']=this.epTryAuthMethod;
        for(const p of this.epTryVisibleParams.filter(p=>p.location==='header'&&String(p.value)!=='')) opts.headers[p.name]=String(p.value);
        if(this.epTryBodyType==='multipart'){
          const form=new FormData();
          for(const p of this.epTryMultipart){
            if(String(p.type).includes('file')) for(const file of (this.epTryFiles[p.name]||[])) form.append(p.name,file,file.name);
            else if(String(p.value)!=='') form.append(p.name,String(p.value));
          }
          opts.body=form;
        }else if(opts.method!=='GET' && this.epTryBody.trim()){ opts.body=this.epTryBody; opts.headers['content-type']='application/json'; }
        const r=await fetch('/call/'+e.id+(qs?'?'+qs:''), opts);
        this.epTryStatus=r.status; this.epTryMs=Math.round(performance.now()-t0);
        const contentType=(r.headers.get('content-type')||'').toLowerCase();
        if(contentType.startsWith('audio/')){
          const blob=await r.blob(); this.epTryAudioUrl=URL.createObjectURL(blob);
          this.epTryAudioName='speech.'+({'audio/mpeg':'mp3','audio/wav':'wav','audio/opus':'opus'}[contentType.split(';')[0]]||'audio');
          this.epTryResp='Audio ready ('+this.fmtBytes(blob.size)+')';
        }else{
          const txt=await r.text();
          if(!txt){ this.epTryResp='(empty response body - status '+r.status+')'; }
          else{ try{ const parsed=JSON.parse(txt); this.epTryResp=JSON.stringify(parsed,null,2).slice(0,20000); }catch(_){ this.epTryResp=txt.slice(0,8000); } }
        }
        // The charge, read back from the same telemetry Activity renders. The audit writer is
        // fire-and-forget, so give it a beat; also refresh the balance pill.
        setTimeout(async()=>{ try{
          const rows=await this.api('/calls?limit=5');
          const row=(rows||[]).find(c=>c.endpoint_id===e.id);
          if(row && row.cost_charged_micro!=null) this.epTryCost=row.cost_charged_micro;
          this.loadBilling();
        }catch(_){}} , 900);
      }catch(err){ this.epTryResp='Request failed: '+err; this.epTryStatus=0; this.epTryMs=Math.round(performance.now()-t0); }
      finally{ this.epTryBusy=false; }
    },
async runTry(){ this.trying=true; const t0=performance.now();
      try{ const opts={method:this.tryMethod, credentials:'include', headers:{...this.headers()}};
        if(this.tryMethod!=='GET' && (this.tryBody||'').trim()){ opts.body=this.tryBody; opts.headers['content-type']='application/json'; }  // send the body for POST/PUT/DELETE
        const r=await fetch(`/call/${this.tryTool.name}/${this.tryPath}`,opts);
        this.tryStatus=r.status; this.tryMs=Math.round(performance.now()-t0); const txt=await r.text();
        if(!txt){ this.tryResp='(empty response body - status '+r.status+')'; }  // e.g. a 404 with no body left the box blank
        else { try{ this.tryResp=JSON.stringify(JSON.parse(txt),null,2); }catch(_){ this.tryResp=txt.slice(0,4000); } }
      }catch(e){ this.tryResp='Request failed: '+e; this.tryStatus=0; this.tryMs=Math.round(performance.now()-t0); } finally{ this.trying=false; } }
}
