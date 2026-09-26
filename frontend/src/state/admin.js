
export default {
async loadAdmin(){ this.confirmAdmUser=null; this.confirmAdmOrg=null;
      try{ this.adminStats=await this.api('/admin/stats'); this.adminOrgs=await this.api('/admin/orgs'); this.adminUsers=await this.api('/admin/users'); }
      catch(e){ this.err='Admin: '+(e.detail||e.status); }
      this.loadAdminHub(); },
// Hub listing review: a maker's `treg hub list` is a request; approve puts the tool in search,
// reject takes it out with a reason the maker reads. 404 = the hub is off here: no section.
async loadAdminHub(state){ if(state) this.admHub={...this.admHub, state};
      try{ const rows=await this.api('/admin/hub/listings?state='+this.admHub.state);
           const updates=await this.api('/admin/hub/updates').catch(()=>[]);
           this.admHub={...this.admHub, on:true, rows, updates}; }
      catch(e){ this.admHub={...this.admHub, on:false, rows:[]}; } },
async admHubUpdate(u, decision){ const reason=(this.admHub.reason['u:'+u.tool_id]||'').trim();
      if(decision==='reject' && !reason){ this.err='Write the reason first: the maker reads it.'; return; }
      this.admHub={...this.admHub, busy:u.tool_id}; this.err='';
      try{ await this.api('/admin/hub/updates/'+encodeURIComponent(u.tool_id), {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({decision, reason})}); }
      catch(e){ this.err='Update decision failed: '+(e.detail&&e.detail.rule||e.detail||e.status); }
      this.admHub={...this.admHub, busy:null}; await this.loadAdminHub(); },
async admHubDecide(r, decision){ const reason=(this.admHub.reason[r.tool_id]||'').trim();
      if(decision==='reject' && !reason){ this.err='Write the reason first: the maker reads it.'; return; }
      this.admHub={...this.admHub, busy:r.tool_id}; this.err='';
      // The job: what the admin typed, else the manifest's proposal (an empty box sends nothing).
      const cap=(this.admHub.cap[r.tool_id]||'').trim();
      const body={decision, reason, ...(decision==='approve' && cap ? {capability: cap==='none' ? '' : cap} : {})};
      try{ await this.api('/admin/hub/listings/'+encodeURIComponent(r.tool_id), {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)}); }
      catch(e){ this.err='Listing decision failed: '+(e.detail&&e.detail.rule||e.detail||e.status); }
      this.admHub={...this.admHub, busy:null}; await this.loadAdminHub(); },
async _adm(path, method, body){ this.adminBusy=true; this.err='';
      try{ await this.api(path, {method, ...(body?{headers:{'content-type':'application/json'},body:JSON.stringify(body)}:{})}); await this.loadAdmin(); }
      catch(e){ this.err='Admin action failed: '+(e.detail||e.status); } finally{ this.adminBusy=false; } },
admGrant(u){ this._adm('/admin/users/'+u.id+'/superadmin','POST',{value:!u.is_superadmin}); },
admSuspendUser(u){ this._adm('/admin/users/'+u.id+'/suspend','POST',{value:!u.suspended}); },
admDeleteUser(u){ if(this.confirmAdmUser!==u.id){ this.confirmAdmUser=u.id; return; } this.confirmAdmUser=null; this._adm('/admin/users/'+u.id,'DELETE'); },
admSuspendOrg(o){ this._adm('/admin/orgs/'+o.id+'/suspend','POST',{value:!o.suspended}); },
admDeleteOrg(o){ if(this.confirmAdmOrg!==o.id){ this.confirmAdmOrg=o.id; return; } this.confirmAdmOrg=null; this._adm('/admin/orgs/'+o.id,'DELETE'); }
}
