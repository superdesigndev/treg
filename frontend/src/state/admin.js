
export default {
async loadAdmin(){ this.confirmAdmUser=null; this.confirmAdmOrg=null;
      try{ this.adminStats=await this.api('/admin/stats'); this.adminOrgs=await this.api('/admin/orgs'); this.adminUsers=await this.api('/admin/users'); }
      catch(e){ this.err='Admin: '+(e.detail||e.status); } },
async _adm(path, method, body){ this.adminBusy=true; this.err='';
      try{ await this.api(path, {method, ...(body?{headers:{'content-type':'application/json'},body:JSON.stringify(body)}:{})}); await this.loadAdmin(); }
      catch(e){ this.err='Admin action failed: '+(e.detail||e.status); } finally{ this.adminBusy=false; } },
admGrant(u){ this._adm('/admin/users/'+u.id+'/superadmin','POST',{value:!u.is_superadmin}); },
admSuspendUser(u){ this._adm('/admin/users/'+u.id+'/suspend','POST',{value:!u.suspended}); },
admDeleteUser(u){ if(this.confirmAdmUser!==u.id){ this.confirmAdmUser=u.id; return; } this.confirmAdmUser=null; this._adm('/admin/users/'+u.id,'DELETE'); },
admSuspendOrg(o){ this._adm('/admin/orgs/'+o.id+'/suspend','POST',{value:!o.suspended}); },
admDeleteOrg(o){ if(this.confirmAdmOrg!==o.id){ this.confirmAdmOrg=o.id; return; } this.confirmAdmOrg=null; this._adm('/admin/orgs/'+o.id,'DELETE'); }
}
