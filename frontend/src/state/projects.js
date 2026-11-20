
export default {
async createProject(){ const name=(this.projectName||'').trim();
      if(!name){ this.orgErr='Give the project a name.'; return; }
      this.projBusy=true; this.orgErr='';
      try{ await this.api('/orgs/'+this.activeOrgId+'/projects',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name})});
           this.projectName=''; }
      catch(e){ this.orgErr='Add project failed: '+(e.detail||e.status); }
      this.projBusy=false; await this.loadOrgAdmin(); },
async deleteProject(p){ if(this.confirmProj!==p.id){ this.confirmProj=p.id; return; }
      try{ await this.api('/orgs/'+this.activeOrgId+'/projects/'+p.id,{method:'DELETE'}); }
      catch(e){ this.orgErr='Delete project failed: '+(e.detail||e.status); }
      this.confirmProj=null; this.editProj=null; await this.loadOrgAdmin(); },
projName(pid){ const p=(this.projects||[]).find(x=>x.id===pid); return p?p.name:('project '+pid); },
openProjTools(p){ if(this.editProj===p.id){ this.editProj=null; return; }
      const d={}; (this.tools||[]).forEach(t=>{ d[t.id]=(t.project_id===p.id); });
      this.projToolDraft=d; this.editProj=p.id; },
async saveProjTools(p){ this.projToolBusy=true; this.orgErr='';
      try{ for(const t of (this.tools||[])){
          const inP=(t.project_id===p.id), want=!!this.projToolDraft[t.id];
          if(inP===want) continue;
          // unchecking only frees a tool that is IN this project — never touches another project's tools
          const body={project: want ? p.slug : null};
          await this.api('/tools/'+t.id,{method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
          t.project_id = want ? p.id : null; }
        this.editProj=null; }
      catch(e){ this.orgErr='Save project tools failed: '+(e.detail||e.status); }
      this.projToolBusy=false; await this.loadOrgAdmin(); }
}
