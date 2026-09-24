export default {
async loadTeamResources(){
      if(!this.activeOrgId) return;
      this.teamResourceBusy=true; this.teamResourceErr='';
      try{ this.teamResources=await this.api('/orgs/'+this.activeOrgId+'/provider-resources?source=platform');
        this.teamResourcePage=Math.min(this.teamResourcePage,Math.max(1,Math.ceil(this.teamResources.length/this.teamResourcePageSize))); }
      catch(e){ this.teamResources=[]; this.teamResourceErr='Could not load team resources: '+(e.detail||e.status||e); }
      finally{ this.teamResourceBusy=false; }
    },
async copyTeamResourceId(resource){
      if(!(await this.toClipboard(resource.upstream_id||''))) return;
      this.teamResourceCopied=resource.id; setTimeout(()=>{ if(this.teamResourceCopied===resource.id)this.teamResourceCopied=null; },1400);
    },
closeEpTry(){
      if(this.epTryAudioUrl) URL.revokeObjectURL(this.epTryAudioUrl);
      this.epTryAudioUrl=''; this.epTry=null;
    },
setEpTryFiles(name,event){ this.epTryFiles={...this.epTryFiles,[name]:Array.from(event.target.files||[])}; },
fishVoiceRows(payload){
      const rows=Array.isArray(payload)?payload:(payload&&[payload.items,payload.models,payload.results,payload.data].find(Array.isArray))||[];
      return rows.map((v,i)=>({
        id:v.id||v._id||v.model_id||('fish-'+i), provider:'fishaudio', kind:'voice',
        upstream_id:v.upstream_id||v._id||v.id||v.model_id||'',
        display_name:v.display_name||v.title||v.name||'Untitled voice',
        status:v.status||'active', created_at:v.created_at||v.createdAt||null,
      })).filter(v=>v.upstream_id);
    },
async loadFishVoices(){
      if(!this.activeOrgId) return;
      this.fishVoiceBusy=true; this.fishVoiceNote='';
      try{
        const r=await fetch('/orgs/'+this.activeOrgId+'/provider-resources?provider=fishaudio&kind=voice',
          {credentials:'include',headers:{...this.headers()}});
        if(!r.ok) throw new Error((await r.text())||('HTTP '+r.status));
        this.fishVoices=this.fishVoiceRows(await r.json());
        this.fishVoicesSource=r.headers.get('X-Treg-Resource-Source')||'platform';
        this.fishVoiceNote=this.fishVoicesSource==='byok'
          ?'These voices come from your connected Fish Audio account.'
          :'These voices belong only to this treg team.';
      }catch(err){ this.fishVoices=[]; this.fishVoiceNote='Could not load voices: '+(err.detail||err); }
      finally{ this.fishVoiceBusy=false; }
    },
async useFishVoice(voice){
      const all=[...(this.plats.list||[]),...((this.platData&&this.platData.endpoints)||[])];
      let ep=all.find(e=>e.id==='fishaudio.tts.s2-1-pro');
      try{ if(!ep){ const detail=await this.api('/catalog/endpoints/fishaudio.tts.s2-1-pro'); ep=detail.endpoint||detail; } }
      catch(err){ this.epTryResp='Could not open Fish Audio TTS: '+err; return; }
      this.openEpTry(ep); this.epTryTab='manual';
      try{ const body=JSON.parse(this.epTryBody||'{}'); body.reference_id=voice.upstream_id; this.epTryBody=JSON.stringify(body,null,2); }catch(_){}
    },
beginRenameFishVoice(voice){
      this.fishVoiceDialog={action:'rename',voice,name:voice.display_name||'',busy:false,error:''};
      this.$nextTick(()=>{ const el=this.elements.fishVoiceName; if(el){ el.focus(); el.select(); } });
    },
beginDeleteFishVoice(voice){ this.fishVoiceDialog={action:'delete',voice,name:'',busy:false,error:''}; },
closeFishVoiceDialog(){ if(this.fishVoiceDialog&&!this.fishVoiceDialog.busy)this.fishVoiceDialog=null; },
async submitFishVoiceDialog(){
      const d=this.fishVoiceDialog; if(!d||d.busy)return;
      d.busy=true; d.error='';
      try{
        if(d.action==='rename'){
          const r=await fetch('/call/fishaudio.voices.update?id='+encodeURIComponent(d.voice.upstream_id),{method:'PATCH',credentials:'include',headers:{...this.headers(),'content-type':'application/json'},body:JSON.stringify({title:d.name.trim(),visibility:'private'})});
          if(!r.ok)throw new Error((await r.text())||('HTTP '+r.status));
          this.epTryResp='Renamed voice to “'+d.name.trim()+'”.';
        }else{
          const r=await fetch('/call/fishaudio.voices.delete?id='+encodeURIComponent(d.voice.upstream_id),{method:'DELETE',credentials:'include',headers:{...this.headers()}});
          if(!(r.ok||r.status===404))throw new Error((await r.text())||('HTTP '+r.status));
          this.epTryResp='Deleted “'+(d.voice.display_name||d.voice.upstream_id)+'”.';
        }
        this.fishVoiceDialog=null; const reloads=[this.loadTeamResources()]; if(this.epTry)reloads.push(this.loadFishVoices()); await Promise.all(reloads);
      }catch(err){ d.error=String(err&&err.message||err); d.busy=false; }
    }
}
