
export default {
dot(s){ return s==='unknown'||s==='-'?'○':'●'; },
short(e){ return (e||'').split('@')[0]; },
activityAgentKey(a){ return (this.apiKeys||[]).find(k=>k.id===a.api_key_id && k.assigned_type==='agent')||null; },
activityWho(a){ const k=this.activityAgentKey(a); return k?(k.assigned_name||this.short(k.identity)):this.short(a.user_email); },
activityOwner(a){ const k=this.activityAgentKey(a); return k&&k.created_by?this.short(k.created_by):''; },
isPersonal(o){ return !!o && o.name===this.me; },
// personal org = the auto-created one named after your email
    jumpToTeam(){ const t=this.myOrgs.find(o=>!this.isPersonal(o)); if(t) this.switchOrg(t); }
}
