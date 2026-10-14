
export default {
// ---- deny rules ----
    denyWho(uid){ const m=(this.orgMembers||[]).find(x=>x.user_id===uid); return m?m.email:('user '+uid); },
async addDeny(){ const f=this.denyForm;
      if(!f.host && !f.path_prefix && !f.method){ this.orgErr='Give at least a host, a path, or a method.'; return; }
      this.denyBusy=true; this.orgErr='';
      try{ await this.api('/orgs/'+this.activeOrgId+'/deny',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(f)});
           this.denyForm={host:'',path_prefix:'',method:'',user_id:null,project_id:null,note:''}; }
      catch(e){ this.orgErr='Add rule failed: '+(e.detail||e.status); }
      this.denyBusy=false; await this.loadOrgAdmin(); },
async removeDeny(r){ if(this.confirmDeny!==r.id){ this.confirmDeny=r.id; return; }
      try{ await this.api('/orgs/'+this.activeOrgId+'/deny/'+r.id,{method:'DELETE'}); }
      catch(e){ this.orgErr='Remove rule failed: '+(e.detail||e.status); }
      this.confirmDeny=null; await this.loadOrgAdmin(); }
}
